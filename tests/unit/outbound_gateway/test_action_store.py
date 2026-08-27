from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from hashlib import sha256
from types import MappingProxyType
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID

import pytest

from postgres_mcp.outbound_gateway.adapters.base import ProviderDisposition
from postgres_mcp.outbound_gateway.adapters.base import ProviderObservation
from postgres_mcp.outbound_gateway.context import ActionContext
from postgres_mcp.outbound_gateway.context import DerivedTarget
from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import ActionState
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.store import PostgresActionStore

ACTION_ID = UUID("4cbac369-48c6-5b62-95e9-41f50259e732")
ACTION_UID = UUID("9ebddbf7-8fc8-5a4f-bba7-869ea7053521")
NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def action_row(state="received"):
    return {
        "action_id": ACTION_ID,
        "wakeup_event_id": 7,
        "action_role": "prospect_reply",
        "operation": "email.send",
        "intent_kind": "showing_offer",
        "appointment_slot": datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        "arguments": {"text": "hello"},
        "state": state,
        "action_uid": ACTION_UID if state != "received" else None,
        "provider_request_ref": None,
        "provider_message_id": None,
        "completion_kind": None,
        "detail_code": state,
        "attempt_count": 0,
        "next_attempt_at": NOW,
        "payload_hash": "a" * 64,
        "canonical_context": {"identity_version": "v1"},
        "canonical_scope": {"version": "v1"},
        "recipient_scope": {
            "kind": "email_thread",
            "target_id": "lead@convo.zillow.com",
            "verified": True,
        },
        "provider_account": "nigel-zoho",
        "routing_policy_version": "v1",
    }


def context():
    return ActionContext(
        action_id=ACTION_ID,
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
        lock_holder=f"outbound-gateway:{ACTION_ID}",
        thread_identity="zrm-thread-1",
        showing_lifecycle_id="showing:7",
        calendar_event_uid=None,
    )


def provider_context(operation, arguments, target, *, claim_id=301, source_event_id="tenantcloud:claim:301", desired_hash="d" * 64):
    intent = {
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: IntentKind.TENANTCLOUD_LEAD_STATUS,
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: IntentKind.TENANTCLOUD_MAINTENANCE_CREATE,
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: IntentKind.TENANTCLOUD_MAINTENANCE_STATUS,
    }[operation]
    return replace(
        context(),
        action_role=ActionRole.PROVIDER_MUTATION,
        operation=operation,
        intent_kind=intent,
        appointment_slot=None,
        arguments=MappingProxyType(arguments),
        target=target,
        provider_account="tenantcloud",
        canonical_context=MappingProxyType(
            {
                "identity_version": "v1",
                "tenantcloud_claim_id": claim_id,
                "source_event_id": source_event_id,
                "operation_target": {"kind": target.kind, "target_id": target.target_id},
                **({"provider_ids": {"property_id": "12", "unit_id": "34"}} if operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE else {}),
            }
        ),
        canonical_scope=MappingProxyType({"version": "v1", "desired_state_hash": desired_hash}),
    )


class Row:
    def __init__(self, cells):
        self.cells = cells


@pytest.mark.asyncio
async def test_store_uses_database_functions_and_sanitized_observations():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        state = "dispatching" if "record_outbound_provider_request" in query else "received"
        return [Row(action_row(state))]

    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        created = await store.create_or_load(context())
        recorded = await store.record_provider_request(
            ACTION_ID,
            "worker-1",
            ProviderObservation(
                ProviderDisposition.PENDING,
                "provider_pending",
                provider_request_ref="req-1",
                provider_call_id="call-1",
                evidence={"secret": "must-not-persist"},
            ),
        )

    assert created.state is ActionState.RECEIVED
    assert recorded.state is ActionState.DISPATCHING
    assert "create_or_load_outbound_action" in calls[0][0]
    assert "record_outbound_provider_request" in calls[1][0]
    assert "must-not-persist" not in str(calls[1][1])
    assert calls[1][1][-1] == '{"detail_code":"provider_pending","disposition":"pending"}'


