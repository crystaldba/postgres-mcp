# pyright: reportArgumentType=false, reportOptionalMemberAccess=false

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import MappingProxyType
from uuid import UUID

import pytest

from postgres_mcp.outbound_gateway.adapters.base import ProviderDisposition
from postgres_mcp.outbound_gateway.adapters.base import ProviderObservation
from postgres_mcp.outbound_gateway.adapters.base import transport_observation
from postgres_mcp.outbound_gateway.adapters.calendar import CalendarAdapter
from postgres_mcp.outbound_gateway.adapters.cliq import CliqAdapter
from postgres_mcp.outbound_gateway.adapters.email import EmailAdapter
from postgres_mcp.outbound_gateway.adapters.quo import QuoSmsAdapter
from postgres_mcp.outbound_gateway.adapters.tenantcloud import TenantCloudAdapter
from postgres_mcp.outbound_gateway.context import ActionContext
from postgres_mcp.outbound_gateway.context import DerivedTarget
from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.provider_client import McpCallResult
from postgres_mcp.outbound_gateway.provider_client import TransportErrorKind

ACTION_ID = UUID("4cbac369-48c6-5b62-95e9-41f50259e732")
ACTION_UID = UUID("9ebddbf7-8fc8-5a4f-bba7-869ea7053521")
NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def call(self, server_name, tool, arguments):
        self.calls.append((server_name, tool, arguments))
        return self.results.pop(0)


def context(operation=Operation.EMAIL_SEND, **overrides):
    role = (
        ActionRole.CALENDAR_MUTATION
        if operation.value.startswith("calendar.")
        else ActionRole.INTERNAL_NOTIFICATION
        if operation.value.startswith("cliq.")
        else ActionRole.PROSPECT_REPLY
    )
    target = {
        Operation.EMAIL_SEND: DerivedTarget("email_thread", "lead@convo.zillow.com", True),
        Operation.QUO_SMS_SEND: DerivedTarget("quo_conversation", "quo-thread-1", True),
        Operation.CLIQ_CHANNEL_POST: DerivedTarget("cliq_channel", "tenant-leads", True),
        Operation.CLIQ_CHAT_POST: DerivedTarget("cliq_chat", "CT_123", True),
        Operation.CALENDAR_CREATE: DerivedTarget("calendar", "nigel", True),
        Operation.CALENDAR_UPDATE: DerivedTarget("calendar", "nigel", True),
        Operation.CALENDAR_DELETE: DerivedTarget("calendar", "nigel", True),
    }[operation]
    intent = {
        Operation.EMAIL_SEND: IntentKind.SHOWING_OFFER,
        Operation.QUO_SMS_SEND: IntentKind.SHOWING_OFFER,
        Operation.CLIQ_CHANNEL_POST: IntentKind.LEAD_ALERT,
        Operation.CLIQ_CHAT_POST: IntentKind.MANUAL_REVIEW_ALERT,
        Operation.CALENDAR_CREATE: IntentKind.SHOWING_CREATE,
        Operation.CALENDAR_UPDATE: IntentKind.SHOWING_UPDATE,
        Operation.CALENDAR_DELETE: IntentKind.SHOWING_DELETE,
    }[operation]
    values = dict(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=role,
        operation=operation,
        intent_kind=intent,
        appointment_slot=None if operation is Operation.CALENDAR_DELETE else datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        arguments=MappingProxyType(
            {}
            if operation is Operation.CALENDAR_DELETE
            else {"text": "Friday at 10:30 works. — Nigel"}
            if not operation.value.startswith("calendar.")
            else {"description": "Tour"}
        ),
        source="zillow",
        source_message_id=700,
        source_message_key="zillow_rm_web_extract:700",
        source_sent_at=NOW,
        conversation_id="conversation:zillow-1",
        conversation_watermark=700,
        prospect_id="prospect:amanda",
        aliases=("email:amanda@example.com",),
        property_id="building:bullman-st",
        property_label="138 Bullman St #144-A",
        target=target,
        provider_account="nigel-zoho"
        if operation is Operation.EMAIL_SEND
        else "leasing-line"
        if operation is Operation.QUO_SMS_SEND
        else target.target_id,
        routing_policy_version="v1",
        canonical_scope=MappingProxyType({"version": "v1"}),
        canonical_context=MappingProxyType({"identity_version": "v1"}),
        payload_hash="a" * 64,
        lock_holder=f"outbound-gateway:{ACTION_ID}",
        thread_identity="zrm-thread-1",
        showing_lifecycle_id="showing:7",
        calendar_event_uid="existing-event" if operation in {Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE} else None,
        source_subject="Zillow inquiry for 138 Bullman St #144-A",
        prospect_name="Amanda Snyder",
        recipient_phone="+19085550199" if operation is Operation.QUO_SMS_SEND else None,
        calendar_event_url="https://calendar.local/events/existing-event.ics"
        if operation in {Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE}
        else None,
        calendar_event_etag='"etag-1"' if operation in {Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE} else None,
    )
    values.update(overrides)
    return ActionContext(**values)


def pending(request_id="req-1"):
    return McpCallResult(structured_content={"status": "pending", "request_id": request_id, "call_id": request_id})


def completed(tool, content):
    return McpCallResult(
        structured_content={
            "status": "completed",
            "request_id": "req-1",
            "result": {"tool_name": tool, "structured_content": content},
        }
    )


