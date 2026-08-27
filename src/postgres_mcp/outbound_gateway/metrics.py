"""Durable gateway metrics, circuit state, and sanitized threshold alerts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from hashlib import sha256
from typing import Any
from typing import Mapping

from postgres_mcp.sql import SafeSqlDriver

from .models import Operation

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_PROVIDER_BY_OPERATION = {
    Operation.EMAIL_SEND: "agent-email",
    Operation.QUO_SMS_SEND: "quo",
    Operation.CLIQ_CHANNEL_POST: "agent-email",
    Operation.CLIQ_CHAT_POST: "agent-email",
    Operation.CALENDAR_CREATE: "agent-email",
    Operation.CALENDAR_UPDATE: "agent-email",
    Operation.CALENDAR_DELETE: "agent-email",
    Operation.TENANTCLOUD_MESSAGE_SEND: "tenantcloud",
    Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: "tenantcloud",
    Operation.TENANTCLOUD_MAINTENANCE_CREATE: "tenantcloud",
    Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: "tenantcloud",
}
_REPLAY_OUTCOMES = (
    "eligible",
    "verified_handled",
    "superseded",
    "manual_review",
    "ineligible",
    "sent",
    "duplicate",
    "no_op_verified_handled",
    "no_op_superseded",
    "no_op_ineligible",
)


@dataclass(frozen=True)
class MetricSample:
    name: str
    value: int | float
    labels: Mapping[str, str] | None = None


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus(samples: tuple[MetricSample, ...] | list[MetricSample]) -> str:
    lines: list[str] = []
    for sample in sorted(
        samples,
        key=lambda item: (
            item.name,
            tuple(sorted((item.labels or {}).items())),
        ),
    ):
        if not _METRIC_NAME.fullmatch(sample.name):
            raise ValueError("invalid metric name")
        labels = ""
        if sample.labels:
            pairs = []
            for key, value in sorted(sample.labels.items()):
                if not _LABEL_NAME.fullmatch(key):
                    raise ValueError("invalid metric label")
                pairs.append(f'{key}="{_escape_label(str(value))}"')
            labels = "{" + ",".join(pairs) + "}"
        value = sample.value
        rendered_value = str(int(value)) if isinstance(value, int) or float(value).is_integer() else str(value)
        lines.append(f"{sample.name}{labels} {rendered_value}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class CircuitStatus:
    is_open: bool
    retry_after_seconds: int
    failure_count: int


@dataclass(frozen=True)
class GatewayAlert:
    kind: str
    action_id: str | None
    operation: str | None
    state: str | None
    detail_code: str
    age_seconds: int
    failure_count: int
    window_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action_id": self.action_id,
            "operation": self.operation,
            "state": self.state,
            "detail_code": self.detail_code,
            "age_seconds": self.age_seconds,
            "failure_count": self.failure_count,
            "window_key": self.window_key,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


ACTION_METRICS_SQL = """
SELECT
    count(*)::bigint AS submitted,
    count(*) FILTER (
        WHERE state = 'completed' AND completion_kind = 'sent'
    )::bigint AS sent,
    count(*) FILTER (
        WHERE state = 'completed' AND completion_kind = 'duplicate'
    )::bigint AS duplicate,
    count(*) FILTER (WHERE state = 'stale')::bigint AS stale,
    count(*) FILTER (WHERE state = 'rejected')::bigint AS rejected,
    count(*) FILTER (
        WHERE state IN ('definitive_failed', 'dead_letter', 'manual_review')
    )::bigint AS failed,
    coalesce(max(extract(epoch FROM (now() - created_at))) FILTER (
        WHERE state IN ('received', 'dependency_wait', 'prepared', 'dispatching',
                        'provider_accepted', 'retry_ready')
    ), 0)::bigint AS pending_oldest_seconds,
    coalesce(max(extract(epoch FROM (now() - updated_at))) FILTER (
        WHERE state IN ('unknown', 'reconciling')
    ), 0)::bigint AS unknown_oldest_seconds,
    count(*) FILTER (WHERE lock_id IS NOT NULL)::bigint AS lock_acquired,
    count(*) FILTER (WHERE lock_id IS NOT NULL AND state = 'completed')::bigint AS lock_completed,
    count(*) FILTER (
        WHERE lock_id IS NOT NULL AND state IN ('stale', 'rejected', 'definitive_failed')
    )::bigint AS lock_released,
    count(*) FILTER (
        WHERE lock_id IS NOT NULL AND state IN ('unknown', 'reconciling', 'dead_letter', 'manual_review')
    )::bigint AS lock_retained