@pytest.mark.asyncio
async def test_store_work_query_includes_expired_dispatch_without_unlocking_it():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row({"action_id": ACTION_ID, "state": "dispatching"})]

    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        work = await store.list_work(20, 5)

    assert work == [(ACTION_ID, ActionState.DISPATCHING)]
    assert "lease_expires_at <= now()" in calls[0][0]
    assert "next_attempt_at <= now()" in calls[0][0]
    assert "attempt_count <" in calls[0][0]
    assert "FOR UPDATE SKIP LOCKED" not in calls[0][0]
    assert calls[0][1] == [5, 20]


@pytest.mark.asyncio
async def test_store_schedules_bounded_next_attempt_through_database_function():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("unknown"))]

    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        scheduled = await store.schedule_next_attempt(
            ACTION_ID,
            ActionState.UNKNOWN,
            120,
            "provider_timeout",
        )

    assert scheduled.state is ActionState.UNKNOWN
    assert "schedule_outbound_action_attempt" in calls[0][0]
    assert calls[0][1] == [ACTION_ID, "unknown", 120, "provider_timeout"]


@pytest.mark.asyncio
async def test_prospect_lock_scope_includes_canonical_source_turn():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("prepared"))]

    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await store.prepare(context(), ActionState.RECEIVED)

    assert "prepare_outbound_action_and_acquire_lock" in calls[0][0]
    assert calls[0][1][4] == "showing_offer:turn:700"


async def prepared_lock_intent(subject):
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("prepared"))]

    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await PostgresActionStore(object()).prepare(subject, ActionState.RECEIVED)
    return calls[0][1][4]


@pytest.mark.asyncio
async def test_provider_status_lock_has_versioned_claim_source_target_and_desired_state():
    subject = provider_context(
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        {"status": "working"},
        DerivedTarget("tenantcloud_lead", "6001", True),
    )

    lock_intent = await prepared_lock_intent(subject)

    assert lock_intent == ("v1:claim:301:source:tenantcloud:claim:301:op:tenantcloud.lead.status.update:target:6001:state:" + "d" * 64)


@pytest.mark.asyncio
async def test_provider_status_lock_omits_claim_segment_without_a_literal_none_when_claim_is_absent():
    """context.py stores "" (not None) for tenantcloud_claim_id when a wake
    has no TenantCloud claim linkage -- confirm prepare() consumes that
    shape without baking the literal string "None" into the persisted,
    immutable lock_intent identity string."""
    subject = provider_context(
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        {"status": "working"},
        DerivedTarget("tenantcloud_lead", "6001", True),
        claim_id="",
        source_event_id="tenantcloud:claim:unset",
    )

    lock_intent = await prepared_lock_intent(subject)

    assert "None" not in lock_intent
    assert lock_intent == ("v1:claim::source:tenantcloud:claim:unset:op:tenantcloud.lead.status.update:target:6001:state:" + "d" * 64)


@pytest.mark.asyncio
async def test_maintenance_create_lock_uses_explicit_boundaries_and_hashes_text():
    text = "Café\nPipe"
    subject = provider_context(
        Operation.TENANTCLOUD_MAINTENANCE_CREATE,
        {
            "category_id": 57,
            "title": "Kitchen leak",
            "priority": "normal",
            "initiated_at": "2026-08-04",
            "text": text,
            "entry_allowed": False,
            "available_on": "2026-08-05",
        },
        DerivedTarget("tenantcloud_property_unit", "property:12:unit:34", True),
    )

    lock_intent = await prepared_lock_intent(subject)

    assert lock_intent == (
        "v1:claim:301:source:tenantcloud:claim:301:"
        "op:tenantcloud.maintenance.create:target:property:12:unit:34:"
        "property:12:unit:34:category:57:initiated:2026-08-04:text:"
        f"{sha256(text.encode('utf-8')).hexdigest()}:state:{'d' * 64}"
    )
    assert text not in lock_intent
    assert "Café" not in lock_intent