def calendar_request_status_envelope(event):
    """The verified real `request_status` shape for a completed async job.

    Agent Email's queue wraps the terminal tool result in an envelope with
    its own status/timing metadata, and the inner tool result carries the
    event object under `data.event` (not `data.content`).
    """
    return McpCallResult(
        structured_content={
            "call_id": "req-1",
            "request_id": "req-1",
            "tool_name": "calendar_create_event",
            "account_id": "nigel-zoho",
            "status": "completed",
            "queued_at": "2026-07-16T01:00:00Z",
            "started_at": "2026-07-16T01:00:01Z",
            "completed_at": "2026-07-16T01:00:02Z",
            "duration_ms": 850,
            "category": None,
            "retryable": False,
            "message": "completed",
            "result": {
                "tool_name": "calendar_create_event",
                "structured_content": {
                    "status": "success",
                    "data": {"event": event} if event is not None else {},
                },
            },
        }
    )


def test_auth_rejection_is_retryable_definitive_non_acceptance():
    observation = transport_observation(
        McpCallResult(
            error_kind=TransportErrorKind.AUTH_REJECTED,
            is_error=True,
            safe_detail="provider_auth_rejected",
        )
    )

    assert observation is not None
    assert observation.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE
    assert observation.detail_code == "provider_auth_rejected"
    assert observation.category == "provider_authentication"
    assert observation.retryable is True


@pytest.mark.asyncio
async def test_email_adapter_derives_recipient_and_returns_structured_receipt():
    adapter = EmailAdapter(
        sender_domains={"nigel-zoho": "pfg.example"},
        cc_by_source={"zillow": "management@pfg.io"},
    )
    request = adapter.build_request(context(), ACTION_UID)
    assert request.server_name == "agent-email"
    assert request.tool == "email_send"
    assert request.arguments == {
        "account_id": "nigel-zoho",
        "to": [{"address": "lead@convo.zillow.com"}],
        "cc": [{"address": "management@pfg.io"}],
        "subject": "Re: Zillow inquiry for 138 Bullman St #144-A",
        "text": "Friday at 10:30 works. — Nigel",
        "outbound_action_uid": str(ACTION_UID),
    }
    client = FakeClient(pending(), completed("email_send", {"status": "success", "provider_message_id": "<mail-1@example.com>"}))
    observation = await adapter.invoke(client, request)
    assert observation.disposition is ProviderDisposition.PENDING
    observation = await adapter.poll(client, observation)
    assert observation.disposition is ProviderDisposition.ACCEPTED
    receipt = adapter.parse_receipt(context(), observation)
    assert receipt is not None
    assert receipt.provider_message_id == "<mail-1@example.com>"
    assert receipt.provider_request_ref == "req-1"


@pytest.mark.asyncio
async def test_email_reconciliation_reads_queued_thread_result_text():
    adapter = EmailAdapter(sender_domains={"nigel-zoho": "pfg.example"})
    unknown = await adapter.invoke(
        FakeClient(
            McpCallResult(
                error_kind=TransportErrorKind.TIMEOUT,
                is_error=True,
            )
        ),
        adapter.build_request(context(), ACTION_UID),
    )
    client = FakeClient(
        pending("thread-lookup-1"),
        McpCallResult(
            text=("**Request ID:** thread-lookup-1\n**Status:** completed\n**Result:** queued provider result follows"),
            structured_content={
                "status": "completed",
                "request_id": "thread-lookup-1",
                "result": {
                    "tool_name": "email_get_thread",
                    "structured_content": {
                        "status": "success",
                        "data": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "**Thread:** exact deterministic message",
                                }
                            ]
                        },
                    },
                },
            },
        ),
    )

    reconciled = await adapter.reconcile(client, context(), ACTION_UID, unknown)

    assert reconciled.disposition is ProviderDisposition.ACCEPTED
    assert reconciled.detail_code == "email_reconciled_by_message_id"
    assert reconciled.message_id == f"<outbound-action-{ACTION_UID}@pfg.example>"


@pytest.mark.asyncio
async def test_email_reconciliation_polls_bounded_pending_lookup_to_completion():
    adapter = EmailAdapter(
        sender_domains={"nigel-zoho": "pfg.example"},
        reconciliation_poll_attempts=3,
    )
    unknown = ProviderObservation(
        ProviderDisposition.AMBIGUOUS,
        "prior_timeout",
    )
    client = FakeClient(
        pending("thread-lookup-1"),
        pending("thread-lookup-1"),
        McpCallResult(
            structured_content={
                "status": "completed",
                "request_id": "thread-lookup-1",
                "result": {
                    "tool_name": "email_get_thread",
                    "structured_content": {
                        "data": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "**Thread:** exact deterministic message",
                                }
                            ]
                        }
                    },
                },
            }
        ),
    )

    reconciled = await adapter.reconcile(client, context(), ACTION_UID, unknown)

    assert reconciled.disposition is ProviderDisposition.ACCEPTED
    assert [call[1] for call in client.calls] == [
        "email_get_thread",
        "request_status",
        "request_status",
    ]


