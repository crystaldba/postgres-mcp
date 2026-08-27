# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from postgres_mcp.outbound_gateway.metrics import CircuitStatus
from postgres_mcp.outbound_gateway.metrics import GatewayObservability
from postgres_mcp.outbound_gateway.metrics import MetricSample
from postgres_mcp.outbound_gateway.metrics import render_prometheus
from postgres_mcp.outbound_gateway.models import Operation


class Row:
    def __init__(self, cells):
        self.cells = cells


def test_prometheus_renderer_covers_durable_gateway_surface_and_sorts_labels():
    rendered = render_prometheus(
        (
            MetricSample("outbound_gateway_actions_total", 8, {"outcome": "submitted"}),
            MetricSample("outbound_gateway_actions_total", 3, {"outcome": "sent"}),
            MetricSample("outbound_gateway_actions_total", 1, {"outcome": "duplicate"}),
            MetricSample("outbound_gateway_actions_total", 1, {"outcome": "stale"}),
            MetricSample("outbound_gateway_actions_total", 1, {"outcome": "rejected"}),
            MetricSample("outbound_gateway_actions_total", 2, {"outcome": "failed"}),
            MetricSample("outbound_gateway_provider_calls_total", 4),
            MetricSample("outbound_gateway_provider_retries_total", 2),
            MetricSample("outbound_gateway_malformed_evidence_total", 1),
            MetricSample("outbound_gateway_locks_total", 4, {"outcome": "acquired"}),
            MetricSample("outbound_gateway_replay_items_total", 2, {"outcome": "eligible"}),
            MetricSample("outbound_gateway_pending_oldest_seconds", 70),
            MetricSample("outbound_gateway_unknown_oldest_seconds", 40),
            MetricSample(
                "outbound_gateway_circuit_open",
                1,
                {"provider": "agent-email", "operation": "email.send"},
            ),
        )
    )

    for name in (
        "outbound_gateway_actions_total",
        "outbound_gateway_provider_calls_total",
        "outbound_gateway_provider_retries_total",
        "outbound_gateway_malformed_evidence_total",
        "outbound_gateway_locks_total",
        "outbound_gateway_replay_items_total",
        "outbound_gateway_pending_oldest_seconds",
        "outbound_gateway_unknown_oldest_seconds",
        "outbound_gateway_circuit_open",
    ):
        assert name in rendered
    assert 'outbound_gateway_circuit_open{operation="email.send",provider="agent-email"} 1' in rendered
    assert "message_body" not in rendered
    assert "recipient" not in rendered
    assert "authorization" not in rendered.casefold()


@pytest.mark.asyncio
async def test_collect_uses_durable_aggregates_for_actions_attempts_locks_and_replays():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        if "FROM outbound_actions" in query and "submitted" in query:
            return [
                Row(
                    {
                        "submitted": 10,
                        "sent": 4,
                        "duplicate": 2,
                        "stale": 1,
                        "rejected": 1,
                        "failed": 2,
                        "pending_oldest_seconds": 80,
                        "unknown_oldest_seconds": 50,
                        "lock_acquired": 5,
                        "lock_completed": 6,
                        "lock_released": 2,
                        "lock_retained": 1,
                    }
                )
            ]
        if "FROM outbound_action_attempts" in query:
            return [Row({"provider_calls": 6, "provider_retries": 2, "malformed_evidence": 1})]
        if "FROM outbound_replay_items" in query:
            return [Row({"outcome": "eligible", "count": 3}), Row({"outcome": "verified_handled", "count": 2})]
        if "failure_count" in query:
            return [
                Row(
                    {
                        "operation": "email.send",
                        "provider": "agent-email",
                        "failure_count": 0,
                        "is_open": False,
                        "retry_after_seconds": 0,
                    }
                )
            ]
        raise AssertionError(query)

    observer = GatewayObservability(object())
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        samples = await observer.collect()

    rendered = render_prometheus(samples)
    assert 'outbound_gateway_actions_total{outcome="submitted"} 10' in rendered
    assert 'outbound_gateway_actions_total{outcome="duplicate"} 2' in rendered
    assert 'outbound_gateway_locks_total{outcome="retained"} 1' in rendered
    assert 'outbound_gateway_replay_items_total{outcome="eligible"} 3' in rendered
    assert "SELECT" in calls[0][0]
    assert all("body" not in query.casefold() for query, _ in calls)
    assert all("recipient_scope" not in query.casefold() for query, _ in calls)


@pytest.mark.asyncio
async def test_collect_always_exposes_zero_replay_outcomes_when_ledger_is_empty():
    async def execute(_driver, query, _params):
        if "FROM outbound_actions" in query and "submitted" in query:
            return [Row({})]
        if "FROM outbound_action_attempts" in query:
            return [Row({})]
        if "FROM outbound_replay_items" in query:
            return []
        if "failure_count" in query:
            return [Row({"failure_count": 0, "is_open": False, "retry_after_seconds": 0})]
        raise AssertionError(query)

    observer = GatewayObservability(object())
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        rendered = render_prometheus(await observer.collect())

    assert 'outbound_gateway_replay_items_total{outcome="eligible"} 0' in rendered
    assert 'outbound_gateway_replay_items_total{outcome="sent"} 0' in rendered


