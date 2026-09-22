# pyright: reportArgumentType=false, reportOptionalMemberAccess=false

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import MappingProxyType
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID

import pytest
from pglast import parse_sql

from postgres_mcp.outbound_gateway.context import ActionContext
from postgres_mcp.outbound_gateway.context import DerivedTarget
from postgres_mcp.outbound_gateway.evidence import DatabasePreflightEvidenceLoader
from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.preflight import CalendarDependencyState
from postgres_mcp.outbound_gateway.preflight import RefreshStatus

NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def context(**overrides):
    values = dict(
        action_id=UUID("4cbac369-48c6-5b62-95e9-41f50259e732"),
        wakeup_event_id=7,
        action_role=ActionRole.PROSPECT_REPLY,
        operation=Operation.EMAIL_SEND,
        intent_kind=IntentKind.SHOWING_OFFER,
        appointment_slot=datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        arguments=MappingProxyType({"text": "hello"}),
        source="zillow",
        source_message_id=700,
        source_message_key="zillow:700",
        source_sent_at=NOW,
        conversation_id="conversation:zillow-1",
        conversation_watermark=700,
        prospect_id="prospect:amanda",
        aliases=("email:amanda@example.com",),
        property_id="building:bullman-st",
        property_label="138 Bullman St #144-A",
        target=DerivedTarget("email_thread", "lead@convo.zillow.com", True),
        provider_account="nigel-zoho",
        routing_policy_version="v1",
        canonical_scope=MappingProxyType({"version": "v1"}),
        canonical_context=MappingProxyType({"identity_version": "v1"}),
        payload_hash="a" * 64,
        lock_holder="outbound-gateway:4cbac369-48c6-5b62-95e9-41f50259e732",
        thread_identity="zrm-thread-1",
        showing_lifecycle_id="showing:7",
        calendar_event_uid=None,
        channel_id=44,
        refresh_evidence=MappingProxyType(
            {
                "status": "covered",
                "covered_through": "2026-07-16T01:00:00Z",
                "covered_thread_identity": "zrm-thread-1",
                "attempt_count": 1,
            }
        ),
    )
    values.update(overrides)
    return ActionContext(**values)


class Row:
    def __init__(self, cells):
        self.cells = cells


@pytest.mark.asyncio
async def test_evidence_loader_reads_message_receipts_and_refresh_without_staff_gate():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": 701,
                    "verified_outbound_request_ref": "provider-1",
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    loader = DatabasePreflightEvidenceLoader(object())
    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        proof = await loader.load(context())

    assert proof.verified_outbound_message_id == 701
    assert proof.verified_outbound_request_ref == "provider-1"
    assert proof.verified_outbound_covers_source is True
    assert proof.calendar_dependency is CalendarDependencyState.NOT_REQUIRED
    assert proof.refresh.status is RefreshStatus.COVERED
    assert proof.refresh.covered_thread_identity == "zrm-thread-1"
    query = calls[0][0].casefold()
    parse_sql(calls[0][0].replace("{}", "NULL"))
    assert "verified_staff" not in query
    assert "showing_slot_validation" not in query
    assert "calendar_events" not in query
    assert "related_messages" in query
    assert "'{{data,object,conversationid}}'" in query
    assert "'{{data,object,phonenumberid}}'" in query
    assert "'{{data,object,direction}}'" in query
    assert "related.payload->'provider_ids'->>'message'" in query
    assert "related.source_message_id" in query
    assert "recipient.value->>'address'" in query
    assert query.count("(related.sent_at, related.id) > (") == 2
    assert "related.id = any({}::bigint[])" in query
    assert calls[0][1] == [
        "zillow",
        "lead@convo.zillow.com",
        "lead@convo.zillow.com",
        "zillow",
        "nigel-zoho",
        "zrm-thread-1",
        "zrm-thread-1",
        "",
        "",
        "zillow",
        44,
        NOW,
        700,
        [700],
        [],
        NOW,
        700,
        "zillow",
        "zillow",
        "zillow",
        "showing_offer",
        7,
        7,
        NOW,
    ]