def test_email_adapter_applies_management_copy_only_to_configured_sources():
    adapter = EmailAdapter(
        sender_domains={"nigel-zoho": "pfg.io"},
        cc_by_source={"zillow": "management@pfg.io", "hotpads": "management@pfg.io"},
    )

    tenantcloud_request = adapter.build_request(
        context(source="tenantcloud"),
        ACTION_UID,
    )

    assert "cc" not in tenantcloud_request.arguments


@pytest.mark.asyncio
async def test_quo_adapter_requires_id_and_reconciles_exact_message_tuple():
    adapter = QuoSmsAdapter(user_id="user-1")
    request = adapter.build_request(context(Operation.QUO_SMS_SEND), ACTION_UID)
    assert request.arguments == {
        "phone_number_id": "leasing-line",
        "to": "+19085550199",
        "user_id": "user-1",
        "content": "Friday at 10:30 works. — Nigel",
    }
    client = FakeClient(
        McpCallResult(structured_content={"status": "sent", "message_id": "quo-message-1"}),
    )
    observation = await adapter.invoke(client, request)
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert adapter.parse_receipt(context(Operation.QUO_SMS_SEND), observation).provider_message_id == "quo-message-1"

    ambiguous = McpCallResult(error_kind=TransportErrorKind.TIMEOUT, is_error=True, safe_detail="transport_timeout")
    history = McpCallResult(
        structured_content={
            "messages": [
                {
                    "id": "quo-message-2",
                    "direction": "outgoing",
                    "to": "+19085550199",
                    "content": "Friday at 10:30 works. — Nigel",
                    "created_at": "2026-07-16T01:00:05Z",
                }
            ]
        }
    )
    retry_client = FakeClient(ambiguous, history)
    unknown = await adapter.invoke(retry_client, request)
    assert unknown.disposition is ProviderDisposition.AMBIGUOUS
    reconciled = await adapter.reconcile(retry_client, context(Operation.QUO_SMS_SEND), ACTION_UID, unknown)
    assert reconciled.disposition is ProviderDisposition.ACCEPTED
    assert reconciled.message_id == "quo-message-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "tool", "target_field"),
    [
        (Operation.CLIQ_CHANNEL_POST, "cliq_channel_bot_post", "channel_unique_name"),
        (Operation.CLIQ_CHAT_POST, "cliq_chat_post", "chat_id"),
    ],
)
async def test_cliq_adapter_builds_only_derived_destination(operation, tool, target_field):
    adapter = CliqAdapter(operation)
    ctx = context(operation)
    request = adapter.build_request(ctx, ACTION_UID)
    assert request.server_name == "agent-email"
    assert request.tool == tool
    assert request.arguments[target_field] == ctx.target.target_id
    assert request.arguments["text"] == ctx.arguments["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "tool"),
    [
        (Operation.CALENDAR_CREATE, "calendar_create_event"),
        (Operation.CALENDAR_UPDATE, "calendar_update_event"),
        (Operation.CALENDAR_DELETE, "calendar_delete_event"),
    ],
)
async def test_calendar_adapter_uses_deterministic_uid_and_exact_revision(operation, tool):
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(operation)
    request = adapter.build_request(ctx, ACTION_UID)
    assert request.server_name == "agent-email"
    assert request.tool == tool
    assert request.arguments["account_id"] == "nigel-zoho"
    assert request.arguments["calendar"] == "nigel"
    if operation is Operation.CALENDAR_CREATE:
        assert request.arguments["uid"] == str(ACTION_UID)
        assert request.arguments["location"] == "138 Bullman St #144-A"
        assert request.arguments["end"] == "2026-07-17T15:00:00Z"
    else:
        assert request.arguments["event_url"].endswith("existing-event.ics")
        assert request.arguments["etag"] == '"etag-1"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "tool", "provider_text", "provider_id"),
    [
        (
            Operation.CALENDAR_CREATE,
            "calendar_create_event",
            "**Event Created**\nUID: created-event\nURL: https://calendar.local/events/created-event.ics",
            "created-event",
        ),
        (
            Operation.CALENDAR_UPDATE,
            "calendar_update_event",
            "**Event Updated**\nUID: existing-event",
            "existing-event",
        ),
        (
            Operation.CALENDAR_DELETE,
            "calendar_delete_event",
            "**Event Deleted**\nURL: https://calendar.local/events/existing-event.ics",
            "https://calendar.local/events/existing-event.ics",
        ),
    ],
)
async def test_calendar_adapter_parses_agent_email_request_status_result(
    operation,
    tool,
    provider_text,
    provider_id,
):
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(operation)
    client = FakeClient(
        pending(),
        completed(
            tool,
            {
                "status": "success",
                "data": {"content": [{"type": "text", "text": provider_text}]},
            },
        ),
    )

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    assert observation.disposition is ProviderDisposition.PENDING
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == provider_id
    assert observation.provider_request_ref == "req-1"


