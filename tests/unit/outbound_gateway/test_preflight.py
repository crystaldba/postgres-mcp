from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import MappingProxyType
from uuid import UUID

import pytest

from postgres_mcp.outbound_gateway.context import ActionContext
from postgres_mcp.outbound_gateway.context import DerivedTarget
from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.preflight import CalendarDependencyState
from postgres_mcp.outbound_gateway.preflight import PreflightEvidence
from postgres_mcp.outbound_gateway.preflight import PreflightOutcome
from postgres_mcp.outbound_gateway.preflight import SafetyPreflight

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


def context(**overrides):
    values = {
        "action_id": UUID("8f8f1a45-13a7-4bd3-a15a-f8d265bbc567"),
        "wakeup_event_id": 123,
        "action_role": ActionRole.PROSPECT_REPLY,
        "operation": Operation.EMAIL_SEND,
        "intent_kind": IntentKind.SHOWING_OFFER,
        "appointment_slot": datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
        "arguments": MappingProxyType({"text": "Tour"}),
        "source": "zillow",
        "source_message_id": 700,
        "source_message_key": "zillow:700",
        "source_sent_at": NOW - timedelta(minutes=10),
        "conversation_id": "conversation:zillow-1",
        "conversation_watermark": 700,
        "prospect_id": "prospect:a",
        "aliases": ("email:a@example.com",),
        "property_id": "building:bullman",
        "property_label": "138 Bullman St #144-A",
        "target": DerivedTarget("email_thread", "a@convo.zillow.com", True),
        "provider_account": "nigel-zoho",
        "routing_policy_version": "v1",
        "canonical_scope": MappingProxyType({"role": "prospect_reply"}),
        "canonical_context": MappingProxyType({"source_message_id": 700}),
        "payload_hash": "a" * 64,
        "lock_holder": "outbound-gateway:8f8f1a45-13a7-4bd3-a15a-f8d265bbc567",
        "thread_identity": "zillow-thread-1",
        "showing_lifecycle_id": "showing:123",
        "calendar_event_uid": None,
    }
    values.update(overrides)
    return ActionContext(**values)


def evidence(ctx, **overrides):
    values = {
        "current_recipient_id": ctx.target.target_id,
        "current_property_id": ctx.property_id,
        "current_appointment_slot": ctx.appointment_slot,
        "later_inbound_message_id": None,
        "verified_outbound_message_id": None,
        "verified_outbound_request_ref": None,
        "verified_outbound_covers_source": False,
        "calendar_dependency": CalendarDependencyState.NOT_REQUIRED,
        "calendar_already_applied": False,
        "calendar_context_changed": False,
        "overlapping_showing_prospect_ids": (),
        "refresh_required_through": NOW,
        "refresh": None,
    }
    values.update(overrides)
    return PreflightEvidence(**values)


def test_later_prospect_turn_is_stale_and_verified_reply_is_duplicate():
    ctx = context(source="quo")
    stale = SafetyPreflight.evaluate(
        ctx,
        evidence(ctx, later_inbound_message_id=701),
        now=NOW,
    )
    assert stale.outcome == PreflightOutcome.STALE
    assert stale.detail_code == "newer_inbound"
    duplicate = SafetyPreflight.evaluate(
        ctx,
        evidence(
            ctx,
            verified_outbound_message_id=702,
            verified_outbound_request_ref="quo-message-1",
            verified_outbound_covers_source=True,
        ),
        now=NOW,
    )
    assert duplicate.outcome == PreflightOutcome.DUPLICATE
    assert duplicate.detail_code == "already_handled"


def test_unrelated_messages_do_not_suppress_calendar_or_internal_roles():
    calendar = context(
        source="zillow",
        action_role=ActionRole.CALENDAR_MUTATION,
        operation=Operation.CALENDAR_CREATE,
        intent_kind=IntentKind.SHOWING_CREATE,
        target=DerivedTarget("calendar", "nigel", True),
    )
    internal = context(
        source="zoho_cliq",
        action_role=ActionRole.INTERNAL_NOTIFICATION,
        operation=Operation.CLIQ_CHANNEL_POST,
        intent_kind=IntentKind.LEAD_ALERT,
        appointment_slot=None,
        target=DerivedTarget("cliq_channel", "tenant-leads", True),
    )
    noisy = {"later_inbound_message_id": 999, "verified_outbound_message_id": 1000}
    assert SafetyPreflight.evaluate(calendar, evidence(calendar, **noisy), now=NOW).outcome == PreflightOutcome.READY
    assert SafetyPreflight.evaluate(internal, evidence(internal, **noisy), now=NOW).outcome == PreflightOutcome.READY