@pytest.mark.asyncio
async def test_provider_lock_changes_for_desired_state_and_distinct_wake_identity():
    base = provider_context(
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE,
        {"status": 2},
        DerivedTarget("tenantcloud_maintenance_request", "81", True),
    )
    changed_state = replace(
        base,
        arguments=MappingProxyType({"status": 3}),
        canonical_scope=MappingProxyType({"version": "v1", "desired_state_hash": "e" * 64}),
    )
    distinct_wake = replace(
        base,
        canonical_context=MappingProxyType(
            {
                **base.canonical_context,
                "tenantcloud_claim_id": 302,
                "source_event_id": "tenantcloud:claim:302",
            }
        ),
    )

    base_key = await prepared_lock_intent(base)
    same_key = await prepared_lock_intent(replace(base, action_id=UUID("f255a04a-93df-4e55-afd7-da866f992111")))
    changed_key = await prepared_lock_intent(changed_state)
    distinct_key = await prepared_lock_intent(distinct_wake)

    assert same_key == base_key
    assert changed_key != base_key
    assert distinct_key != base_key


@pytest.mark.asyncio
async def test_tenantcloud_message_keeps_existing_reply_lock_identity():
    subject = replace(
        context(),
        operation=Operation.TENANTCLOUD_MESSAGE_SEND,
        intent_kind=IntentKind.INQUIRY_REPLY,
        appointment_slot=None,
        arguments=MappingProxyType({"text": "Reply"}),
    )

    assert await prepared_lock_intent(subject) == "inquiry_reply:turn:700"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "operation", "intent", "expected"),
    [
        (
            ActionRole.CALENDAR_MUTATION,
            Operation.CALENDAR_CREATE,
            IntentKind.SHOWING_CREATE,
            "showing_create:lifecycle:showing:7",
        ),
        (
            ActionRole.INTERNAL_NOTIFICATION,
            Operation.CLIQ_CHANNEL_POST,
            IntentKind.LEAD_ALERT,
            "lead_alert:event:7",
        ),
    ],
)
async def test_existing_nonreply_lock_keys_are_exact(role, operation, intent, expected):
    subject = replace(context(), action_role=role, operation=operation, intent_kind=intent)

    assert await prepared_lock_intent(subject) == expected


# Exactly migration 118's required six keys for p_observation
# (118_...sql:353-364), plus the facade's own opaque evidence_hash as a
# seventh key the adapter attaches and the store must strip before sending
# p_observation (see tenantcloud_shared.READBACK_OBSERVATION_KEYS).
ADAPTER_EVIDENCE = {
    "canonical_observed_state": {"status": "working"},
    "operation": "tenantcloud.lead.status.update",
    "provider_object_id": "6001",
    "target_reference": "lead:6001",
    "readback_timestamp": "2026-07-16T01:00:00Z",
    "readback_verified": True,
    "evidence_hash": "e" * 64,
}
SIX_KEY_OBSERVATION = {key: value for key, value in ADAPTER_EVIDENCE.items() if key != "evidence_hash"}


def _tenantcloud_accepted_row(state="provider_accepted"):
    # transition_outbound_action RETURNS SETOF outbound_actions, which gets
    # no new evidence columns from migration 118 -- the evidence lives on
    # outbound_action_attempts (067) and must be fetched separately.
    return {**action_row(state), "operation": "tenantcloud.lead.status.update"}


@pytest.mark.asyncio
async def test_transition_to_provider_accepted_sends_exact_six_key_observation_and_literal_evidence_kind():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        if "outbound_action_attempts" in query:
            return [
                Row(
                    {
                        "evidence_kind": "verified_provider_readback",
                        "evidence_reference": "lead:6001",
                        "evidence_hash": "e" * 64,
                        "provider_observation": SIX_KEY_OBSERVATION,
                    }
                )
            ]
        return [Row(_tenantcloud_accepted_row())]

    store = PostgresActionStore(object())
    observation = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_accepted",
        provider_request_ref="lead:6001",
        message_id="tenantcloud-lead:6001:working",
        accepted_at=None,
        evidence=ADAPTER_EVIDENCE,
    )
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        result = await store.transition(
            ACTION_ID,
            ActionState.DISPATCHING,
            ActionState.PROVIDER_ACCEPTED,
            "worker-1",
            observation,
        )

    transition_call = next(call for call in calls if "transition_outbound_action" in call[0])
    params = transition_call[1]
    sent_observation = json.loads(params[8])
    assert sent_observation == SIX_KEY_OBSERVATION
    assert set(sent_observation) == {
        "canonical_observed_state", "operation", "provider_object_id",
        "target_reference", "readback_timestamp", "readback_verified",
    }
    assert "evidence_hash" not in sent_observation
    assert params[9] == "verified_provider_readback"
    assert params[10] == "lead:6001"
    assert params[10] == observation.provider_request_ref  # evidence_reference == provider_request_ref (118:351)
    assert params[11] == "e" * 64

    attempts_call = next(call for call in calls if "outbound_action_attempts" in call[0])
    assert attempts_call[1] == [ACTION_ID, result.attempt_count]

    assert result.provider_evidence_kind == "verified_provider_readback"
    assert result.provider_evidence_hash == "e" * 64
    assert result.provider_readback_evidence == SIX_KEY_OBSERVATION