@pytest.mark.asyncio
async def test_calendar_adapter_accepts_structured_event_object_from_async_job_result():
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_CREATE)
    event = {
        "id": "created-event",
        "uid": "7ea7586e-64d1-4c7b-9c1a-2b6a6f9d6a11",
        "summary": "Tour — Amanda Snyder",
        "start": "2026-07-17T14:30:00Z",
        "end": "2026-07-17T15:00:00Z",
        "all_day": False,
        "recurring": False,
        "calendar": "nigel",
        "description": "Tour",
        "location": "138 Bullman St #144-A",
        "event_url": "https://calendar.local/events/created-event.ics",
        "etag": "",
        "sequence": 0,
    }
    client = FakeClient(pending(), calendar_request_status_envelope(event))

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    assert observation.disposition is ProviderDisposition.PENDING
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == event["uid"]
    assert observation.provider_request_ref == "req-1"
    assert observation.evidence == {
        "kind": "calendar_uid",
        "calendar_event_uid": event["uid"],
        "event_url": event["event_url"],
    }


@pytest.mark.asyncio
async def test_calendar_adapter_stays_ambiguous_when_structured_payload_has_no_event_object():
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_CREATE)
    client = FakeClient(pending(), calendar_request_status_envelope(None))

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.AMBIGUOUS
    assert observation.detail_code == "malformed_provider_success"


@pytest.mark.asyncio
async def test_calendar_adapter_accepts_plain_text_fallback_with_real_newlines():
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_CREATE)
    client = FakeClient(
        pending(),
        completed(
            "calendar_create_event",
            {
                "status": "success",
                "data": {
                    "content": [
                        {
                            "type": "text",
                            "text": "**Event Created**\nUID: created-event\nURL: https://calendar.local/events/created-event.ics",
                        }
                    ]
                },
            },
        ),
    )

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "created-event"
    assert observation.evidence["event_url"] == "https://calendar.local/events/created-event.ics"


@pytest.mark.asyncio
async def test_calendar_adapter_accepts_delete_receipt_without_uid_object():
    """Verified live: `calendar_delete_event` returns `data.deleted` + `data.event_url`
    with no `uid` field at all, so the uid-seeking walk in _parse finds nothing.
    The adapter must still accept, deriving the uid from the URL basename."""
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_DELETE)
    event_url = "https://calendar.zoho.com/caldav/a0c35cb9ee5f4192a18ebbc05adfe4fa/events/3685a803-4c37-608c-a344-139060123c48.ics"
    client = FakeClient(
        pending(),
        completed(
            "calendar_delete_event",
            {"status": "success", "data": {"deleted": True, "event_url": event_url}},
        ),
    )

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    assert observation.disposition is ProviderDisposition.PENDING
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "3685a803-4c37-608c-a344-139060123c48"
    assert observation.evidence == {
        "kind": "calendar_uid",
        "calendar_event_uid": "3685a803-4c37-608c-a344-139060123c48",
        "event_url": event_url,
    }


@pytest.mark.asyncio
async def test_calendar_adapter_stays_ambiguous_when_delete_receipt_reports_not_deleted():
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_DELETE)
    client = FakeClient(
        pending(),
        completed(
            "calendar_delete_event",
            {
                "status": "success",
                "data": {
                    "deleted": False,
                    "event_url": "https://calendar.zoho.com/caldav/a0c35cb9ee5f4192a18ebbc05adfe4fa/events/3685a803-4c37-608c-a344-139060123c48.ics",
                },
            },
        ),
    )

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.AMBIGUOUS
    assert observation.detail_code == "malformed_provider_success"


@pytest.mark.asyncio
async def test_calendar_adapter_stays_ambiguous_when_delete_receipt_has_no_event_url():
    adapter = CalendarAdapter(account_by_calendar={"nigel": "nigel-zoho"})
    ctx = context(Operation.CALENDAR_DELETE)
    client = FakeClient(
        pending(),
        completed(
            "calendar_delete_event",
            {"status": "success", "data": {"deleted": True, "event_url": ""}},
        ),
    )

    observation = await adapter.invoke(client, adapter.build_request(ctx, ACTION_UID))
    observation = await adapter.poll(client, observation)

    assert observation.disposition is ProviderDisposition.AMBIGUOUS
    assert observation.detail_code == "malformed_provider_success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected", "detail"),
    [
        (McpCallResult(error_kind=TransportErrorKind.TIMEOUT, is_error=True), ProviderDisposition.AMBIGUOUS, "provider_timeout"),
        (McpCallResult(error_kind=TransportErrorKind.CONNECTION_LOST, is_error=True), ProviderDisposition.AMBIGUOUS, "provider_connection_lost"),
        (
            McpCallResult(structured_content={"status": "failed", "category": "auth_error", "retryable": False}),
            ProviderDisposition.AMBIGUOUS,
            "provider_auth_error",
        ),
        (
            McpCallResult(structured_content={"status": "failed", "category": "transient_upstream_error", "retryable": True}),
            ProviderDisposition.AMBIGUOUS,
            "provider_transient_upstream_error",
        ),
        (
            McpCallResult(
                structured_content={
                    "status": "completed",
                    "completed_result": {"tool_name": "email_send", "structured_content": {"status": "success"}},
                }
            ),
            ProviderDisposition.AMBIGUOUS,
            "malformed_provider_success",
        ),
    ],
)
async def test_provider_failures_without_acceptance_proof_remain_ambiguous(result, expected, detail):
    adapter = EmailAdapter(sender_domains={"nigel-zoho": "pfg.example"})
    client = FakeClient(result)
    observation = await adapter.invoke(client, adapter.build_request(context(), ACTION_UID))
    assert observation.disposition is expected
    assert observation.detail_code == detail
    assert observation.disposition is not ProviderDisposition.ACCEPTED