@pytest.mark.asyncio
async def test_database_health_is_one_bounded_select():
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(return_value=[Row({"healthy": 1})]),
    ) as execute:
        healthy = await GatewayObservability(object()).database_healthy()

    assert healthy is True
    assert execute.await_args.args[1].strip() == "SELECT 1 AS healthy"


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_and_reports_bounded_retry():
    async def execute(_driver, query, params):
        assert "failure_count" in query
        assert params == ["email.send", 5, 300, 180]
        return [
            Row(
                {
                    "operation": "email.send",
                    "provider": "agent-email",
                    "failure_count": 5,
                    "is_open": True,
                    "retry_after_seconds": 121,
                }
            )
        ]

    observer = GatewayObservability(object(), circuit_failure_threshold=5, circuit_window_seconds=300, circuit_open_seconds=180)
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        status = await observer.circuit_status(Operation.EMAIL_SEND)

    assert status == CircuitStatus(is_open=True, retry_after_seconds=121, failure_count=5)


@pytest.mark.asyncio
async def test_threshold_scan_emits_sanitized_deduplicated_alerts():
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        if "gateway_alert_candidates" in query:
            return [
                Row(
                    {
                        "alert_kind": "old_unknown",
                        "action_id": "4cbac369-48c6-5b62-95e9-41f50259e732",
                        "operation": "email.send",
                        "state": "unknown",
                        "detail_code": "provider_queue_timeout",
                        "age_seconds": 901,
                        "failure_count": 1,
                    }
                ),
                Row(
                    {
                        "alert_kind": "repeated_evidence_failure",
                        "action_id": None,
                        "operation": "calendar.create",
                        "state": None,
                        "detail_code": "malformed_provider_success",
                        "age_seconds": 0,
                        "failure_count": 4,
                    }
                ),
                Row(
                    {
                        "alert_kind": "completion_failure",
                        "action_id": "aaaaaaaa-48c6-5b62-95e9-41f50259e732",
                        "operation": "quo.sms.send",
                        "state": "provider_accepted",
                        "detail_code": "provider_receipt_missing",
                        "age_seconds": 301,
                        "failure_count": 1,
                    }
                ),
            ]
        if "record_outbound_gateway_alert" in query:
            return [Row({"inserted": len([call for call in calls if "record_outbound_gateway_alert" in call[0]]) <= 2})]
        if "failure_count" in query:
            return [
                Row(
                    {
                        "operation": "email.send",
                        "provider": "agent-email",
                        "failure_count": 5,
                        "is_open": True,
                        "retry_after_seconds": 120,
                    }
                )
            ]
        raise AssertionError(query)

    observer = GatewayObservability(object(), alert_window_seconds=300)
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        alerts = await observer.scan_alerts(now=now)

    assert {alert.kind for alert in alerts} == {"old_unknown", "repeated_evidence_failure"}
    rendered = "\n".join(alert.as_json() for alert in alerts)
    assert "message_body" not in rendered
    assert "recipient" not in rendered
    assert "secret" not in rendered
    assert all(
        set(alert.as_dict())
        <= {
            "kind",
            "action_id",
            "operation",
            "state",
            "detail_code",
            "age_seconds",
            "failure_count",
            "window_key",
        }
        for alert in alerts
    )


@pytest.mark.asyncio
async def test_alert_fingerprint_is_stable_while_age_changes_inside_window():
    fingerprints = []

    async def execute(_driver, query, params):
        if "gateway_alert_candidates" in query:
            age_seconds = 301 if not fingerprints else 302
            return [
                Row(
                    {
                        "alert_kind": "old_unknown",
                        "action_id": "4cbac369-48c6-5b62-95e9-41f50259e732",
                        "operation": "email.send",
                        "state": "unknown",
                        "detail_code": "provider_queue_timeout",
                        "age_seconds": age_seconds,
                        "failure_count": 1,
                    }
                )
            ]
        if "record_outbound_gateway_alert" in query:
            fingerprints.append(params[0])
            return [Row({"inserted": len(fingerprints) == 1})]
        if "failure_count" in query:
            return [Row({"failure_count": 0, "is_open": False, "retry_after_seconds": 0})]
        raise AssertionError(query)

    observer = GatewayObservability(object(), alert_window_seconds=300)
    with patch(
        "postgres_mcp.outbound_gateway.metrics.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        first = await observer.scan_alerts(now=datetime(2026, 7, 16, 12, 0, 1, tzinfo=timezone.utc))
        second = await observer.scan_alerts(now=datetime(2026, 7, 16, 12, 0, 2, tzinfo=timezone.utc))

    assert len(first) == 1
    assert second == ()
    assert fingerprints[0] == fingerprints[1]