@pytest.mark.asyncio
async def test_evidence_excludes_only_canonical_cross_channel_duplicate_from_newer_inbound():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": None,
                    "verified_outbound_request_ref": None,
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    duplicate_context = context(cross_channel_duplicate_message_ids=(196337,))
    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await DatabasePreflightEvidenceLoader(object()).load(duplicate_context)

    query, params = calls[0]
    normalized_query = " ".join(query.split())
    assert ("NOT ( coalesce(related.canonical_message_id, related.id) = ANY({}::bigint[]) )") in normalized_query
    assert params[13] == [700, 196337]


@pytest.mark.asyncio
async def test_evidence_excludes_durable_same_message_canonical_duplicates():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": None,
                    "verified_outbound_request_ref": None,
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await DatabasePreflightEvidenceLoader(object()).load(context())

    query, params = calls[0]
    normalized_query = " ".join(query.split())
    assert "message_row.canonical_message_id" in normalized_query
    assert "coalesce(related.canonical_message_id, related.id)" in normalized_query
    assert "= ANY({}::bigint[])" in normalized_query
    assert params[13] == [700]


@pytest.mark.asyncio
async def test_evidence_excludes_only_certified_zillow_scrape_rows_from_newer_inbound():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": None,
                    "verified_outbound_request_ref": None,
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    certified_context = context(certified_older_message_ids=(197065, 197067))
    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await DatabasePreflightEvidenceLoader(object()).load(certified_context)

    query, params = calls[0]
    normalized_query = " ".join(query.split())
    expected = "NOT ( lower(related.source) = 'zillow_rm_web_extract' AND related.id = ANY({}::bigint[]) )"
    assert expected in normalized_query
    assert params[14] == [197065, 197067]


@pytest.mark.asyncio
async def test_quo_evidence_query_binds_conversation_line_target_and_nested_receipt():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": 702,
                    "verified_outbound_request_ref": "quo-provider-702",
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    quo_context = context(
        operation=Operation.QUO_SMS_SEND,
        source="quo",
        target=DerivedTarget("quo_conversation", "conversation-live", True),
        provider_account="PN-line-live",
        thread_identity="conversation-live",
        recipient_phone="+19085550199",
    )
    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        proof = await DatabasePreflightEvidenceLoader(object()).load(quo_context)

    assert proof.verified_outbound_request_ref == "quo-provider-702"
    query = calls[0][0].casefold()
    assert "regexp_replace" in query
    assert "related.payload#>>'{{data,object,id}}'" in query
    assert "lower(message_row.source) in ('quo', 'openphone')" in query
    assert calls[0][1][4:9] == [
        "PN-line-live",
        "conversation-live",
        "conversation-live",
        "19085550199",
        "19085550199",
    ]


@pytest.mark.asyncio
async def test_same_building_calendar_overlap_is_not_loaded_as_blocking_evidence():
    async def execute(_driver, _query, _params):
        return [
            Row(
                {
                    "later_inbound_message_id": None,
                    "verified_outbound_message_id": None,
                    "verified_outbound_request_ref": None,
                    "latest_sent_at": NOW,
                    "calendar_dependency_state": "not_required",
                    "calendar_already_applied": False,
                }
            )
        ]

    with patch(
        "postgres_mcp.outbound_gateway.evidence.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        proof = await DatabasePreflightEvidenceLoader(object()).load(context())

    assert proof.overlapping_showing_prospect_ids == ()


@pytest.mark.asyncio
async def test_evidence_query_survives_literal_json_path_braces():
    class Driver:
        async def execute_query(self, query, *args, **kwargs):
            assert "#>>'{data,object,phoneNumberId}'" in query
            assert "#>>'{data,object,conversationId}'" in query
            assert "#>>'{data,object,direction}'" in query
            return [
                Row(
                    {
                        "later_inbound_message_id": None,
                        "verified_outbound_message_id": None,
                        "verified_outbound_request_ref": None,
                        "latest_sent_at": NOW,
                        "calendar_dependency_state": "not_required",
                        "calendar_already_applied": False,
                    }
                )
            ]

    proof = await DatabasePreflightEvidenceLoader(Driver()).load(context())

    assert proof.calendar_dependency is CalendarDependencyState.NOT_REQUIRED