# ---------------------------------------------------------------------------
# TenantCloud adapter
#
# The shared TenantCloudMutations facade (a different repo's module) exposes
# named, readback-verified methods. The fake below mirrors its exact method
# signatures and result shapes (MutationExecution/MutationObservation/
# ReconciliationResult) via plain attribute-only fakes so this adapter never
# imports across repos -- see scripts/tenantcloud_mutations.py in
# Comm-Data-Store for the authoritative shapes.


class FakeDisposition:
    def __init__(self, value: str):
        self.value = value


TC_ACCEPTED = FakeDisposition("accepted")
TC_DEFINITIVE_NON_ACCEPTANCE = FakeDisposition("definitive_non_acceptance")
TC_UNKNOWN = FakeDisposition("unknown")


class FakeAudit:
    def __init__(self, error_code=None):
        self.error_code = error_code


class FakeMutationResult:
    def __init__(self, disposition, error_code=None):
        self.disposition = disposition
        self.audit = FakeAudit(error_code)


class FakeMutationObservation:
    def __init__(
        self,
        *,
        target_reference,
        provider_object_id,
        canonical_observed_state,
        readback_timestamp="2026-07-16T01:00:00Z",
        evidence_hash="e" * 64,
        readback_verified=True,
    ):
        self.target_reference = target_reference
        self.provider_object_id = provider_object_id
        self.canonical_observed_state = canonical_observed_state
        self.readback_timestamp = readback_timestamp
        self.evidence_hash = evidence_hash
        self.readback_verified = readback_verified


class FakeMutationExecution:
    def __init__(self, mutation, observation, error_code):
        self.mutation = mutation
        self.observation = observation
        self.error_code = error_code

    @property
    def verified(self) -> bool:
        return self.observation is not None and self.observation.readback_verified


class FakeReconciliationResult:
    def __init__(self, disposition, observation, error_code):
        self.disposition = disposition
        self.observation = observation
        self.error_code = error_code