FROM outbound_actions
"""


ATTEMPT_METRICS_SQL = """
SELECT
    count(*) FILTER (WHERE event_kind = 'provider_request_recorded')::bigint AS provider_calls,
    count(*) FILTER (WHERE to_state IN ('retry_ready', 'reconciling'))::bigint AS provider_retries,
    count(*) FILTER (
        WHERE detail_code IN (
            'malformed_provider_success', 'provider_receipt_missing',
            'provider_request_ref_missing'
        )
    )::bigint AS malformed_evidence
FROM outbound_action_attempts
"""


REPLAY_METRICS_SQL = """
SELECT coalesce(outcome, disposition) AS outcome, count(*)::bigint AS count
FROM outbound_replay_items
GROUP BY coalesce(outcome, disposition)
ORDER BY coalesce(outcome, disposition)
"""


CIRCUIT_STATUS_SQL = """
WITH settings AS (
    SELECT
        {}::text AS operation,
        {}::int AS failure_threshold,
        {}::int AS window_seconds,
        {}::int AS open_seconds
), recent AS (
    SELECT
        action_row.operation,
        count(*) FILTER (
            WHERE attempt_row.to_state IN ('unknown', 'definitive_failed', 'dead_letter')
               OR attempt_row.detail_code IN (
                    'malformed_provider_success', 'provider_receipt_missing',
                    'provider_queue_timeout', 'provider_request_ref_missing'
               )
        )::int AS failure_count,
        max(attempt_row.created_at) FILTER (
            WHERE attempt_row.to_state IN ('unknown', 'definitive_failed', 'dead_letter')
               OR attempt_row.detail_code IN (
                    'malformed_provider_success', 'provider_receipt_missing',
                    'provider_queue_timeout', 'provider_request_ref_missing'
               )
        ) AS last_failure_at,
        max(attempt_row.created_at) FILTER (
            WHERE attempt_row.to_state = 'completed'
        ) AS last_success_at
    FROM outbound_action_attempts AS attempt_row
    JOIN outbound_actions AS action_row ON action_row.action_id = attempt_row.action_id
    CROSS JOIN settings
    WHERE action_row.operation = settings.operation
      AND attempt_row.created_at >= now() - (settings.window_seconds * interval '1 second')
    GROUP BY action_row.operation
)
SELECT
    settings.operation,
    coalesce(failure_count, 0)::int AS failure_count,
    (
        coalesce(failure_count, 0) >= settings.failure_threshold
        AND last_failure_at IS NOT NULL
        AND last_failure_at > coalesce(last_success_at, '-infinity'::timestamptz)
        AND last_failure_at > now() - (settings.open_seconds * interval '1 second')
    ) AS is_open,
    CASE
        WHEN last_failure_at IS NULL THEN 0
        ELSE greatest(
            0,
            ceil(settings.open_seconds - extract(epoch FROM (now() - last_failure_at)))::int
        )
    END AS retry_after_seconds
