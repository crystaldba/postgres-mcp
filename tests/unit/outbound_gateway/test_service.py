# pyright: reportArgumentType=false, reportOptionalMemberAccess=false

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import MappingProxyType
from unittest.mock import AsyncMock
from uuid import UUID
from uuid import uuid4

import pytest

from postgres_mcp.outbound_gateway.adapters.base import ProviderDisposition
from postgres_mcp.outbound_gateway.adapters.base import ProviderObservation
from postgres_mcp.outbound_gateway.adapters.base import ProviderReceipt
from postgres_mcp.outbound_gateway.context import ActionContext
from postgres_mcp.outbound_gateway.context import ContextDerivationError
from postgres_mcp.outbound_gateway.context import DerivedTarget
from postgres_mcp.outbound_gateway.context import canonical_payload_hash
from postgres_mcp.outbound_gateway.metrics import CircuitStatus
from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import ActionState
from postgres_mcp.outbound_gateway.models import CompletionKind
from postgres_mcp.outbound_gateway.models import ExecuteRequest
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.models import PublicStatus
from postgres_mcp.outbound_gateway.models import parse_outbound_request
from postgres_mcp.outbound_gateway.preflight import CalendarDependencyState
from postgres_mcp.outbound_gateway.preflight import PreflightEvidence
from postgres_mcp.outbound_gateway.service import OutboundActionRecord
from postgres_mcp.outbound_gateway.service import OutboundActionService
from postgres_mcp.outbound_gateway.tenantcloud_shared import TENANTCLOUD_OPERATIONS
from postgres_mcp.outbound_gateway.tenantcloud_shared import tenantcloud_persisted_arguments
from postgres_mcp.outbound_gateway.traffic_control import InFlightAction
from postgres_mcp.outbound_gateway.traffic_control import NewerActivity

ACTION_ID = UUID("4cbac369-48c6-5b62-95e9-41f50259e732")
ACTION_UID = UUID("9ebddbf7-8fc8-5a4f-bba7-869ea7053521")
NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def request(*, override: bool = False) -> ExecuteRequest:
    value = parse_outbound_request(
        {
            "op": "execute",
            "wakeup_event_id": 7,
            "action_role": "prospect_reply",
            "operation": "email.send",
            "intent_kind": "showing_offer",
            "appointment_slot": "2026-07-17T10:30:00-04:00",
            "arguments": {"to_address": "lead@convo.zillow.com", "text": "Friday at 10:30 works. — Nigel"},
            "override": override,
        }
    )
    assert isinstance(value, ExecuteRequest)
    return value


def context() -> ActionContext:
    return ActionContext(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=ActionRole.PROSPECT_REPLY,
        operation=Operation.EMAIL_SEND,
        intent_kind=IntentKind.SHOWING_OFFER,
        appointment_slot=datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        arguments=MappingProxyType({"to_address": "lead@convo.zillow.com", "text": "Friday at 10:30 works. — Nigel"}),
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
        source_subject="Zillow inquiry",
        prospect_name="Amanda Snyder",
    )


def evidence(**overrides):
    values = dict(
        current_recipient_id="lead@convo.zillow.com",
        current_property_id="building:bullman-st",
        current_appointment_slot=datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        later_inbound_message_id=None,
        verified_outbound_message_id=None,
        verified_outbound_request_ref=None,
        verified_outbound_covers_source=False,
        calendar_dependency=CalendarDependencyState.NOT_REQUIRED,
        calendar_already_applied=False,
        calendar_context_changed=False,
        overlapping_showing_prospect_ids=("prospect:other-1", "prospect:other-2"),
        refresh_required_through=NOW,
        refresh=None,
    )
    values.update(overrides)
    return PreflightEvidence(**values)


def row(state=ActionState.RECEIVED, **overrides):
    values = dict(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=ActionRole.PROSPECT_REPLY,
        operation=Operation.EMAIL_SEND,
        intent_kind=IntentKind.SHOWING_OFFER,
        appointment_slot=datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        arguments={"to_address": "lead@convo.zillow.com", "text": "Friday at 10:30 works. — Nigel"},
        state=state,
        action_uid=ACTION_UID if state is not ActionState.RECEIVED else None,
        provider_request_ref=None,
        provider_message_id=None,
        provider_accepted_at=None,
        completion_kind=None,
        detail_code=state.value,
        attempt_count=0,
        next_attempt_at=NOW,
        payload_hash="",
        canonical_context={},
        canonical_scope={},
        recipient_scope={},
        provider_account="",
        routing_policy_version="",
    )
    values.update(overrides)
    return OutboundActionRecord(**values)


class LeaseUnavailableError(RuntimeError):
    """Mirrors claim_outbound_action's 55P03 'outbound action lease
    unavailable' -- raised by the real SQL function (Comm-Data-Store
    migrations/068_outbound_gateway_observability.sql:156-167) when the
    expected_state isn't in its live whitelist."""


# The exact live whitelist claim_outbound_action enforces today
# (migrations/068_outbound_gateway_observability.sql:156-159) -- 'received'
# is deliberately excluded: a fresh row must go through
# prepare_outbound_action_and_acquire_lock first.
_CLAIMABLE_STATES = {
    ActionState.DEPENDENCY_WAIT,
    ActionState.PREPARED,
    ActionState.DISPATCHING,
    ActionState.PROVIDER_ACCEPTED,
    ActionState.UNKNOWN,
    ActionState.RECONCILING,
    ActionState.RETRY_READY,
    ActionState.DEAD_LETTER,
}


class InvalidDefinitiveFailStateError(RuntimeError):
    """Mirrors definitively_fail_outbound_action's 'invalid outbound
    definitive failure state' (Comm-Data-Store migrations/
    067_outbound_action_gateway.sql:898-902), raised when
    outbound_action_transition_allowed(expected_state, 'definitive_failed')
    is false."""


# outbound_action_transition_allowed()'s full table (067:346-389) has these
# and only these edges into 'definitive_failed' -- notably no
# ('dependency_wait', 'definitive_failed') or ('received', 'definitive_failed').
_DEFINITIVE_FAIL_ALLOWED_FROM = {
    ActionState.PREPARED,
    ActionState.DISPATCHING,
    ActionState.RETRY_READY,
    ActionState.MANUAL_REVIEW,
}