class FakeTenantCloudMutations:
    """Mirrors TenantCloudMutations' named write/reconcile methods only --
    the adapter must never need MutationOperation, which lives in the other
    repo and cannot be imported here."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.send_message_result = None
        self.mark_lead_working_result = None
        self.create_maintenance_request_result = None
        self.update_maintenance_status_result = None
        self.reconcile_message_result = FakeReconciliationResult(TC_UNKNOWN, None, "no_match")
        self.reconcile_lead_status_result = FakeReconciliationResult(TC_DEFINITIVE_NON_ACCEPTANCE, None, "authoritative_absence")
        self.reconcile_maintenance_create_result = FakeReconciliationResult(TC_UNKNOWN, None, "no_match")
        self.reconcile_maintenance_status_result = FakeReconciliationResult(TC_DEFINITIVE_NON_ACCEPTANCE, None, "authoritative_absence")

    def send_message(self, thread_id, body):
        self.calls.append(("send_message", (thread_id, body), {}))
        return self.send_message_result

    def mark_lead_working(self, lead_id):
        self.calls.append(("mark_lead_working", (lead_id,), {}))
        return self.mark_lead_working_result

    def create_maintenance_request(self, **kwargs):
        self.calls.append(("create_maintenance_request", (), kwargs))
        return self.create_maintenance_request_result

    def update_maintenance_status(self, request_id, status):
        self.calls.append(("update_maintenance_status", (request_id, status), {}))
        return self.update_maintenance_status_result

    def reconcile_message(self, thread_id, body, *, source_turn_at):
        self.calls.append(("reconcile_message", (thread_id, body), {"source_turn_at": source_turn_at}))
        return self.reconcile_message_result

    def reconcile_lead_status(self, lead_id):
        self.calls.append(("reconcile_lead_status", (lead_id,), {}))
        return self.reconcile_lead_status_result

    def reconcile_maintenance_create(self, *, dispatched_after, **kwargs):
        self.calls.append(("reconcile_maintenance_create", (), {"dispatched_after": dispatched_after, **kwargs}))
        return self.reconcile_maintenance_create_result

    def reconcile_maintenance_status(self, request_id, status):
        self.calls.append(("reconcile_maintenance_status", (request_id, status), {}))
        return self.reconcile_maintenance_status_result


def tenantcloud_context(operation: Operation, **overrides):
    role = ActionRole.PROSPECT_REPLY if operation is Operation.TENANTCLOUD_MESSAGE_SEND else ActionRole.PROVIDER_MUTATION
    intent = {
        Operation.TENANTCLOUD_MESSAGE_SEND: IntentKind.INQUIRY_REPLY,
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: IntentKind.TENANTCLOUD_LEAD_STATUS,
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: IntentKind.TENANTCLOUD_MAINTENANCE_CREATE,
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: IntentKind.TENANTCLOUD_MAINTENANCE_STATUS,
    }[operation]
    target = {
        Operation.TENANTCLOUD_MESSAGE_SEND: DerivedTarget("tenantcloud_thread", "555", True),
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: DerivedTarget("tenantcloud_lead", "6001", True),
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: DerivedTarget("tenantcloud_property_unit", "property:12:unit:34", True),
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: DerivedTarget("tenantcloud_maintenance_request", "81", True),
    }[operation]
    arguments = {
        Operation.TENANTCLOUD_MESSAGE_SEND: {"text": "Friday at 10:30 works. — Nigel"},
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: {"status": "working"},
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: {
            "category_id": 57,
            "title": "Kitchen leak",
            "priority": "normal",
            "initiated_at": "2026-08-04",
            "text": "Sink leaking under cabinet",
            "entry_allowed": False,
            "available_on": None,
        },
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: {"status": 2},
    }[operation]
    canonical_context = {"identity_version": "v1"}
    if operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
        canonical_context["provider_ids"] = {"property_id": "12", "unit_id": "34"}
    values = dict(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=role,
        operation=operation,
        intent_kind=intent,
        appointment_slot=None,
        arguments=MappingProxyType(arguments),
        source="tenantcloud",
        source_message_id=700,
        source_message_key="tenantcloud_api:700",
        source_sent_at=NOW,
        conversation_id="conversation:tenantcloud-1",
        conversation_watermark=700,
        prospect_id="tenantcloud:claim:301",
        aliases=(),
        property_id=None,
        property_label=None,
        target=target,
        provider_account="tenantcloud",
        routing_policy_version="v1",
        canonical_scope=MappingProxyType({"version": "v1"}),
        canonical_context=MappingProxyType(canonical_context),
        payload_hash="a" * 64,
        lock_holder=f"outbound-gateway:{ACTION_ID}",
        thread_identity="tenantcloud:lead-thread:555",
        showing_lifecycle_id="showing:wake:7",
        calendar_event_uid=None,
    )
    values.update(overrides)
    return ActionContext(**values)


@pytest.mark.asyncio
async def test_tenantcloud_message_send_performs_one_write_and_verified_readback():
    facade = FakeTenantCloudMutations()
    facade.send_message_result = FakeMutationExecution(
        FakeMutationResult(TC_ACCEPTED),
        FakeMutationObservation(
            target_reference="thread:555",
            provider_object_id="9001",
            canonical_observed_state={"thread_id": "555", "body": "Friday at 10:30 works. — Nigel"},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MESSAGE_SEND)
    request = adapter.build_request(ctx, ACTION_UID)

    observation = await adapter.invoke(facade, request)

    assert [call[0] for call in facade.calls] == ["reconcile_message", "send_message"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-message:9001"
    assert observation.provider_request_ref == "thread:555"
    # Exactly migration 118's six required keys (118_...sql:353-364) plus the
    # facade's own opaque evidence_hash the store must peel off separately.
    assert set(observation.evidence) == {
        "canonical_observed_state", "operation", "provider_object_id",
        "target_reference", "readback_timestamp", "readback_verified", "evidence_hash",
    }
    assert observation.evidence["canonical_observed_state"] == {"thread_id": "555", "body": "Friday at 10:30 works. — Nigel"}
    assert observation.evidence["operation"] == "tenantcloud.message.send"
    assert observation.evidence["provider_object_id"] == "9001"
    assert observation.evidence["target_reference"] == "thread:555"
    assert observation.evidence["readback_verified"] is True
    assert observation.evidence["evidence_hash"] == "e" * 64
    receipt = adapter.parse_receipt(ctx, observation)
    assert receipt is not None
    assert receipt.provider_message_id == "tenantcloud-message:9001"


@pytest.mark.asyncio
async def test_tenantcloud_message_send_already_present_skips_post():
    facade = FakeTenantCloudMutations()
    facade.reconcile_message_result = FakeReconciliationResult(
        TC_ACCEPTED,
        FakeMutationObservation(
            target_reference="thread:555",
            provider_object_id="9001",
            canonical_observed_state={"thread_id": "555", "body": "Friday at 10:30 works. — Nigel"},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MESSAGE_SEND)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert [call[0] for call in facade.calls] == ["reconcile_message"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-message:9001"


@pytest.mark.asyncio
async def test_tenantcloud_lead_status_already_working_skips_patch():
    facade = FakeTenantCloudMutations()
    facade.reconcile_lead_status_result = FakeReconciliationResult(
        TC_ACCEPTED,
        FakeMutationObservation(
            target_reference="lead:6001",
            provider_object_id="6001",
            canonical_observed_state={"status": "working"},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert [call[0] for call in facade.calls] == ["reconcile_lead_status"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-lead:6001:working"


@pytest.mark.asyncio
async def test_tenantcloud_lead_status_writes_when_not_yet_applied():
    facade = FakeTenantCloudMutations()
    facade.mark_lead_working_result = FakeMutationExecution(
        FakeMutationResult(TC_ACCEPTED),
        FakeMutationObservation(
            target_reference="lead:6001",
            provider_object_id="6001",
            canonical_observed_state={"status": "working"},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert [call[0] for call in facade.calls] == ["reconcile_lead_status", "mark_lead_working"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-lead:6001:working"


@pytest.mark.asyncio
async def test_tenantcloud_maintenance_create_already_present_skips_post():
    facade = FakeTenantCloudMutations()
    facade.reconcile_maintenance_create_result = FakeReconciliationResult(
        TC_ACCEPTED,
        FakeMutationObservation(
            target_reference="maintenance_request:4200",
            provider_object_id="4200",
            canonical_observed_state={"status": 1},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MAINTENANCE_CREATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert [call[0] for call in facade.calls] == ["reconcile_maintenance_create"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-maintenance:4200"


@pytest.mark.asyncio
async def test_tenantcloud_maintenance_create_evidence_target_reference_is_stable_property_unit():
    # The eventual maintenance_request id does not exist at enqueue time, so
    # migration 118's arguments->>'target_reference' comparison (118_...sql:370)
    # can only be satisfied by a pre-write-knowable value -- the stable
    # property:unit identifier, not the facade's own per-write
    # "maintenance_request:<new id>" reference. provider_request_ref (used as
    # evidence_reference, which the DB requires to equal it, 118_...sql:351)
    # still uses the facade's own reference.
    facade = FakeTenantCloudMutations()
    facade.create_maintenance_request_result = FakeMutationExecution(
        FakeMutationResult(TC_ACCEPTED),
        FakeMutationObservation(
            target_reference="maintenance_request:4200",
            provider_object_id="4200",
            canonical_observed_state={"status": 1},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MAINTENANCE_CREATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert observation.evidence["target_reference"] == "property:12:unit:34"
    assert observation.provider_request_ref == "maintenance_request:4200"


@pytest.mark.asyncio
async def test_tenantcloud_maintenance_status_update_writes_and_verifies():
    facade = FakeTenantCloudMutations()
    facade.update_maintenance_status_result = FakeMutationExecution(
        FakeMutationResult(TC_ACCEPTED),
        FakeMutationObservation(
            target_reference="maintenance_request:81",
            provider_object_id="81",
            canonical_observed_state={"status": 2},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert [call[0] for call in facade.calls] == ["reconcile_maintenance_status", "update_maintenance_status"]
    assert observation.disposition is ProviderDisposition.ACCEPTED
    assert observation.message_id == "tenantcloud-maintenance:81"


@pytest.mark.asyncio
async def test_tenantcloud_auth_rejection_before_dispatch_is_retryable_non_acceptance():
    facade = FakeTenantCloudMutations()
    facade.mark_lead_working_result = FakeMutationExecution(
        FakeMutationResult(TC_DEFINITIVE_NON_ACCEPTANCE, "authentication_unavailable"),
        None,
        "authentication_unavailable",
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert observation.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE
    assert observation.retryable is True
    assert observation.category == "provider_authentication"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation_result",
    [
        FakeMutationResult(TC_UNKNOWN, "authentication_rejected"),
        FakeMutationResult(TC_UNKNOWN, "transport_error"),
    ],
)
async def test_tenantcloud_auth_rejection_timeout_or_connection_loss_after_dispatch_is_ambiguous(mutation_result):
    facade = FakeTenantCloudMutations()
    facade.mark_lead_working_result = FakeMutationExecution(mutation_result, None, mutation_result.audit.error_code)
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert observation.disposition is ProviderDisposition.AMBIGUOUS
    assert observation.disposition is not ProviderDisposition.ACCEPTED


@pytest.mark.asyncio
async def test_tenantcloud_accepted_write_without_verified_readback_stays_ambiguous():
    facade = FakeTenantCloudMutations()
    facade.send_message_result = FakeMutationExecution(
        FakeMutationResult(TC_ACCEPTED),
        None,
        "readback_mismatch",
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MESSAGE_SEND)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert observation.disposition is ProviderDisposition.AMBIGUOUS
    assert observation.disposition is not ProviderDisposition.ACCEPTED


@pytest.mark.asyncio
async def test_tenantcloud_message_reconciliation_uses_bounded_exact_tuple_search_never_blind_post():
    facade = FakeTenantCloudMutations()
    facade.reconcile_message_result = FakeReconciliationResult(
        TC_ACCEPTED,
        FakeMutationObservation(
            target_reference="thread:555",
            provider_object_id="9001",
            canonical_observed_state={"thread_id": "555", "body": "Friday at 10:30 works. — Nigel"},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MESSAGE_SEND)
    prior = ProviderObservation(ProviderDisposition.AMBIGUOUS, "tenantcloud_write_ambiguous_transport_error")

    reconciled = await adapter.reconcile(facade, ctx, ACTION_UID, prior)

    assert [call[0] for call in facade.calls] == ["reconcile_message"]
    call = facade.calls[0]
    assert call[1] == ("555", "Friday at 10:30 works. — Nigel")
    assert call[2]["source_turn_at"] == NOW
    assert reconciled.disposition is ProviderDisposition.ACCEPTED
    assert reconciled.message_id == "tenantcloud-message:9001"


@pytest.mark.asyncio
async def test_tenantcloud_maintenance_create_reconciliation_uses_bounded_full_tuple_search_never_blind_post():
    facade = FakeTenantCloudMutations()
    facade.reconcile_maintenance_create_result = FakeReconciliationResult(
        TC_ACCEPTED,
        FakeMutationObservation(
            target_reference="maintenance_request:4200",
            provider_object_id="4200",
            canonical_observed_state={"status": 1},
        ),
        None,
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MAINTENANCE_CREATE)
    prior = ProviderObservation(ProviderDisposition.AMBIGUOUS, "tenantcloud_write_ambiguous_transport_error")

    reconciled = await adapter.reconcile(facade, ctx, ACTION_UID, prior)

    assert [call[0] for call in facade.calls] == ["reconcile_maintenance_create"]
    call = facade.calls[0]
    assert call[2]["dispatched_after"] == NOW
    assert call[2]["property_id"] == "12"
    assert call[2]["unit_id"] == "34"
    assert call[2]["category_id"] == 57
    assert reconciled.disposition is ProviderDisposition.ACCEPTED
    assert reconciled.message_id == "tenantcloud-maintenance:4200"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["no_match", "ambiguous_match"])
async def test_tenantcloud_reconciliation_with_zero_or_multiple_matches_remains_unknown(error_code):
    facade = FakeTenantCloudMutations()
    facade.reconcile_message_result = FakeReconciliationResult(TC_UNKNOWN, None, error_code)
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_MESSAGE_SEND)
    prior = ProviderObservation(ProviderDisposition.AMBIGUOUS, "tenantcloud_write_ambiguous_transport_error")

    reconciled = await adapter.reconcile(facade, ctx, ACTION_UID, prior)

    assert reconciled.disposition is ProviderDisposition.AMBIGUOUS
    assert reconciled.disposition is not ProviderDisposition.ACCEPTED


@pytest.mark.asyncio
async def test_tenantcloud_status_reconciliation_retries_patch_only_after_authoritative_absence():
    facade = FakeTenantCloudMutations()
    facade.reconcile_lead_status_result = FakeReconciliationResult(
        TC_DEFINITIVE_NON_ACCEPTANCE, None, "authoritative_absence"
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)
    prior = ProviderObservation(ProviderDisposition.AMBIGUOUS, "tenantcloud_write_ambiguous_transport_error")

    reconciled = await adapter.reconcile(facade, ctx, ACTION_UID, prior)

    assert reconciled.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE
    assert reconciled.retryable is True


# ---------------------------------------------------------------------------
# FIX 2: a provably pre-dispatch failure must retry, not escalate.
#
# _from_execution already classifies an invoke()-time authentication_unavailable
# rejection as DEFINITIVE_NON_ACCEPTANCE+retryable=True (proven above by
# test_tenantcloud_auth_rejection_before_dispatch_is_retryable_non_acceptance)
# -- nothing could have been written if the provider never even accepted the
# write attempt. _from_reconciliation only ever produced that same
# provably-safe-to-retry classification for the two *status* operations
# (via definitive_absence_detail, itself really meaning "not yet applied" --
# a different but also-retryable reason); for the two *create* operations
# (message.send, maintenance.create) definitive_absence_detail is None, so
# ANY "definitive_non_acceptance" reconciliation result -- including one
# whose error_code is authentication_unavailable, the exact same signal
# _from_execution already trusts -- fell through to the generic ambiguous
# bucket below. That is "the ambiguous-create reconcile path": a reconcile
# call that could not even authenticate proves nothing was written (you
# cannot write with less authentication than a read requires), so it should
# retry exactly like the invoke()-time case, not escalate toward
# reconcile/manual_review.


@pytest.mark.asyncio
async def test_tenantcloud_provider_rejected_non_acceptance_is_not_retryable():
    """PIN (unchanged): a definitive provider rejection that is NOT the
    authentication_unavailable signal (e.g. the provider validated the
    request and rejected it on its merits) must stay non-retryable --
    only authentication_unavailable is provably pre-dispatch."""
    facade = FakeTenantCloudMutations()
    facade.mark_lead_working_result = FakeMutationExecution(
        FakeMutationResult(TC_DEFINITIVE_NON_ACCEPTANCE, "validation_rejected"),
        None,
        "validation_rejected",
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(Operation.TENANTCLOUD_LEAD_STATUS_UPDATE)

    observation = await adapter.invoke(facade, adapter.build_request(ctx, ACTION_UID))

    assert observation.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE
    assert observation.retryable is False
    assert observation.detail_code == "tenantcloud_provider_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "reconcile_method"),
    [
        (Operation.TENANTCLOUD_MESSAGE_SEND, "reconcile_message_result"),
        (Operation.TENANTCLOUD_MAINTENANCE_CREATE, "reconcile_maintenance_create_result"),
    ],
)
async def test_tenantcloud_reconciliation_auth_unavailable_on_a_create_is_retryable_not_ambiguous(operation, reconcile_method):
    """NEW: closes the ambiguous-create reconcile path gap described above.
    Before the fix, this produced AMBIGUOUS "tenantcloud_reconciliation_
    authentication_unavailable" for these two operations -- the same
    detail-code family as the live incident's
    tenantcloud_reconciliation_no_match, just for a provably-safe-to-retry
    reason instead of a genuinely inconclusive one."""
    facade = FakeTenantCloudMutations()
    setattr(
        facade,
        reconcile_method,
        FakeReconciliationResult(TC_DEFINITIVE_NON_ACCEPTANCE, None, "authentication_unavailable"),
    )
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    ctx = tenantcloud_context(operation)
    prior = ProviderObservation(ProviderDisposition.AMBIGUOUS, "tenantcloud_write_ambiguous_transport_error")

    reconciled = await adapter.reconcile(facade, ctx, ACTION_UID, prior)

    assert reconciled.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE
    assert reconciled.retryable is True
    assert reconciled.category == "provider_authentication"
    assert reconciled.detail_code == "tenantcloud_auth_rejected_before_dispatch"