FROM settings
LEFT JOIN recent ON true
"""


ALERT_CANDIDATES_SQL = """
WITH gateway_alert_candidates AS (
    SELECT
        CASE
            WHEN state IN ('unknown', 'reconciling') THEN 'old_unknown'
            ELSE 'old_pending'
        END AS alert_kind,
        action_id::text AS action_id,
        operation,
        state,
        detail_code,
        extract(epoch FROM (now() - updated_at))::int AS age_seconds,
        1::int AS failure_count
    FROM outbound_actions
    WHERE state IN (
        'received', 'dependency_wait', 'prepared', 'dispatching',
        'provider_accepted', 'retry_ready', 'unknown', 'reconciling'
    )
      AND updated_at <= now() - ({} * interval '1 second')

    UNION ALL

    SELECT
        'completion_failure', action_id::text, operation, state, detail_code,
        extract(epoch FROM (now() - updated_at))::int, 1::int
    FROM outbound_actions
    WHERE state = 'provider_accepted'
      AND updated_at <= now() - ({} * interval '1 second')

    UNION ALL

    SELECT
        'repeated_evidence_failure', NULL, action_row.operation, NULL,
        attempt_row.detail_code, 0,
        count(*)::int
    FROM outbound_action_attempts AS attempt_row
    JOIN outbound_actions AS action_row ON action_row.action_id = attempt_row.action_id
    WHERE attempt_row.detail_code IN (
        'malformed_provider_success', 'provider_receipt_missing',
        'provider_request_ref_missing'
    )
      AND attempt_row.created_at >= now() - ({} * interval '1 second')
    GROUP BY action_row.operation, attempt_row.detail_code
    HAVING count(*) >= {}
)
SELECT alert_kind, action_id, operation, state, detail_code, age_seconds, failure_count
FROM gateway_alert_candidates
ORDER BY alert_kind, action_id NULLS LAST, operation, detail_code
"""


class GatewayObservability:
    def __init__(
        self,
        driver: Any,
        *,
        circuit_failure_threshold: int = 5,
        circuit_window_seconds: int = 300,
        circuit_open_seconds: int = 180,
        old_action_seconds: int = 300,
        evidence_failure_threshold: int = 3,
        alert_window_seconds: int = 300,
    ):
        self._driver = driver
        self.circuit_failure_threshold = max(1, circuit_failure_threshold)
        self.circuit_window_seconds = max(1, circuit_window_seconds)
        self.circuit_open_seconds = max(1, circuit_open_seconds)
        self.old_action_seconds = max(1, old_action_seconds)
        self.evidence_failure_threshold = max(1, evidence_failure_threshold)
        self.alert_window_seconds = max(1, alert_window_seconds)

    async def _rows(self, query: str, params: list[Any] | None = None) -> list[Mapping[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self._driver, query, params or [])  # type: ignore[arg-type]
        return [row.cells for row in rows or []]

    async def database_healthy(self) -> bool:
        try:
            rows = await self._rows("SELECT 1 AS healthy")
        except Exception:  # database outage must become an unhealthy response
            return False
        return bool(rows and rows[0].get("healthy") == 1)

    async def collect(self) -> tuple[MetricSample, ...]:
        action_rows = await self._rows(ACTION_METRICS_SQL)
        attempt_rows = await self._rows(ATTEMPT_METRICS_SQL)
        replay_rows = await self._rows(REPLAY_METRICS_SQL)
        action = action_rows[0] if action_rows else {}
        attempts = attempt_rows[0] if attempt_rows else {}
        samples: list[MetricSample] = []
        for outcome in ("submitted", "sent", "duplicate", "stale", "rejected", "failed"):
            samples.append(
                MetricSample(
                    "outbound_gateway_actions_total",
                    int(action.get(outcome) or 0),
                    {"outcome": outcome},
                )
            )
        samples.extend(
            (
                MetricSample(
                    "outbound_gateway_provider_calls_total",
                    int(attempts.get("provider_calls") or 0),
                ),
                MetricSample(
                    "outbound_gateway_provider_retries_total",
                    int(attempts.get("provider_retries") or 0),
                ),
                MetricSample(
                    "outbound_gateway_malformed_evidence_total",
                    int(attempts.get("malformed_evidence") or 0),
                ),
                MetricSample(
                    "outbound_gateway_pending_oldest_seconds",
                    int(action.get("pending_oldest_seconds") or 0),
                ),
                MetricSample(
                    "outbound_gateway_unknown_oldest_seconds",
                    int(action.get("unknown_oldest_seconds") or 0),
                ),
            )
        )
        for outcome, column in (
            ("acquired", "lock_acquired"),
            ("completed", "lock_completed"),
            ("released", "lock_released"),
            ("retained", "lock_retained"),
        ):
            samples.append(
                MetricSample(
                    "outbound_gateway_locks_total",
                    int(action.get(column) or 0),
                    {"outcome": outcome},
                )
            )
        replay_counts = {
            str(row.get("outcome") or "unknown"): int(row.get("count") or 0)
            for row in replay_rows
        }
        for outcome in _REPLAY_OUTCOMES:
            replay_counts.setdefault(outcome, 0)
        samples.extend(
            MetricSample(
                "outbound_gateway_replay_items_total",
                count,
                {"outcome": outcome},
            )
            for outcome, count in replay_counts.items()
        )
        for operation in Operation:
            status = await self.circuit_status(operation)
            samples.append(
                MetricSample(
                    "outbound_gateway_circuit_open",
                    int(status.is_open),
                    {
                        "operation": operation.value,
                        "provider": _PROVIDER_BY_OPERATION[operation],
                    },
                )
            )
        return tuple(samples)

    async def circuit_status(self, operation: Operation) -> CircuitStatus:
        rows = await self._rows(
            CIRCUIT_STATUS_SQL,
            [
                operation.value,
                self.circuit_failure_threshold,
                self.circuit_window_seconds,
                self.circuit_open_seconds,
            ],
        )
        row = rows[0] if rows else {}
        return CircuitStatus(
            is_open=bool(row.get("is_open")),
            retry_after_seconds=max(0, int(row.get("retry_after_seconds") or 0)),
            failure_count=max(0, int(row.get("failure_count") or 0)),
        )

    async def scan_alerts(self, *, now: datetime | None = None) -> tuple[GatewayAlert, ...]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rows = await self._rows(
            ALERT_CANDIDATES_SQL,
            [
                self.old_action_seconds,
                self.old_action_seconds,
                self.circuit_window_seconds,
                self.evidence_failure_threshold,
            ],
        )
        candidates = [self._alert_from_row(row, current) for row in rows]
        for operation in Operation:
            circuit = await self.circuit_status(operation)
            if circuit.is_open:
                candidates.append(
                    GatewayAlert(
                        kind="open_circuit",
                        action_id=None,
                        operation=operation.value,
                        state=None,
                        detail_code="provider_circuit_open",
                        age_seconds=0,
                        failure_count=circuit.failure_count,
                        window_key=self._window_key(current),
                    )
                )
        emitted: list[GatewayAlert] = []
        for alert in candidates:
            if await self._record_alert(alert):
                emitted.append(alert)
        return tuple(emitted)

    def _alert_from_row(self, row: Mapping[str, Any], now: datetime) -> GatewayAlert:
        return GatewayAlert(
            kind=str(row["alert_kind"]),
            action_id=str(row["action_id"]) if row.get("action_id") else None,
            operation=str(row["operation"]) if row.get("operation") else None,
            state=str(row["state"]) if row.get("state") else None,
            detail_code=str(row.get("detail_code") or "unknown"),
            age_seconds=max(0, int(row.get("age_seconds") or 0)),
            failure_count=max(0, int(row.get("failure_count") or 0)),
            window_key=self._window_key(now),
        )

    def _window_key(self, now: datetime) -> str:
        epoch = int(now.timestamp())
        start = epoch - (epoch % self.alert_window_seconds)
        return str(start)

    async def _record_alert(self, alert: GatewayAlert) -> bool:
        fingerprint_material = {
            "action_id": alert.action_id,
            "detail_code": alert.detail_code,
            "kind": alert.kind,
            "operation": alert.operation,
            "state": alert.state,
            "window_key": alert.window_key,
        }
        fingerprint = sha256(
            json.dumps(
                fingerprint_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        rows = await self._rows(
            """
            SELECT record_outbound_gateway_alert(
                {}, {}, {}, {}, {}, {}, {}, {}
            ) AS inserted
            """,
            [
                fingerprint,
                alert.kind,
                alert.action_id,
                alert.operation,
                alert.state,
                alert.detail_code,
                alert.window_key,
                alert.failure_count,
            ],
        )
        return bool(rows and rows[0].get("inserted"))


def bounded_backoff_seconds(
    attempt_count: int,
    *,
    base_seconds: int = 5,
    max_seconds: int = 900,
) -> int:
    exponent = max(0, min(int(attempt_count) - 1, 20))
    return min(max(1, max_seconds), max(1, base_seconds) * int(math.pow(2, exponent)))