class FakeStore:
    def __init__(self, initial=None, *, remediation_successor_id=None, remediation_error=None):
        self.current = initial
        self.calls = []
        self.last_receipt = None
        self.remediation_successor_id = remediation_successor_id
        self.remediation_error = remediation_error

    async def create_or_load(self, ctx):
        self.calls.append(("create", ctx.action_id))
        if self.current is None:
            self.current = row()
        return self.current

    async def prepare(self, ctx, expected_state):
        self.calls.append(("prepare", expected_state))
        self.current = replace(self.current, state=ActionState.PREPARED, action_uid=ACTION_UID)
        return self.current

    async def claim(self, action_id, expected_state, lease_owner, lease_seconds):
        self.calls.append(("claim", expected_state, lease_owner))
        if expected_state not in _CLAIMABLE_STATES:
            raise LeaseUnavailableError(
                f"outbound action lease unavailable: state {expected_state.value!r} is not "
                "claimable (migrations/068_outbound_gateway_observability.sql:156-159)"
            )
        return self.current

    async def record_provider_request(self, action_id, lease_owner, observation):
        self.calls.append(("record_request", observation.provider_request_ref))
        self.current = replace(self.current, provider_request_ref=observation.provider_request_ref)
        return self.current

    async def transition(self, action_id, expected_state, next_state, lease_owner, observation):
        self.calls.append(("transition", expected_state, next_state, observation.detail_code, lease_owner))
        self.current = replace(
            self.current,
            state=next_state,
            provider_request_ref=observation.provider_request_ref or self.current.provider_request_ref,
            provider_message_id=observation.message_id or self.current.provider_message_id,
            detail_code=observation.detail_code,
        )
        return self.current

    async def complete(self, action_id, expected_state, lease_owner, receipt, completion_kind, detail_code):
        self.calls.append(("complete", expected_state, receipt.provider_request_ref))
        self.last_receipt = receipt
        self.current = replace(
            self.current,
            state=ActionState.COMPLETED,
            provider_request_ref=receipt.provider_request_ref,
            provider_message_id=receipt.provider_message_id,
            completion_kind=completion_kind,
            detail_code=detail_code,
        )
        return self.current

    async def definitive_fail(self, action_id, expected_state, lease_owner, observation):
        self.calls.append(("definitive_fail", observation.detail_code, observation))
        if expected_state not in _DEFINITIVE_FAIL_ALLOWED_FROM:
            raise InvalidDefinitiveFailStateError(
                f"invalid outbound definitive failure state: no transition from "
                f"{expected_state.value!r} to 'definitive_failed' "
                "(migrations/067_outbound_action_gateway.sql:346-389)"
            )
        self.current = replace(
            self.current,
            state=ActionState.DEFINITIVE_FAILED,
            detail_code=observation.detail_code,
            error_category=observation.category,
        )
        return self.current

    async def remediate_traffic_block(self, action_id, *, operator_identity, reason):
        self.calls.append(("remediate", action_id, operator_identity, reason))
        if self.remediation_error is not None:
            raise self.remediation_error
        parent = self.current
        successor_id = self.remediation_successor_id or uuid4()
        self.current = row(
            ActionState.RECEIVED,
            action_id=successor_id,
            wakeup_event_id=parent.wakeup_event_id,
            action_role=parent.action_role,
            operation=parent.operation,
            intent_kind=parent.intent_kind,
            appointment_slot=parent.appointment_slot,
            arguments=dict(parent.arguments),
            detail_code="operator_remediation_created",
            payload_hash=parent.payload_hash,
            canonical_context=dict(parent.canonical_context),
            canonical_scope=dict(parent.canonical_scope),
            recipient_scope=dict(parent.recipient_scope),
            provider_account=parent.provider_account,
            routing_policy_version=parent.routing_policy_version,
        )
        return self.current

    async def schedule_next_attempt(self, action_id, expected_state, delay_seconds, detail_code):
        self.calls.append(("schedule", expected_state, delay_seconds, detail_code))
        self.current = replace(
            self.current,
            detail_code=detail_code,
            next_attempt_at=NOW + timedelta(seconds=delay_seconds),
        )
        return self.current

    async def get(self, action_id):
        return self.current if self.current and self.current.action_id == action_id else None


class FakeAdapter:
    def __init__(self, *observations):
        self.observations = list(observations)
        self.calls = []

    def build_request(self, ctx, action_uid):
        self.calls.append(("build", ctx.target.target_id, action_uid))
        return object()

    async def invoke(self, client, provider_request):
        self.calls.append(("invoke",))
        return self.observations.pop(0)

    async def poll(self, client, observation):
        self.calls.append(("poll", observation.provider_request_ref))
        return self.observations.pop(0)

    def parse_receipt(self, ctx, observation):
        if observation.disposition is not ProviderDisposition.ACCEPTED:
            return None
        return ProviderReceipt(
            provider_request_ref=observation.provider_request_ref,
            provider_message_id=observation.message_id,
            accepted_at=observation.accepted_at,
            evidence=observation.evidence,
        )

    async def reconcile(self, client, ctx, action_uid, observation):
        self.calls.append(("reconcile",))
        return self.observations.pop(0)


def service(store, adapter, *, proof=None, circuit_guard=None, traffic_mode="off", traffic_probe=None):
    loader = AsyncMock()
    loader.load.return_value = context()
    preflight = AsyncMock()
    preflight.load.return_value = proof or evidence()
    return OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=preflight,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
        response_budget_seconds=1,
        sleep=AsyncMock(),
        circuit_guard=circuit_guard,
        traffic_mode=traffic_mode,
        traffic_probe=traffic_probe,
    )


class FakeProbe:
    def __init__(self, *, in_flight=None, newer=None, watermark=NOW, raise_on_in_flight=False):
        self.in_flight = in_flight or []
        self.newer = newer
        self.watermark = watermark
        self.raise_on_in_flight = raise_on_in_flight
        self.calls = []

    async def in_flight_actions(self, recipient_key, exclude_action_id):
        self.calls.append(("in_flight", recipient_key, exclude_action_id))
        if self.raise_on_in_flight:
            raise RuntimeError("probe boom")
        return self.in_flight

    async def newest_activity_after(self, recipient_key, channel_id, watermark, exclude_action_id):
        self.calls.append(("newest_activity", recipient_key, channel_id, watermark, exclude_action_id))
        return self.newer

    async def context_watermark(self, wakeup_event_id):
        self.calls.append(("watermark", wakeup_event_id))
        return self.watermark


@pytest.mark.asyncio
async def test_execute_persists_dispatch_before_io_and_completes_receipt_atomically():
    accepted_at = NOW
    pending = ProviderObservation(
        ProviderDisposition.PENDING,
        "provider_pending",
        provider_request_ref="req-1",
        provider_call_id="req-1",
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "provider_accepted",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=accepted_at,
        evidence={"kind": "provider_message_id"},
    )
    store = FakeStore()
    adapter = FakeAdapter(pending, accepted)

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.SENT
    assert [call[0] for call in store.calls] == [
        "create",
        "prepare",
        "claim",
        "transition",
        "record_request",
        "transition",
        "complete",
    ]
    assert store.calls[3][1:3] == (ActionState.PREPARED, ActionState.DISPATCHING)
    assert adapter.calls[:2] == [("build", "lead@convo.zillow.com", ACTION_UID), ("invoke",)]
    assert result.provider_request_ref == "req-1"


@pytest.mark.asyncio
async def test_lock_contention_waits_without_dispatching_provider():
    store = FakeStore(row())

    async def contended_prepare(ctx, expected_state):
        store.calls.append(("prepare", expected_state))
        store.current = replace(
            store.current,
            state=ActionState.DEPENDENCY_WAIT,
            detail_code="intent_lock_contended",
            next_attempt_at=NOW + timedelta(seconds=5),
        )
        return store.current

    store.prepare = contended_prepare
    adapter = FakeAdapter(
        ProviderObservation(
            ProviderDisposition.ACCEPTED,
            "accepted",
            provider_request_ref="must-not-send",
            message_id="must-not-send",
            accepted_at=NOW,
        )
    )

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "intent_lock_contended"
    assert not adapter.calls
    assert not any(call[0] == "claim" for call in store.calls)