@pytest.mark.parametrize(
    ("changed", "detail"),
    [
        ({"current_recipient_id": "wrong"}, "recipient_mismatch"),
        ({"current_property_id": "building:other"}, "context_mismatch"),
        ({"current_appointment_slot": datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)}, "context_mismatch"),
    ],
)
def test_recipient_property_and_slot_changes_reject(changed, detail):
    ctx = context(source="quo")
    decision = SafetyPreflight.evaluate(ctx, evidence(ctx, **changed), now=NOW)
    assert decision.outcome == PreflightOutcome.REJECTED
    assert decision.detail_code == detail


@pytest.mark.parametrize(
    "intent",
    [
        IntentKind.SHOWING_CONFIRMATION,
        IntentKind.SHOWING_RESCHEDULE,
        IntentKind.SHOWING_CANCELLATION,
    ],
)
def test_confirmation_reschedule_and_cancellation_wait_for_calendar(intent):
    ctx = context(
        source="quo",
        intent_kind=intent,
        appointment_slot=None if intent is IntentKind.SHOWING_CANCELLATION else context().appointment_slot,
    )
    waiting = SafetyPreflight.evaluate(
        ctx,
        evidence(ctx, calendar_dependency=CalendarDependencyState.PENDING),
        now=NOW,
    )
    assert waiting.outcome == PreflightOutcome.DEPENDENCY_WAIT
    assert waiting.detail_code == "calendar_dependency_pending"
    ready = SafetyPreflight.evaluate(
        ctx,
        evidence(ctx, calendar_dependency=CalendarDependencyState.COMPLETED),
        now=NOW,
    )
    assert ready.outcome == PreflightOutcome.READY


def test_inquiry_and_offer_need_no_calendar_dependency_and_group_showings_pass():
    for intent in (IntentKind.INQUIRY_REPLY, IntentKind.SHOWING_OFFER):
        ctx = context(
            source="quo",
            intent_kind=intent,
            appointment_slot=None if intent is IntentKind.INQUIRY_REPLY else context().appointment_slot,
        )
        decision = SafetyPreflight.evaluate(
            ctx,
            evidence(
                ctx,
                calendar_dependency=CalendarDependencyState.NOT_REQUIRED,
                overlapping_showing_prospect_ids=("prospect:b", "prospect:c"),
            ),
            now=NOW,
        )
        assert decision.outcome == PreflightOutcome.READY
        assert "staff" not in decision.detail_code


def test_calendar_duplicate_and_changed_revision_are_role_specific():
    ctx = context(
        source="zillow",
        action_role=ActionRole.CALENDAR_MUTATION,
        operation=Operation.CALENDAR_CREATE,
        intent_kind=IntentKind.SHOWING_CREATE,
        target=DerivedTarget("calendar", "nigel", True),
    )
    duplicate = SafetyPreflight.evaluate(
        ctx,
        evidence(ctx, calendar_already_applied=True),
        now=NOW,
    )
    assert duplicate.outcome == PreflightOutcome.DUPLICATE
    assert duplicate.detail_code == "calendar_already_applied"
    stale = SafetyPreflight.evaluate(
        ctx,
        evidence(ctx, calendar_context_changed=True),
        now=NOW,
    )
    assert stale.outcome == PreflightOutcome.STALE
    assert stale.detail_code == "calendar_context_changed"


@pytest.mark.parametrize("age", [timedelta(minutes=29), timedelta(minutes=30), timedelta(hours=3)])
def test_zillow_refresh_policy_is_skill_owned_not_duplicated_in_gateway(age):
    ctx = context(source_sent_at=NOW - age)

    decision = SafetyPreflight.evaluate(ctx, evidence(ctx, refresh=None), now=NOW)

    assert decision.outcome == PreflightOutcome.READY
    assert decision.detail_code == "ready"
    assert "staff" not in decision.detail_code