@pytest.mark.asyncio
async def test_transition_to_provider_accepted_without_provider_request_ref_fails_explicitly():
    store = PostgresActionStore(object())
    observation = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_accepted",
        provider_request_ref=None,
        message_id="tenantcloud-lead:6001:working",
        accepted_at=None,
        evidence=ADAPTER_EVIDENCE,
    )
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=AssertionError("must not reach the database")),
    ):
        with pytest.raises(ValueError, match="provider_request_ref"):
            await store.transition(
                ACTION_ID,
                ActionState.DISPATCHING,
                ActionState.PROVIDER_ACCEPTED,
                "worker-1",
                observation,
            )


@pytest.mark.asyncio
async def test_create_or_load_persists_desired_state_target_reference_idempotency_key_for_tenantcloud():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("received"))]

    subject = provider_context(
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        {"status": "working"},
        DerivedTarget("tenantcloud_lead", "6001", True),
    )
    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await store.create_or_load(subject)

    persisted_arguments = json.loads(calls[0][1][-1])
    assert persisted_arguments["status"] == "working"
    assert persisted_arguments["desired_state"] == {"status": "working"}
    assert persisted_arguments["target_reference"] == "lead:6001"
    assert persisted_arguments["idempotency_key"] == (
        "v1:claim:301:source:tenantcloud:claim:301:op:tenantcloud.lead.status.update:"
        "target:lead:6001:state:" + "d" * 64
    )


@pytest.mark.asyncio
async def test_create_or_load_normalizes_desired_state_for_maintenance_create_with_available_on():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("received"))]

    subject = provider_context(
        Operation.TENANTCLOUD_MAINTENANCE_CREATE,
        {
            "category_id": 57,
            "title": "Kitchen leak",
            "priority": "normal",
            "initiated_at": "2026-08-04",
            "text": "Sink leaking under cabinet",
            "entry_allowed": False,
            "available_on": "2026-08-05",
        },
        DerivedTarget("tenantcloud_property_unit", "property:12:unit:34", True),
    )
    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await store.create_or_load(subject)

    persisted_arguments = json.loads(calls[0][1][-1])
    assert persisted_arguments["desired_state"] == {
        "property_id": "12",
        "unit_id": "34",
        "category_id": "57",
        "title": "Kitchen leak",
        "priority": "normal",
        "initiated_at": "08/04/2026",
        "text": "Sink leaking under cabinet",
        "entry_allowed": False,
        "status": 1,
        "available_on": "08/05/2026",
    }
    assert persisted_arguments["target_reference"] == "property:12:unit:34"


@pytest.mark.asyncio
async def test_create_or_load_leaves_non_tenantcloud_arguments_byte_for_byte():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("received"))]

    store = PostgresActionStore(object())
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        await store.create_or_load(context())

    persisted_arguments = json.loads(calls[0][1][-1])
    assert persisted_arguments == {"text": "hello"}


@pytest.mark.asyncio
async def test_transition_to_provider_accepted_without_readback_shape_leaves_evidence_columns_unset():
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        return [Row(action_row("provider_accepted"))]

    store = PostgresActionStore(object())
    observation = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "provider_accepted",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=None,
        evidence={"kind": "provider_message_id", "provider_message_id": "mail-1"},
    )
    with patch(
        "postgres_mcp.outbound_gateway.store.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        result = await store.transition(
            ACTION_ID,
            ActionState.DISPATCHING,
            ActionState.PROVIDER_ACCEPTED,
            "worker-1",
            observation,
        )

    params = calls[0][1]
    assert params[9] is None
    assert params[10] is None
    assert params[11] is None
    assert "provider_message_id" not in params[8]
    assert result.provider_evidence_kind is None
    assert result.provider_readback_evidence == {}