@pytest.mark.asyncio
async def test_repeated_completed_execute_is_duplicate_without_provider_call():
    store = FakeStore(
        row(
            ActionState.COMPLETED,
            action_uid=ACTION_UID,
            provider_request_ref="req-existing",
            provider_message_id="mail-existing",
            completion_kind=CompletionKind.SENT,
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.DUPLICATE
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["create"]


@pytest.mark.asyncio
async def test_repeated_execute_accepts_durable_subject_alias_promotion_before_create():
    stored_prospect = "prospect:factbook:stable-id"
    current_prospect = "subject:durable-alias-id"
    stored_context = {"identity_version": "v1", "prospect_id": stored_prospect}
    stored_scope = {"version": "v1", "prospect_id": stored_prospect}
    current_context = replace(
        context(),
        prospect_id=current_prospect,
        canonical_context=MappingProxyType(
            {"identity_version": "v1", "prospect_id": current_prospect}
        ),
        canonical_scope=MappingProxyType(
            {"version": "v1", "prospect_id": current_prospect}
        ),
    )
    payload_hash = canonical_payload_hash(
        {
            "action_role": current_context.action_role.value,
            "operation": current_context.operation.value,
            "intent_kind": current_context.intent_kind.value,
            "appointment_slot": current_context.appointment_slot,
            "arguments": current_context.arguments,
            "canonical_context": stored_context,
        }
    )
    store = FakeStore(
        row(
            ActionState.COMPLETED,
            action_uid=ACTION_UID,
            provider_request_ref="req-existing",
            provider_message_id="mail-existing",
            completion_kind=CompletionKind.SENT,
            payload_hash=payload_hash,
            canonical_context=stored_context,
            canonical_scope=stored_scope,
            recipient_scope={
                "kind": "email_thread",
                "target_id": "lead@convo.zillow.com",
                "verified": True,
            },
            provider_account="nigel-zoho",
            routing_policy_version="v1",
        )
    )
    store.create_or_load = AsyncMock(
        side_effect=RuntimeError("outbound action payload mismatch")
    )
    adapter = FakeAdapter()
    gateway = service(store, adapter)
    gateway._context_loader.load.return_value = current_context

    result = await gateway.execute(request())

    assert result.status is PublicStatus.DUPLICATE
    assert adapter.calls == []
    store.create_or_load.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_existing_outbound_keeps_provider_id_as_receipt_identity():
    store = FakeStore()
    adapter = FakeAdapter()
    provider_message_id = "<existing-message@pfg.io>"
    proof = evidence(
        verified_outbound_message_id=702,
        verified_outbound_request_ref=provider_message_id,
        verified_outbound_covers_source=True,
    )

    result = await service(store, adapter, proof=proof).execute(request())

    assert result.status is PublicStatus.DUPLICATE
    assert store.current.provider_request_ref == provider_message_id
    assert store.current.provider_message_id == provider_message_id
    assert store.last_receipt.evidence == {
        "kind": "verified_existing_outbound",
        "cds_message_id": 702,
    }
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_provider_accepted_crash_recovery_completes_from_persisted_receipt():
    store = FakeStore(
        row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="req-accepted",
            provider_message_id="mail-accepted",
            provider_accepted_at=NOW,
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["claim", "complete"]
    assert store.current.provider_message_id == "mail-accepted"


@pytest.mark.asyncio
async def test_provider_accepted_exhaustion_recovers_persisted_receipt():
    store = FakeStore(
        row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="req-accepted",
            provider_message_id="mail-accepted",
            provider_accepted_at=NOW,
            attempt_count=5,
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).exhaust(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["claim", "complete"]
    assert store.current.provider_message_id == "mail-accepted"


def tenantcloud_context(**overrides):
    values = dict(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=ActionRole.PROVIDER_MUTATION,
        operation=Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        intent_kind=IntentKind.TENANTCLOUD_LEAD_STATUS,
        appointment_slot=None,
        arguments=MappingProxyType({"lead_id": 6001, "status": "working"}),
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
        target=DerivedTarget("tenantcloud_lead", "6001", True),
        provider_account="tenantcloud",
        routing_policy_version="v1",
        canonical_scope=MappingProxyType({"version": "v1"}),
        canonical_context=MappingProxyType({"identity_version": "v1"}),
        payload_hash="a" * 64,
        lock_holder=f"outbound-gateway:{ACTION_ID}",
        thread_identity="tenantcloud:lead-thread:6001",
        showing_lifecycle_id="showing:wake:7",
        calendar_event_uid=None,
    )
    values.update(overrides)
    return ActionContext(**values)


def tenantcloud_row(state=ActionState.RECEIVED, **overrides):
    values = dict(
        action_id=ACTION_ID,
        wakeup_event_id=7,
        action_role=ActionRole.PROVIDER_MUTATION,
        operation=Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        intent_kind=IntentKind.TENANTCLOUD_LEAD_STATUS,
        appointment_slot=None,
        arguments={"lead_id": 6001, "status": "working"},
        state=state,
        action_uid=ACTION_UID if state is not ActionState.RECEIVED else None,
        provider_request_ref=None,
        provider_message_id=None,
        provider_accepted_at=None,
        completion_kind=None,
        detail_code=state.value,
        attempt_count=0,
        next_attempt_at=NOW,
        payload_hash="",
        canonical_context={},
        canonical_scope={},
        recipient_scope={},
        provider_account="",
        routing_policy_version="",
    )
    values.update(overrides)
    return OutboundActionRecord(**values)


def tenantcloud_service(store, adapter):
    loader = AsyncMock()
    loader.load.return_value = tenantcloud_context()
    preflight = AsyncMock()
    preflight.load.return_value = evidence(
        current_recipient_id="6001",
        current_property_id=None,
        current_appointment_slot=None,
    )
    return OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=preflight,
        adapters={Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
        response_budget_seconds=1,
        sleep=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_tenantcloud_enqueue_preflights_and_prepares_without_provider_io() -> None:
    store = FakeStore(tenantcloud_row())
    adapter = AsyncMock()
    gateway = tenantcloud_service(store, adapter)

    result = await gateway.enqueue(tenantcloud_row().execute_request())

    assert result.status is PublicStatus.PENDING
    assert store.current.state is ActionState.PREPARED
    adapter.invoke.assert_not_called()
    adapter.reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_tenantcloud_prepare_remediation_preflights_without_provider_io() -> None:
    store = FakeStore(tenantcloud_row())
    adapter = AsyncMock()
    gateway = tenantcloud_service(store, adapter)

    result = await gateway.prepare(ACTION_ID)

    assert result.status is PublicStatus.PENDING
    assert store.current.state is ActionState.PREPARED
    adapter.invoke.assert_not_called()
    adapter.reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_tenantcloud_prepare_accepts_verified_remediation_successor_identity() -> None:
    successor_id = UUID("e8f10652-ae8c-528f-9d6a-05f56f7f18c0")
    context = tenantcloud_context()
    store = FakeStore(
        tenantcloud_row(
            action_id=successor_id,
            retry_of_action_id=ACTION_ID,
            detail_code="operator_remediation_created",
            payload_hash=context.payload_hash,
            canonical_context=dict(context.canonical_context),
            canonical_scope=dict(context.canonical_scope),
            recipient_scope={
                "kind": context.target.kind,
                "target_id": context.target.target_id,
                "verified": context.target.verified,
            },
            provider_account=context.provider_account,
            routing_policy_version=context.routing_policy_version,
        )
    )
    adapter = AsyncMock()
    gateway = tenantcloud_service(store, adapter)

    result = await gateway.prepare(successor_id)

    assert result.status is PublicStatus.PENDING
    assert store.current.state is ActionState.PREPARED
    adapter.invoke.assert_not_called()
    adapter.reconcile.assert_not_called()


def tenantcloud_context_for(operation, **overrides):
    """Full ActionContext for each of the four TenantCloud operations --
    enough detail (canonical_context claim/source/provider_ids, canonical_scope
    desired_state_hash) for tenantcloud_persisted_arguments() to run for real,
    the same way create_or_load() does at enqueue time."""
    target = {
        Operation.TENANTCLOUD_MESSAGE_SEND: DerivedTarget("tenantcloud_thread", "555", True),
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: DerivedTarget("tenantcloud_lead", "6001", True),
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: DerivedTarget("tenantcloud_property_unit", "property:12:unit:34", True),
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: DerivedTarget("tenantcloud_maintenance_request", "81", True),
    }[operation]
    intent = {
        Operation.TENANTCLOUD_MESSAGE_SEND: IntentKind.INQUIRY_REPLY,
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: IntentKind.TENANTCLOUD_LEAD_STATUS,
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: IntentKind.TENANTCLOUD_MAINTENANCE_CREATE,
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: IntentKind.TENANTCLOUD_MAINTENANCE_STATUS,
    }[operation]
    role = ActionRole.PROSPECT_REPLY if operation is Operation.TENANTCLOUD_MESSAGE_SEND else ActionRole.PROVIDER_MUTATION
    arguments = {
        Operation.TENANTCLOUD_MESSAGE_SEND: {"thread_id": 555, "text": "Friday at 10:30 works. — Nigel"},
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: {"lead_id": 6001, "status": "working"},
        Operation.TENANTCLOUD_MAINTENANCE_CREATE: {
            "property_id": 12,
            "unit_id": 34,
            "category_id": 57,
            "title": "Kitchen leak",
            "priority": "normal",
            "initiated_at": "2026-08-04",
            "text": "Sink leaking under cabinet",
            "entry_allowed": False,
            "available_on": None,
        },
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: {"request_id": 81, "status": 2},
    }[operation]
    canonical_context = {
        "identity_version": "v1",
        "tenantcloud_claim_id": 301,
        "source_event_id": "tenantcloud:claim:301",
    }
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
        canonical_scope=MappingProxyType({"version": "v1", "desired_state_hash": "d" * 64}),
        canonical_context=MappingProxyType(canonical_context),
        payload_hash="a" * 64,
        lock_holder=f"outbound-gateway:{ACTION_ID}",
        thread_identity="tenantcloud:thread-1",
        showing_lifecycle_id="showing:wake:7",
        calendar_event_uid=None,
    )
    values.update(overrides)
    return ActionContext(**values)


@pytest.mark.parametrize("operation", sorted(TENANTCLOUD_OPERATIONS, key=lambda op: op.value))
def test_execute_request_round_trips_arguments_enriched_by_create_or_load(operation):
    """Regression test for the round-2 finding: store.create_or_load()
    persists arguments enriched with desired_state/target_reference/
    idempotency_key (migration 118 reads those off outbound_actions.arguments
    directly). OutboundActionRecord.execute_request() rebuilds an
    ExecuteRequest from action.arguments to reload context on every
    reconcile()/resume() call -- and every ArgumentModel is a StrictModel
    with extra="forbid", so those three gateway-owned keys must not reach
    model_validate(). This builds arguments via the *real* enrichment path
    (tenantcloud_persisted_arguments, exactly what create_or_load calls),
    not a hand-authored dict, so it can't miss what create_or_load actually
    writes.
    """
    context = tenantcloud_context_for(operation)
    enriched_arguments = tenantcloud_persisted_arguments(context)
    assert {"desired_state", "target_reference", "idempotency_key"} <= set(enriched_arguments)

    row = tenantcloud_row(
        operation=operation,
        action_role=context.action_role,
        intent_kind=context.intent_kind,
        appointment_slot=context.appointment_slot,
        arguments=dict(enriched_arguments),
    )

    rebuilt = row.execute_request()

    assert rebuilt.operation is operation
    # The rebuilt, strict-model arguments must match the *original*
    # unenriched arguments exactly -- payload_hash was computed from these
    # at enqueue time, before enrichment, and context re-derivation depends
    # on getting the same arguments back.
    assert rebuilt.arguments.model_dump(mode="json", exclude_none=False) == dict(context.arguments)


# Exactly migration 118's six required keys
# (118_...sql:353-364 / tenantcloud_shared.READBACK_OBSERVATION_KEYS).
VERIFIED_READBACK_EVIDENCE = {
    "canonical_observed_state": {"status": "working"},
    "operation": "tenantcloud.lead.status.update",
    "provider_object_id": "6001",
    "target_reference": "lead:6001",
    "readback_timestamp": "2026-07-16T01:00:00Z",
    "readback_verified": True,
}


@pytest.mark.asyncio
async def test_tenantcloud_reconciliation_records_acceptance_before_completion():
    store = FakeStore(
        tenantcloud_row(
            ActionState.UNKNOWN,
            action_uid=ACTION_UID,
            provider_request_ref="lead:6001",
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_reconciled",
        provider_request_ref="lead:6001",
        message_id="tenantcloud-lead:6001:working",
        accepted_at=NOW,
        evidence={**VERIFIED_READBACK_EVIDENCE, "evidence_hash": "e" * 64},
    )

    result = await tenantcloud_service(store, FakeAdapter(accepted)).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert (
        "transition",
        ActionState.RECONCILING,
        ActionState.PROVIDER_ACCEPTED,
        "tenantcloud_lead_status_reconciled",
        "gateway-test",
    ) in store.calls
    assert ("complete", ActionState.PROVIDER_ACCEPTED, "lead:6001") in store.calls


@pytest.mark.asyncio
async def test_tenantcloud_persisted_acceptance_with_verified_readback_recovers_without_provider_io():
    store = FakeStore(
        tenantcloud_row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="lead:6001",
            provider_message_id="tenantcloud-lead:6001:working",
            provider_accepted_at=NOW,
            provider_evidence_kind="verified_provider_readback",
            provider_evidence_hash="e" * 64,
            provider_readback_evidence=VERIFIED_READBACK_EVIDENCE,
        )
    )
    adapter = FakeAdapter()

    result = await tenantcloud_service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["claim", "complete"]


@pytest.mark.asyncio
async def test_tenantcloud_persisted_acceptance_without_verified_readback_reconciles_never_dispatches_second_write():
    store = FakeStore(
        tenantcloud_row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="lead:6001",
            provider_message_id="tenantcloud-lead:6001:working",
            provider_accepted_at=NOW,
            # No provider_evidence_kind/hash persisted -- e.g. the worker
            # crashed after the durable PROVIDER_ACCEPTED transition but
            # before a matching outbound_action_attempts row could be
            # written or read back.
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_reconciled",
        provider_request_ref="lead:6001",
        message_id="tenantcloud-lead:6001:working",
        accepted_at=NOW,
        evidence={**VERIFIED_READBACK_EVIDENCE, "evidence_hash": "e" * 64},
    )
    adapter = FakeAdapter(accepted)

    result = await tenantcloud_service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == [("reconcile",)]
    assert not any(call[0] == "invoke" for call in adapter.calls)


@pytest.mark.asyncio
async def test_tenantcloud_persisted_acceptance_with_malformed_evidence_hash_reconciles():
    store = FakeStore(
        tenantcloud_row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="lead:6001",
            provider_message_id="tenantcloud-lead:6001:working",
            provider_accepted_at=NOW,
            provider_evidence_kind="verified_provider_readback",
            provider_evidence_hash="not-64-hex-chars",
            provider_readback_evidence=VERIFIED_READBACK_EVIDENCE,
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_reconciled",
        provider_request_ref="lead:6001",
        message_id="tenantcloud-lead:6001:working",
        accepted_at=NOW,
        evidence={**VERIFIED_READBACK_EVIDENCE, "evidence_hash": "e" * 64},
    )
    adapter = FakeAdapter(accepted)

    result = await tenantcloud_service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == [("reconcile",)]


@pytest.mark.asyncio
async def test_tenantcloud_persisted_acceptance_with_incomplete_observation_reconciles():
    incomplete_evidence = {key: value for key, value in VERIFIED_READBACK_EVIDENCE.items() if key != "target_reference"}
    store = FakeStore(
        tenantcloud_row(
            ActionState.PROVIDER_ACCEPTED,
            action_uid=ACTION_UID,
            provider_request_ref="lead:6001",
            provider_message_id="tenantcloud-lead:6001:working",
            provider_accepted_at=NOW,
            provider_evidence_kind="verified_provider_readback",
            provider_evidence_hash="e" * 64,
            provider_readback_evidence=incomplete_evidence,
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "tenantcloud_lead_status_reconciled",
        provider_request_ref="lead:6001",
        message_id="tenantcloud-lead:6001:working",
        accepted_at=NOW,
        evidence={**VERIFIED_READBACK_EVIDENCE, "evidence_hash": "e" * 64},
    )
    adapter = FakeAdapter(accepted)

    result = await tenantcloud_service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls == [("reconcile",)]


@pytest.mark.asyncio
async def test_same_property_overlap_does_not_block_ready_preflight():
    store = FakeStore()
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "provider_accepted",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=NOW,
        evidence={"kind": "provider_message_id"},
    )
    result = await service(store, FakeAdapter(accepted)).execute(request())
    assert result.status is PublicStatus.SENT
    assert any(call[0] == "prepare" for call in store.calls)


@pytest.mark.asyncio
async def test_ambiguous_timeout_retains_lock_and_never_retries_inline():
    store = FakeStore()
    adapter = FakeAdapter(ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_timeout"))

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.UNKNOWN
    assert adapter.calls.count(("invoke",)) == 1
    assert store.current.state is ActionState.UNKNOWN
    assert not any(call[0] == "definitive_fail" for call in store.calls)
    assert any(call[0] == "schedule" and call[3] == "provider_timeout" for call in store.calls)


@pytest.mark.asyncio
async def test_open_provider_circuit_defers_without_provider_call():
    circuit = AsyncMock()
    circuit.circuit_status.return_value = CircuitStatus(
        is_open=True,
        retry_after_seconds=120,
        failure_count=5,
    )
    store = FakeStore()
    adapter = FakeAdapter()

    result = await service(store, adapter, circuit_guard=circuit).execute(request())

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "provider_circuit_open"
    assert adapter.calls == []
    assert ("schedule", ActionState.PREPARED, 120, "provider_circuit_open") in store.calls


@pytest.mark.asyncio
async def test_repeated_execute_cannot_bypass_scheduled_retry_due_time():
    store = FakeStore(
        row(
            ActionState.RETRY_READY,
            action_uid=ACTION_UID,
            attempt_count=1,
            next_attempt_at=NOW + timedelta(minutes=2),
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.PENDING
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["create"]


@pytest.mark.asyncio
async def test_unknown_reconciliation_completes_directly_from_positive_evidence():
    store = FakeStore(
        row(
            ActionState.UNKNOWN,
            action_uid=ACTION_UID,
            provider_request_ref="req-1",
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "email_reconciled_by_message_id",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=NOW,
        evidence={"kind": "exact_message_id"},
    )
    adapter = FakeAdapter(accepted)

    result = await service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert ("complete", ActionState.RECONCILING, "req-1") in store.calls
    assert not any(call[0] == "transition" and call[2] is ActionState.COMPLETED for call in store.calls)


@pytest.mark.asyncio
async def test_expired_dispatching_lease_becomes_unknown_then_reconciles_without_send():
    store = FakeStore(
        row(
            ActionState.DISPATCHING,
            action_uid=ACTION_UID,
            provider_request_ref="req-1",
        )
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "email_reconciled_by_message_id",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=NOW,
        evidence={"kind": "exact_message_id"},
    )
    adapter = FakeAdapter(accepted)

    result = await service(store, adapter).reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert ("invoke",) not in adapter.calls
    assert ("reconcile",) in adapter.calls
    assert any(call[0] == "transition" and call[1] is ActionState.DISPATCHING and call[2] is ActionState.UNKNOWN for call in store.calls)


@pytest.mark.asyncio
async def test_explicit_non_acceptance_is_only_path_to_retry_ready():
    store = FakeStore()
    adapter = FakeAdapter(
        ProviderObservation(
            ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
            "provider_transient_upstream_error",
            provider_request_ref="req-1",
            category="transient_upstream_error",
            retryable=True,
            evidence={"status": "failed", "category": "transient_upstream_error"},
        )
    )

    result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.PENDING
    assert store.current.state is ActionState.RETRY_READY
    assert any(call[0] == "schedule" for call in store.calls)


@pytest.mark.asyncio
async def test_retryable_pre_dispatch_failure_retries_then_completes_with_one_provider_effect():
    """FIX 2, service-level half of the proof (the adapter-level half is
    tests/unit/outbound_gateway/test_adapters.py::
    test_tenantcloud_reconciliation_auth_unavailable_on_a_create_is_retryable_not_ambiguous,
    which shows the reconcile-time auth failure performs zero writes).
    test_explicit_non_acceptance_is_only_path_to_retry_ready above already
    proves _finish_observation promotes a retryable DEFINITIVE_NON_ACCEPTANCE
    straight to RETRY_READY for a single attempt; this extends that to a
    full two-attempt lifecycle matching the live incident's shape: the
    first, provably pre-dispatch failure retries (no manual_review, no
    reconcile detour), and once the retry is due, the very next attempt
    dispatches for real and completes -- exactly two invoke() calls, the
    first of which is the retried rejection with no provider effect."""
    store = FakeStore()
    clock_box = [NOW]
    loader = AsyncMock()
    loader.load.return_value = context()
    preflight = AsyncMock()
    preflight.load.return_value = evidence()
    retryable_rejection = ProviderObservation(
        ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
        "tenantcloud_auth_rejected_before_dispatch",
        category="provider_authentication",
        retryable=True,
    )
    accepted = ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "provider_accepted",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=NOW,
        evidence={"kind": "provider_message_id"},
    )
    adapter = FakeAdapter(retryable_rejection, accepted)
    svc = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=preflight,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: clock_box[0],
        lease_owner="gateway-test",
        response_budget_seconds=1,
        sleep=AsyncMock(),
    )

    first = await svc.execute(request())

    assert first.status is PublicStatus.PENDING
    assert store.current.state is ActionState.RETRY_READY
    assert not any(call[0] == "transition" and call[2] in {ActionState.UNKNOWN, ActionState.RECONCILING} for call in store.calls)
    assert adapter.calls.count(("invoke",)) == 1

    clock_box[0] = NOW + timedelta(hours=1)
    second = await svc.execute(request())

    assert second.status is PublicStatus.SENT
    assert store.current.state is ActionState.COMPLETED
    assert adapter.calls.count(("invoke",)) == 2


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_dead_letters_unknown_without_redispatch():
    store = FakeStore(
        row(
            ActionState.UNKNOWN,
            action_uid=ACTION_UID,
            provider_request_ref="req-1",
            attempt_count=5,
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).exhaust(ACTION_ID)

    assert result.status is PublicStatus.MANUAL_REVIEW
    assert store.current.state is ActionState.MANUAL_REVIEW
    assert adapter.calls == []
    assert any(call[0] == "transition" and call[2] is ActionState.DEAD_LETTER for call in store.calls)
    assert any(call[0] == "transition" and call[1] is ActionState.DEAD_LETTER and call[2] is ActionState.MANUAL_REVIEW for call in store.calls)


@pytest.mark.asyncio
async def test_newer_inbound_marks_action_stale_before_lock_or_provider_io():
    store = FakeStore()
    adapter = FakeAdapter()
    proof = evidence(later_inbound_message_id=701)

    result = await service(store, adapter, proof=proof).execute(request())

    assert result.status is PublicStatus.STALE
    assert adapter.calls == []
    assert [call[0] for call in store.calls] == ["create", "transition"]


@pytest.mark.asyncio
async def test_gateway_does_not_duplicate_skill_owned_zillow_refresh_policy():
    old_context = replace(context(), source_sent_at=datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc))
    loader = AsyncMock()
    loader.load.return_value = old_context
    proof_loader = AsyncMock()
    proof_loader.load.return_value = evidence(refresh_required_through=NOW, refresh=None)
    store = FakeStore()
    adapter = FakeAdapter(
        ProviderObservation(
            ProviderDisposition.ACCEPTED,
            "provider_accepted",
            provider_request_ref="req-1",
            message_id="mail-1",
            accepted_at=NOW,
            evidence={"kind": "provider_message_id"},
        )
    )
    gateway = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=proof_loader,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
    )

    result = await gateway.execute(request())

    assert result.status is PublicStatus.SENT
    assert "staff" not in result.detail_code
    assert ("invoke",) in adapter.calls


@pytest.mark.asyncio
async def test_due_dependency_retry_is_claimed_so_retry_budget_advances():
    store = FakeStore(
        row(
            ActionState.DEPENDENCY_WAIT,
            action_uid=ACTION_UID,
            detail_code="zillow_refresh_required",
            attempt_count=2,
        )
    )
    adapter = FakeAdapter()
    proof = evidence(refresh_required_through=NOW, refresh=None)
    old_context = replace(
        context(),
        source_sent_at=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc),
        intent_kind=IntentKind.SHOWING_CONFIRMATION,
    )
    loader = AsyncMock()
    loader.load.return_value = old_context
    proof_loader = AsyncMock()
    proof_loader.load.return_value = replace(
        proof,
        calendar_dependency=CalendarDependencyState.PENDING,
    )
    gateway = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=proof_loader,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
    )

    result = await gateway.resume(ACTION_ID)

    assert result.status is PublicStatus.PENDING
    assert store.calls[0][:2] == ("claim", ActionState.DEPENDENCY_WAIT)
    assert any(call[0] == "schedule" for call in store.calls)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_due_dependency_terminal_preflight_uses_held_lease():
    store = FakeStore(
        row(
            ActionState.DEPENDENCY_WAIT,
            action_uid=ACTION_UID,
            detail_code="zillow_refresh_required",
            attempt_count=2,
        )
    )
    adapter = FakeAdapter()
    proof = evidence(later_inbound_message_id=701)

    result = await service(store, adapter, proof=proof).resume(ACTION_ID)

    assert result.status is PublicStatus.STALE
    assert store.calls[-1] == (
        "transition",
        ActionState.DEPENDENCY_WAIT,
        ActionState.STALE,
        "newer_inbound",
        "gateway-test",
    )


@pytest.mark.asyncio
async def test_worker_resume_rejects_mutated_persisted_context_before_provider_io():
    store = FakeStore(
        row(
            ActionState.PREPARED,
            action_uid=ACTION_UID,
            payload_hash="f" * 64,
            provider_account="nigel-zoho",
        )
    )
    adapter = FakeAdapter()

    result = await service(store, adapter).resume(ACTION_ID)

    assert result.status is PublicStatus.MANUAL_REVIEW
    assert store.current.state is ActionState.MANUAL_REVIEW
    assert adapter.calls == []
    assert any(call[0] == "transition" and call[2] is ActionState.DEAD_LETTER for call in store.calls)


@pytest.mark.asyncio
async def test_worker_accepts_one_way_durable_subject_alias_promotion():
    stored_prospect = "prospect:factbook:stable-id"
    current_prospect = "subject:durable-alias-id"
    stored_context = {"identity_version": "v1", "prospect_id": stored_prospect}
    stored_scope = {"version": "v1", "prospect_id": stored_prospect}
    current_context = replace(
        context(),
        prospect_id=current_prospect,
        canonical_context=MappingProxyType(
            {"identity_version": "v1", "prospect_id": current_prospect}
        ),
        canonical_scope=MappingProxyType(
            {"version": "v1", "prospect_id": current_prospect}
        ),
    )
    payload_hash = canonical_payload_hash(
        {
            "action_role": current_context.action_role.value,
            "operation": current_context.operation.value,
            "intent_kind": current_context.intent_kind.value,
            "appointment_slot": current_context.appointment_slot,
            "arguments": current_context.arguments,
            "canonical_context": stored_context,
        }
    )
    store = FakeStore(
        row(
            ActionState.UNKNOWN,
            action_uid=ACTION_UID,
            provider_request_ref="req-1",
            payload_hash=payload_hash,
            canonical_context=stored_context,
            canonical_scope=stored_scope,
            recipient_scope={
                "kind": "email_thread",
                "target_id": "lead@convo.zillow.com",
                "verified": True,
            },
            provider_account="nigel-zoho",
            routing_policy_version="v1",
        )
    )
    loader = AsyncMock()
    loader.load.return_value = current_context
    proof_loader = AsyncMock()
    adapter = FakeAdapter(
        ProviderObservation(
            ProviderDisposition.ACCEPTED,
            "provider_accepted",
            provider_request_ref="req-1",
            message_id="mail-1",
            accepted_at=NOW,
            evidence={"kind": "provider_message_id"},
        )
    )
    gateway = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=proof_loader,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
    )

    result = await gateway.reconcile(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert store.current.state is ActionState.COMPLETED
    assert ("reconcile",) in adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "initial_state"),
    [("resume", ActionState.PREPARED), ("reconcile", ActionState.UNKNOWN)],
)
async def test_worker_terminalizes_context_that_can_no_longer_be_derived(method, initial_state):
    store = FakeStore(row(initial_state, action_uid=ACTION_UID, payload_hash="a" * 64))
    adapter = FakeAdapter()
    loader = AsyncMock()
    loader.load.side_effect = ContextDerivationError("wakeup event does not exist")
    proof_loader = AsyncMock()
    gateway = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=proof_loader,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
    )

    result = await getattr(gateway, method)(ACTION_ID)

    assert result.status is PublicStatus.MANUAL_REVIEW
    assert store.current.state is ActionState.MANUAL_REVIEW
    assert adapter.calls == []
    assert any(call[0] == "transition" and call[2] is ActionState.DEAD_LETTER for call in store.calls)


def _accepted_observation() -> ProviderObservation:
    return ProviderObservation(
        ProviderDisposition.ACCEPTED,
        "provider_accepted",
        provider_request_ref="req-1",
        message_id="mail-1",
        accepted_at=NOW,
        evidence={"kind": "provider_message_id"},
    )


@pytest.mark.asyncio
async def test_traffic_control_enforce_defers_lease_held_instead_of_terminalizing():
    """Important 6: a lease block is inherently short-lived -- the other
    in-flight action will reach a terminal state on its own -- unlike a
    stale-context block, which needs a conscious agent decision. A
    deterministic action_id plus a terminal DEFINITIVE_FAILED meant a
    seconds-long lease overlap would brick that resend forever, since
    override never bypasses a lease (only staleness). So enforce+lease_held
    must defer: no claim()/definitive_fail() at all, the row stays exactly
    where prepare() (the same call _preflight()'s READY branch uses) left
    it -- non-terminal and worker-visible, so the worker's next resume()
    (PREPARED is in worker.py's list_work -> resume() routing) re-runs this
    same gate and self-heals once the lease clears.

    FakeStore() with no initial row means create_or_load() lands on a
    fresh RECEIVED row (row()'s default state) -- claim_outbound_action's
    live whitelist excludes 'received' (migrations/
    068_outbound_gateway_observability.sql:156-159), so this also exercises
    the RECEIVED regression: the gate still moves the row through
    prepare() first (to reach a worker-visible state), it just never goes
    on to claim()/definitive_fail() for a lease_held verdict."""
    store = FakeStore()
    adapter = FakeAdapter()
    probe = FakeProbe(
        in_flight=[
            InFlightAction(
                action_id=uuid4(),
                operation="email.send",
                state="dispatching",
                created_at=NOW,
                preview="Friday works for us too.",
            )
        ]
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "lease_held"
    assert result.detail is not None
    assert "Friday works for us too." in result.detail
    assert [call[0] for call in store.calls] == ["create", "prepare"]
    assert store.current.state is ActionState.PREPARED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_resume_enforce_defers_lease_held_on_an_already_prepared_action():
    """Same Important 6 behavior, but from resume() on an action that was
    already PREPARED (no RECEIVED->prepare() dance involved at all --
    claimable is just the action as-is, so this proves the defer applies
    independent of the RECEIVED special case)."""
    store = FakeStore(row(ActionState.PREPARED, action_uid=ACTION_UID))
    adapter = FakeAdapter()
    probe = FakeProbe(
        in_flight=[
            InFlightAction(
                action_id=uuid4(),
                operation="email.send",
                state="dispatching",
                created_at=NOW,
                preview="Friday works for us too.",
            )
        ]
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).resume(ACTION_ID)

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "lease_held"
    assert result.detail is not None
    assert not any(call[0] in ("claim", "definitive_fail") for call in store.calls)
    assert store.current.state is ActionState.PREPARED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_traffic_control_enforce_blocks_on_stale_context():
    store = FakeStore()
    adapter = FakeAdapter()
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.FAILED
    assert result.detail_code == "stale_context"
    assert result.detail is not None
    assert "Are you still available Friday?" in result.detail
    assert "override" in result.detail
    assert adapter.calls == []
    definitive_calls = [call for call in store.calls if call[0] == "definitive_fail"]
    assert len(definitive_calls) == 1
    observation = definitive_calls[0][2]
    assert observation.category == "traffic_blocked"
    assert "Are you still available Friday?" in observation.evidence["detail"]
    assert "override" in observation.evidence["detail"]


@pytest.mark.asyncio
async def test_traffic_control_override_bypasses_stale_context_block():
    store = FakeStore()
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).execute(request(override=True))

    assert result.status is PublicStatus.SENT
    assert adapter.calls
    assert not any(call[0] == "definitive_fail" for call in store.calls)


@pytest.mark.asyncio
async def test_traffic_control_shadow_logs_would_block_and_dispatches(caplog):
    store = FakeStore()
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    with caplog.at_level(logging.WARNING):
        result = await service(store, adapter, traffic_mode="shadow", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.SENT
    assert adapter.calls
    assert not any(call[0] == "definitive_fail" for call in store.calls)
    messages = [record.getMessage() for record in caplog.records]
    assert any("traffic control shadow" in message and "stale_context" in message and "prospect:amanda" in message for message in messages)


@pytest.mark.asyncio
async def test_traffic_control_off_never_calls_probe():
    store = FakeStore()
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(
        in_flight=[
            InFlightAction(
                action_id=uuid4(),
                operation="email.send",
                state="dispatching",
                created_at=NOW,
                preview="would block if consulted",
            )
        ]
    )

    result = await service(store, adapter, traffic_mode="off", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.SENT
    assert probe.calls == []


@pytest.mark.asyncio
async def test_traffic_control_fails_open_and_dispatches_on_probe_error(caplog):
    store = FakeStore()
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(raise_on_in_flight=True)

    with caplog.at_level(logging.WARNING):
        result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.SENT
    assert adapter.calls
    assert not any(call[0] == "definitive_fail" for call in store.calls)
    messages = [record.getMessage() for record in caplog.records]
    assert any("traffic control" in message for message in messages)


def test_traffic_mode_rejects_unknown_value():
    with pytest.raises(ValueError):
        service(FakeStore(), FakeAdapter(), traffic_mode="paranoid")


def test_traffic_mode_without_probe_warns_at_construction(caplog):
    """mode in {shadow, enforce} with no probe silently behaves like 'off'
    (the gate short-circuits on `traffic_probe is None` every call) -- that
    is exactly the wiring bug (env var set, probe forgotten in
    build_runtime()) that must be loud, not invisible."""
    with caplog.at_level(logging.WARNING):
        service(FakeStore(), FakeAdapter(), traffic_mode="shadow", traffic_probe=None)

    messages = [record.getMessage() for record in caplog.records]
    assert any("traffic_probe" in message for message in messages)


def test_traffic_mode_off_without_probe_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        service(FakeStore(), FakeAdapter(), traffic_mode="off", traffic_probe=None)

    assert not caplog.records


@pytest.mark.asyncio
async def test_resume_runs_traffic_gate_with_override_forced_false(caplog):
    """The IMPORTANT gap: worker.py's list_work routes dependency_wait/
    prepared/retry_ready actions through resume(), never execute() -- the
    highest-staleness-risk case (longest wait) must not skip the gate.
    resume() has no ExecuteRequest, so override is unavailable and must be
    treated as False, not True."""
    store = FakeStore(row(ActionState.PREPARED, action_uid=ACTION_UID))
    adapter = FakeAdapter()
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).resume(ACTION_ID)

    assert result.status is PublicStatus.FAILED
    assert result.detail_code == "stale_context"
    assert adapter.calls == []
    assert any(call[0] == "definitive_fail" for call in store.calls)


@pytest.mark.asyncio
async def test_resume_shadow_mode_logs_would_block_and_dispatches(caplog):
    store = FakeStore(row(ActionState.PREPARED, action_uid=ACTION_UID))
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    with caplog.at_level(logging.WARNING):
        result = await service(store, adapter, traffic_mode="shadow", traffic_probe=probe).resume(ACTION_ID)

    assert result.status is PublicStatus.SENT
    assert adapter.calls
    messages = [record.getMessage() for record in caplog.records]
    assert any("traffic control shadow" in message and "stale_context" in message for message in messages)


@pytest.mark.asyncio
async def test_override_resend_of_traffic_blocked_action_dispatches_a_successor():
    """CRITICAL 2's red-green: block, then override-resend. The successor
    action (next effect_ordinal, retry_of_action_id set -- via the same
    create_outbound_remediation_context() operator remediation uses,
    Comm-Data-Store migrations/067_outbound_action_gateway.sql:1200-1267)
    must reach dispatch; the original blocked action_id is never reused
    (it is permanently DEFINITIVE_FAILED and unclaimable)."""
    successor_id = uuid4()
    store = FakeStore(remediation_successor_id=successor_id)
    adapter = FakeAdapter(_accepted_observation())
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )
    svc = service(store, adapter, traffic_mode="enforce", traffic_probe=probe)

    blocked = await svc.execute(request())
    assert blocked.status is PublicStatus.FAILED
    assert blocked.detail_code == "stale_context"
    assert blocked.action_id == ACTION_ID

    resent = await svc.execute(request(override=True))

    assert resent.status is PublicStatus.SENT
    assert resent.action_id == successor_id
    assert resent.action_id != blocked.action_id
    assert adapter.calls
    assert any(call[0] == "remediate" and call[1] == ACTION_ID for call in store.calls)


@pytest.mark.asyncio
async def test_override_resend_without_operator_remediation_stays_blocked():
    """create_outbound_remediation_context requires an evidence-resolved
    outbound_action_resolutions row (067:1221-1228), which only
    resolve_outbound_action_from_evidence (operator-only, never granted to
    the gateway role) can write. Until an operator resolves the block,
    override must fail soft -- stay on the original terminal result -- not
    raise the DB's unhandled precondition-violation exception."""
    store = FakeStore(remediation_error=RuntimeError("remediation requires evidence-resolved definitive failure"))
    adapter = FakeAdapter()
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )
    svc = service(store, adapter, traffic_mode="enforce", traffic_probe=probe)

    blocked = await svc.execute(request())
    assert blocked.status is PublicStatus.FAILED

    resent = await svc.execute(request(override=True))

    assert resent.status is PublicStatus.FAILED
    assert resent.action_id == ACTION_ID
    assert resent.detail is not None
    assert "operator" in resent.detail
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_traffic_control_enforce_defers_contended_dependency_wait_instead_of_terminalizing():
    """Residual Critical from re-review: outbound_action_transition_allowed()
    has no ('dependency_wait', 'definitive_failed') edge (Comm-Data-Store
    migrations/067_outbound_action_gateway.sql:346-389). A fresh RECEIVED row
    whose prepare() hits a contended intent lock lands in DEPENDENCY_WAIT
    (067:556-564) -- claim()+definitive_fail() on that state would raise the
    DB's unhandled 'invalid outbound definitive failure state'. The gate must
    defer (leave the row in DEPENDENCY_WAIT, return a PENDING blocked result)
    instead, exactly like _preflight()'s READY branch already does for the
    same lock-contention outcome."""
    store = FakeStore()
    adapter = FakeAdapter()
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    async def contended_prepare(ctx, expected_state):
        store.calls.append(("prepare", expected_state))
        store.current = replace(
            store.current,
            state=ActionState.DEPENDENCY_WAIT,
            detail_code="intent_lock_contended",
            next_attempt_at=NOW + timedelta(seconds=5),
        )
        return store.current

    store.prepare = contended_prepare

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).execute(request())

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "stale_context"
    assert result.detail is not None
    assert "override" in result.detail
    assert store.current.state is ActionState.DEPENDENCY_WAIT
    assert not any(call[0] in ("claim", "definitive_fail") for call in store.calls)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_resume_enforce_defers_dependency_wait_instead_of_terminalizing():
    """Same residual Critical, second reachable path: worker.py routes an
    already-dependency_wait action straight to resume(), never through
    execute()'s prepare() at all -- the gate must defer here too."""
    store = FakeStore(
        row(
            ActionState.DEPENDENCY_WAIT,
            action_uid=ACTION_UID,
            detail_code="intent_lock_contended",
        )
    )
    adapter = FakeAdapter()
    probe = FakeProbe(
        newer=NewerActivity(
            direction="inbound",
            source="zillow",
            occurred_at=NOW,
            preview="Are you still available Friday?",
            message_id=999,
            action_id=None,
        )
    )

    result = await service(store, adapter, traffic_mode="enforce", traffic_probe=probe).resume(ACTION_ID)

    assert result.status is PublicStatus.PENDING
    assert result.detail_code == "stale_context"
    assert result.detail is not None
    assert store.current.state is ActionState.DEPENDENCY_WAIT
    assert not any(call[0] in ("claim", "definitive_fail") for call in store.calls)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_execute_swallows_post_dispatch_exception_and_returns_durable_row_state(caplog):
    """A network timeout *after* the provider already accepted the HTTP
    request (adapter.invoke() raises here, simulating that) must never
    escape execute() as a raised error: FastMCP wraps any uncaught
    exception as "Error executing tool outbound_action: ...", and the CDS
    reconciler's rejection-prefix rule treats that wrapper as proof
    nothing was sent. By the time invoke() runs, claim()+transition() to
    DISPATCHING has already happened (service.py's _dispatch()), so the
    row is durably recoverable -- the caller must get that durable state
    back, not a raised exception, and an ERROR must be logged."""
    store = FakeStore()
    adapter = FakeAdapter()
    adapter.invoke = AsyncMock(side_effect=RuntimeError("network timeout after accept"))

    with caplog.at_level(logging.ERROR):
        result = await service(store, adapter).execute(request())

    assert result.status is PublicStatus.PENDING
    assert result.action_id == ACTION_ID
    assert store.current.state is ActionState.DISPATCHING
    messages = [r.getMessage() for r in caplog.records]
    assert any("post-dispatch exception" in m for m in messages)


@pytest.mark.asyncio
async def test_resume_swallows_post_dispatch_exception_and_returns_durable_row_state(caplog):
    store = FakeStore(row(ActionState.PREPARED, action_uid=ACTION_UID))
    adapter = FakeAdapter()
    adapter.invoke = AsyncMock(side_effect=RuntimeError("network timeout after accept"))

    with caplog.at_level(logging.ERROR):
        result = await service(store, adapter).resume(ACTION_ID)

    assert result.status is PublicStatus.PENDING
    assert store.current.state is ActionState.DISPATCHING
    messages = [r.getMessage() for r in caplog.records]
    assert any("post-dispatch exception" in m for m in messages)


@pytest.mark.asyncio
async def test_execute_still_raises_context_derivation_error_before_dispatch():
    """Regression guard: context load/validation is deliberately NOT
    covered by the post-dispatch except clause -- a true pre-dispatch
    rejection must keep raising so the MCP error wrapper (and the
    reconciler's rejection-prefix rule that depends on it) stays accurate
    for genuine rejections."""
    store = FakeStore()
    adapter = FakeAdapter()
    loader = AsyncMock()
    loader.load.side_effect = ContextDerivationError("wakeup event does not exist")
    preflight = AsyncMock()
    svc = OutboundActionService(
        store=store,
        context_loader=loader,
        evidence_loader=preflight,
        adapters={Operation.EMAIL_SEND: adapter},
        provider_client=object(),
        clock=lambda: NOW,
        lease_owner="gateway-test",
    )

    with pytest.raises(ContextDerivationError):
        await svc.execute(request())
