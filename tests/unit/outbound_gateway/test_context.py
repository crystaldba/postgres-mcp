from dataclasses import replace
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID

import pytest

from postgres_mcp.outbound_gateway.context import ActionContextLoader
from postgres_mcp.outbound_gateway.context import ContextDerivationError
from postgres_mcp.outbound_gateway.context import RoutingPolicy
from postgres_mcp.outbound_gateway.models import ExecuteRequest
from postgres_mcp.outbound_gateway.models import parse_outbound_request
from postgres_mcp.outbound_gateway.repository import AliasResolution
from postgres_mcp.outbound_gateway.repository import ConversationSnapshot
from postgres_mcp.outbound_gateway.repository import OutboundGatewayRepository
from postgres_mcp.outbound_gateway.repository import WakeEventRecord
from postgres_mcp.outbound_gateway.tenantcloud_shared import tenantcloud_idempotency_key

ACTION_NAMESPACE = UUID("ed6fcf85-39e7-5cdf-9fb8-ccca32a62e8d")


class FakeRepository:
    def __init__(self, record, *, canonical_subject="prospect:canonical", ambiguous=False):
        self.record = record
        self.canonical_subject = canonical_subject
        self.ambiguous = ambiguous
        self.alias_calls = []

    async def load_wake_event(self, wakeup_event_id):
        return self.record if wakeup_event_id == self.record.wakeup_event_id else None

    async def load_conversation_snapshot(self, channel_id):
        return ConversationSnapshot(
            conversation_watermark=900,
            latest_message_id=900,
            latest_sent_at=datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc),
        )

    async def resolve_canonical_subject(self, aliases, property_scope):
        self.alias_calls.append((aliases, property_scope))
        return AliasResolution(
            canonical_subject=self.canonical_subject,
            ambiguous=self.ambiguous,
        )


def record(**overrides):
    values = {
        "wakeup_event_id": 12345,
        "event_source": "zoho_mail",
        "source_event_id": "wake-source-1",
        "event_created_at": datetime(2026, 7, 15, 22, 31, tzinfo=timezone.utc),
        "message_id": 700,
        "canonical_message_id": None,
        "message_source": "zoho_mail",
        "source_message_id": "mail-700",
        "message_sent_at": datetime(2026, 7, 15, 22, 20, tzinfo=timezone.utc),
        "message_updated_at": datetime(2026, 7, 15, 22, 21, tzinfo=timezone.utc),
        "subject": "Zillow inquiry for 138 Bullman St #144-A",
        "body": "I would like to schedule a tour.",
        "user_account_id": "nigel-zoho",
        "channel_id": 44,
        "source_channel_id": "zillow-thread-44",
        "channel_type": "email",
        "channel_name": "INBOX",
        "sender_participant_id": 55,
        "participant_type": "email_address",
        "participant_key": "AmandaSnyder@live.com",
        "display_name": "Amanda Snyder",
        "envelope": {
            "identity": {"factbook_entity_uuid": "aa1a1515-7929-4f17-a632-ec89c32f5895"},
            "message": {
                "prospect_name": "Amanda Snyder",
                "property": "138 Bullman St #144-A",
                "proxy_email": "amanda.abc@convo.zillow.com",
                "direct_email": "AmandaSnyder@live.com",
                "phone": "+1 (908) 555-0100",
            },
        },
        "raw_payload": {"provider": "zillow", "thread_id": "zrm-thread-44"},
    }
    values.update(overrides)
    return WakeEventRecord(**values)


def policy():
    return RoutingPolicy(
        version="appointment-v1",
        email_account_by_provider={
            "zillow": "nigel-zoho",
            "hotpads": "nigel-zoho",
            "tenantcloud": "nigel-zoho",
        },
        quo_line_by_provider={
            "quo": "leasing-main",
            "tenantcloud": "leasing-main",
            "zillow": "leasing-main",
        },
        calendar_by_profile={"appointment-setter": "nigel"},
        cliq_target_by_intent={"lead_alert": "tenant-leads"},
        property_aliases={
            "138 bullman street 144 a": "building:bullman-st",
            "144 bullman street": "building:bullman-st",
            "16 north main street 16": "building:16-n-main",
        },
        conversation_aliases={
            "zillow:zrm-thread-44": "conversation:zillow-amanda-bullman",
            "hotpads:zrm-thread-44": "conversation:zillow-amanda-bullman",
        },
    )


def request(**overrides) -> ExecuteRequest:
    payload = {
        "op": "execute",
        "wakeup_event_id": 12345,
        "action_role": "prospect_reply",
        "operation": "email.send",
        "intent_kind": "showing_offer",
        "appointment_slot": "2026-07-17T10:30:00-04:00",
        "arguments": {"to_address": "amanda.abc@convo.zillow.com", "text": "Friday at 10:30 works.\r\n— Nigel"},
    }
    payload.update(overrides)
    parsed = parse_outbound_request(payload)
    assert isinstance(parsed, ExecuteRequest)
    return parsed


def tenantcloud_record(*, family="lead", entity_ids=None, entity_scope_key=None, **overrides):
    values = {
        **record().__dict__,
        "event_source": "tenantcloud_claim",
        "message_source": "tenantcloud_api",
        "source_event_id": "tenantcloud:claim:301",
        "source_message_id": "tenantcloud:message:700",
        "source_channel_id": "tenantcloud:lead-thread:8001",
        "channel_type": "tenantcloud_lead",
        "raw_payload": {},
        "tenantcloud_claim_id": 301,
        "tenantcloud_claim_family": family,
        "tenantcloud_claim_state": "claimed",
        "tenantcloud_action_owner": "tenantcloud_api",
        "tenantcloud_entity_scope_key": entity_scope_key,
        "envelope": {
            "identity": {"factbook_entity_uuid": "aa1a1515-7929-4f17-a632-ec89c32f5895"},
            "message": {"direct_email": "tenant@example.com"},
            "tenantcloud": {
                "claim_id": 301,
                "action_owner": "tenantcloud_api",
                "claim_state": "claimed",
                "event_family": family,
                "entity_ids": entity_ids or {"lead_id": "6001", "listing_id": "5001", "thread_id": "8001"},
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def tenantcloud_request(operation):
    cases = {
        "tenantcloud.message.send": {
            "action_role": "prospect_reply",
            "intent_kind": "inquiry_reply",
            "arguments": {"thread_id": 8001, "text": "Thanks"},
        },
        "tenantcloud.lead.status.update": {
            "action_role": "provider_mutation",
            "intent_kind": "tenantcloud_lead_status",
            "arguments": {"lead_id": 6001, "status": "working"},
        },
        "tenantcloud.maintenance.create": {
            "action_role": "provider_mutation",
            "intent_kind": "tenantcloud_maintenance_create",
            "arguments": {
                "property_id": 12,
                "unit_id": 34,
                "category_id": 57,
                "title": "Kitchen leak",
                "priority": "normal",
                "initiated_at": "2026-08-04",
                "text": "Pipe is leaking\r\nunder sink",
                "entry_allowed": False,
                "available_on": "2026-08-05",
            },
        },
        "tenantcloud.maintenance.status.update": {
            "action_role": "provider_mutation",
            "intent_kind": "tenantcloud_maintenance_status",
            "arguments": {"request_id": 81, "status": 3},
        },
    }
    return request(operation=operation, appointment_slot=None, **cases[operation])


_TENANTCLOUD_HINT_KEYS = {"lead_id", "listing_id", "thread_id", "request_id", "property_id", "unit_id"}


def tenantcloud_hint_keys(hints):
    """suggest_targets() now also offers email/phone recipient hints
    alongside TenantCloud claim hints (Task 5). The TenantCloud-specific
    tests below only care about the claim-parsing half; this strips any
    to_address/to_phone keys so they can keep asserting on that half in
    isolation, exactly as they did before Task 5 added the rest."""
    return {key: value for key, value in hints.items() if key in _TENANTCLOUD_HINT_KEYS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "target_kind", "target_id"),
    [
        ("tenantcloud.message.send", "tenantcloud_thread", "8001"),
        ("tenantcloud.lead.status.update", "tenantcloud_lead", "6001"),
        ("tenantcloud.maintenance.create", "tenantcloud_property_unit", "property:12:unit:34"),
        ("tenantcloud.maintenance.status.update", "tenantcloud_maintenance_request", "81"),
    ],
)
async def test_tenantcloud_context_uses_agent_supplied_provider_targets(operation, target_kind, target_id):
    """The four TenantCloud operations take their target straight off the
    agent-supplied arguments -- the claim-linked entity_ids on the wake are
    no longer consulted for this. canonical_context/canonical_scope still
    carry the claim bookkeeping (tenantcloud_claim_id, source_event_id,
    desired_state_hash) that store.py and the adapter depend on."""
    event = tenantcloud_record()

    context = await ActionContextLoader(FakeRepository(event), policy()).load(tenantcloud_request(operation))

    assert context.target.kind == target_kind
    assert context.target.target_id == target_id
    assert context.target.verified is True
    assert context.provider_account == "tenantcloud"
    assert context.canonical_context["tenantcloud_claim_id"] == 301
    assert context.canonical_context["source_event_id"] == "tenantcloud:claim:301"
    assert context.canonical_context["operation_target"] == {
        "kind": target_kind,
        "target_id": target_id,
    }
    assert context.canonical_context["routing_policy_version"] == "appointment-v1"
    assert len(context.canonical_scope["desired_state_hash"]) == 64
    if operation == "tenantcloud.maintenance.create":
        assert context.canonical_context["provider_ids"] == {"property_id": "12", "unit_id": "34"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "entity_ids"),
    [
        ("lead", {"lead_id": "6001", "thread_id": "8001"}),
        ("maintenance", {"property_id": "12", "unit_id": "34", "thread_id": "8201"}),
        ("maintenance", {"request_id": "81", "property_id": "12", "unit_id": "34", "thread_id": "8201"}),
    ],
)
async def test_suggest_targets_returns_claim_linked_provider_ids(family, entity_ids):
    """suggest_targets() is the demoted, advisory home for the derivation
    that used to gate execute(): it still reads the wake's claim linkage,
    but only to hint values back to the agent -- never to reject a write."""
    channel = f"tenantcloud:{family}-thread:{entity_ids['thread_id']}"
    event = tenantcloud_record(
        family=family,
        entity_ids=entity_ids,
        source_channel_id=channel,
        channel_type=f"tenantcloud_{family}",
    )

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == entity_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_scope_key", "expected"),
    [
        ("tenantcloud:lead:1574439", {"lead_id": "1574439"}),
        ("tenantcloud:maintenance-request:1322814", {"request_id": "1322814"}),
        ("lead:2398947", {"lead_id": "2398947"}),
        ("tc-lead:2399346", {"lead_id": "2399346"}),
        ("tc:christy-moreno-317-s-main-apt-4", {}),
        ("tenantcloud-prospect:2398368", {"lead_id": "2398368"}),
        ("tenantcloud-lead:2398947", {"lead_id": "2398947"}),
        ("phone:17328017005", {}),
    ],
)
async def test_suggest_targets_parses_real_production_entity_scope_key_prefixes(entity_scope_key, expected):
    """FIX 1(b): tenantcloud_event_claims.entity_ids is populated for 0 of
    142 real production claims -- the ids actually live in
    entity_scope_key's `prefix:id` shape. These 8 cases are the real
    production entity_scope_key distribution verbatim: a lead or
    maintenance-request prefix (however it's spelled: bare, "tc-"
    abbreviated, "tenantcloud-" hyphenated, or "prospect" as a lead
    synonym) parses its numeric tail; a property-address slug or a phone
    number contributes nothing, even though the phone case's tail happens
    to be all-digits -- the prefix has to actually name a lead or
    maintenance request. entity_ids is deliberately left empty here (an
    unrelated key that fails the allowed-keys check) so the asserted value
    is entity_scope_key's contribution alone, not a merge artifact."""
    event = tenantcloud_record(entity_scope_key=entity_scope_key, entity_ids={"unrelated_key": "1"})

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == expected


@pytest.mark.asyncio
async def test_suggest_targets_is_advisory_and_never_raises():
    """A wake with no TenantCloud claim linkage at all yields {}, not an
    error -- suggest_targets() must never reject."""
    event = record()  # ordinary zoho_mail wake, no claim linkage whatsoever

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_overrides",
    [
        {"tenantcloud_claim_id": None},  # claim identity missing
        {"tenantcloud_claim_id": True},  # claim identity wrong type
        {"tenantcloud_claim_id": 302},  # claim identity disagrees with envelope's 301
        {"tenantcloud_claim_family": "maintenance"},  # family (disagrees with envelope's "lead")
    ],
)
async def test_suggest_targets_is_empty_for_untrusted_claim_and_channel_shapes(record_overrides):
    """FIX 1(a) removed the owner/state/source/channel gating (see the
    ignores-ownership-state-source-and-channel test below), but these four
    remain {} because they are id-*correctness* checks, not ownership
    checks: a missing/malformed claim id, or a claim id that disagrees with
    the envelope's own claim linkage, means the entity_ids we'd be reading
    might not even belong to this claim -- and a family mismatch means
    entity_ids' keys (lead_id/listing_id) don't belong to the claim's own
    family's allowed key set. Emitting an id here would risk emitting the
    *wrong* id, which suggest_targets() must never do even though it's
    advisory."""
    event = tenantcloud_record(**record_overrides)

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_overrides",
    [
        {"event_source": "zoho_mail"},  # source
        {"event_source": "tenantcloud"},  # source
        {"raw_payload": {"provider": "zillow"}},  # provider
        {"message_source": "zoho_mail"},  # provider
        {"tenantcloud_claim_state": "pending"},  # state
        {"tenantcloud_action_owner": "legacy"},  # owner
        {"source_channel_id": "tenantcloud:lead-thread:9999"},  # thread vs. entity_ids "conflict"
    ],
)
async def test_suggest_targets_ignores_ownership_state_source_and_channel_shape(record_overrides):
    """FIX 1(a): these 7 shapes used to all yield {} under the retired
    execute-blocking gate (see the CHANGED test below -- this is what those
    same 7 cases were split out of). suggest_targets() is read-only and
    advisory, so a legacy-owned, non-active, differently-sourced, or
    differently-channeled claim is just as safe to hint from as a live
    tenantcloud_api-owned one -- in production 120 of 142 real claims are
    legacy-owned or inactive and were getting silently swallowed to {} by
    this exact gating. None of these touch claim identity or family, so the
    full claim-linked entity_ids dict is still returned.

    CHANGED: split out of test_suggest_targets_is_empty_for_untrusted_claim_and_channel_shapes,
    which previously asserted {} for all 11 of these cases combined. 7 of the
    11 (all of these) are now legitimately parseable once the ownership/
    state/source/channel gating is gone, so they assert the parsed value
    instead of {}; the remaining 4 (claim identity/family) keep asserting {}
    under the original test name just above."""
    event = tenantcloud_record(**record_overrides)

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == {"lead_id": "6001", "listing_id": "5001", "thread_id": "8001"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "entity_ids"),
    [
        ("lead", {"lead_id": True, "thread_id": "8001"}),
        ("lead", {"lead_id": "0", "thread_id": "8001"}),
        ("lead", {"lead_id": "6001 or 6002", "thread_id": "8001"}),
        ("lead", {"lead_id": "9223372036854775808", "thread_id": "8001"}),
        ("lead", {"lead_id": "6001", "leadId": "6002", "thread_id": "8001"}),
        ("lead", {"request_id": "81", "thread_id": "8001"}),  # request_id is a maintenance-only key
    ],
)
async def test_suggest_targets_is_empty_for_malformed_or_cross_family_entity_ids(family, entity_ids):
    """Type/format/overflow/alias-conflict checks on the claim's own
    entity_ids -- unlike the deleted per-operation "required ids for this
    operation" checks (suggest_targets doesn't know the operation), these
    are wake-shape self-consistency checks and still hold."""
    event = tenantcloud_record(
        family=family,
        entity_ids=entity_ids,
        source_channel_id=f"tenantcloud:{family}-thread:{entity_ids.get('thread_id', '8001')}",
        channel_type=f"tenantcloud_{family}",
    )

    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)

    assert tenantcloud_hint_keys(hints) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("event_source", ["tenantcloud_api", "tenantcloud_claim"])
async def test_tenantcloud_context_accepts_production_wake_sources(event_source):
    event = tenantcloud_record(event_source=event_source)

    context = await ActionContextLoader(FakeRepository(event), policy()).load(tenantcloud_request("tenantcloud.lead.status.update"))

    assert context.provider_account == "tenantcloud"
    assert context.canonical_context["tenantcloud_claim_id"] == 301


@pytest.mark.asyncio
async def test_tenantcloud_operation_allowlist_keys_on_operation_not_wake_shape():
    """The provider used to gate enabled_operations_by_provider must be the
    operation's own provider ("tenantcloud" -- it's right there in the
    operation name), not whatever _provider() infers from the wake's
    message shape. Otherwise a TenantCloud execute on a non-TenantCloud-
    shaped wake (e.g. an ordinary email wake) gets rejected by the
    allowlist gate -- the exact wake-shape coupling this task removes,
    surviving one layer up."""
    restricted = replace(
        policy(),
        enabled_operations_by_provider={"tenantcloud": frozenset({"tenantcloud.lead.status.update"})},
    )
    event = record(  # ordinary zoho_mail / email_thread wake -- no TenantCloud shape at all
        wakeup_event_id=1,
        event_source="zoho_mail",
        message_source="zoho_mail",
        channel_type="email_thread",
        raw_payload={},
        envelope={
            "identity": {"factbook_entity_uuid": "aa1a1515-7929-4f17-a632-ec89c32f5895"},
            "message": {"direct_email": "tenant@example.com"},
        },
    )
    exec_request = parse_outbound_request(
        {
            "op": "execute",
            "wakeup_event_id": 1,
            "action_role": "provider_mutation",
            "operation": "tenantcloud.lead.status.update",
            "intent_kind": "tenantcloud_lead_status",
            "appointment_slot": None,
            "arguments": {"lead_id": 2405115, "status": "working"},
        }
    )

    context = await ActionContextLoader(FakeRepository(event), restricted).load(exec_request)

    assert context.target.target_id == "2405115"
    assert context.provider_account == "tenantcloud"


@pytest.mark.asyncio
async def test_tenantcloud_claim_bookkeeping_never_bakes_the_literal_string_none():
    """A claim-less TenantCloud wake must not leave a Python `None` baked
    into canonical_context/canonical_scope's tenantcloud_claim_id: those
    values get f-string-interpolated into persisted, immutable identity
    strings (store.py's lock_intent, tenantcloud_shared's idempotency key),
    where a literal "None" substring would be baked in forever."""
    event = record(  # ordinary wake, no TenantCloud claim linkage at all
        wakeup_event_id=1,
        event_source="tenantcloud_claim",
        message_source="zoho_mail",
        channel_type="email_thread",
    )
    request = parse_outbound_request(
        {
            "op": "execute",
            "wakeup_event_id": 1,
            "action_role": "provider_mutation",
            "operation": "tenantcloud.lead.status.update",
            "intent_kind": "tenantcloud_lead_status",
            "appointment_slot": None,
            "arguments": {"lead_id": 2405115, "status": "working"},
        }
    )
    assert event.tenantcloud_claim_id is None

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request)

    assert context.canonical_context["tenantcloud_claim_id"] == ""
    assert context.canonical_scope["tenantcloud_claim_id"] == ""
    assert "None" not in tenantcloud_idempotency_key(context)


@pytest.mark.asyncio
async def test_tenantcloud_target_comes_from_arguments_on_any_wake_shape():
    event = record(  # ordinary zoho_mail / email_thread wake, no claim linkage
        wakeup_event_id=1,
        event_source="tenantcloud_claim",
        message_source="zoho_mail",
        channel_type="email_thread",
    )
    request = parse_outbound_request(
        {
            "op": "execute",
            "wakeup_event_id": 1,
            "action_role": "provider_mutation",
            "operation": "tenantcloud.lead.status.update",
            "intent_kind": "tenantcloud_lead_status",
            "appointment_slot": None,
            "arguments": {"lead_id": 2405115, "status": "working"},
        }
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request)

    assert context.target.kind == "tenantcloud_lead"
    assert context.target.target_id == "2405115"
    assert context.target.verified is True
    assert context.provider_account == "tenantcloud"


@pytest.mark.asyncio
async def test_execute_ignores_a_target_that_disagrees_with_the_wake():
    """The wake's own claim says lead 999; the agent says 2405115. The agent wins."""
    event = tenantcloud_record(wakeup_event_id=1, entity_ids={"lead_id": "999"})
    request = parse_outbound_request(
        {
            "op": "execute",
            "wakeup_event_id": 1,
            "action_role": "provider_mutation",
            "operation": "tenantcloud.lead.status.update",
            "intent_kind": "tenantcloud_lead_status",
            "appointment_slot": None,
            "arguments": {"lead_id": 2405115, "status": "working"},
        }
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request)

    assert context.target.target_id == "2405115"


@pytest.mark.asyncio
async def test_tenantcloud_provider_mutation_needs_no_unrelated_prospect_alias():
    event = tenantcloud_record(
        family="maintenance",
        entity_ids={"request_id": "81"},
        source_channel_id="tenantcloud:maintenance-request:81",
        channel_type="tenantcloud_maintenance",
        participant_type=None,
        participant_key=None,
        envelope={
            "identity": {},
            "message": {},
            "tenantcloud": {
                "claim_id": 301,
                "action_owner": "tenantcloud_api",
                "claim_state": "claimed",
                "event_family": "maintenance",
                "entity_ids": {"request_id": "81"},
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(tenantcloud_request("tenantcloud.maintenance.status.update"))

    assert context.prospect_id == "tenantcloud:claim:301"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "action", "target_kind", "target_id", "provider_account"),
    [
        (
            record(),
            request(arguments={"to_address": "agent-chosen@example.com", "text": "Friday at 10:30 works.\r\n\u2014 Nigel"}),
            "email_thread",
            "agent-chosen@example.com",
            "nigel-zoho",
        ),
        (
            record(
                raw_payload={"provider": "hotpads", "thread_id": "zrm-thread-44"},
                participant_key="lead.123@convo.zillow.com",
                envelope={
                    "identity": {},
                    "message": {
                        "property": "144 Bullman Street",
                        "proxy_email": "lead.123@convo.zillow.com",
                        "direct_email": "AmandaSnyder@live.com",
                    },
                },
            ),
            request(arguments={"to_address": "agent-chosen@example.com", "text": "Friday at 10:30 works.\r\n\u2014 Nigel"}),
            "email_thread",
            "agent-chosen@example.com",
            "nigel-zoho",
        ),
        (
            record(
                event_source="quo",
                message_source="quo",
                source_channel_id="quo-conversation-9",
                channel_type="sms",
                participant_type="phone",
                participant_key="+19085550199",
                raw_payload={"provider": "quo", "conversation_id": "quo-conversation-9"},
                envelope={
                    "identity": {},
                    "message": {"property": "16 N Main St #16", "phone": "+1 908 555 0199"},
                },
            ),
            request(
                operation="quo.sms.send",
                intent_kind="inquiry_reply",
                appointment_slot=None,
                arguments={"to_phone": "+19085551234", "text": "Thanks"},
            ),
            "quo_conversation",
            "+19085551234",
            "leasing-main",
        ),
        (
            record(
                event_source="zoho_cliq",
                message_source="zoho_cliq",
                source_channel_id="tenant-leads",
                channel_type="channel",
                participant_type="user",
                participant_key="internal-user",
                raw_payload={"provider": "cliq", "channel_id": "tenant-leads"},
                envelope={"identity": {}, "message": {}},
            ),
            request(
                action_role="internal_notification",
                operation="cliq.channel.post",
                intent_kind="lead_alert",
                appointment_slot=None,
                arguments={"channel_or_chat_id": "agent-chosen-channel", "text": "New lead"},
            ),
            "cliq_channel",
            "agent-chosen-channel",
            "agent-chosen-channel",
        ),
        (
            record(raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"}),
            request(
                action_role="calendar_mutation",
                operation="calendar.create",
                intent_kind="showing_create",
                arguments={"calendar_id": "nigel", "description": "Amanda tour"},
            ),
            "calendar",
            "nigel",
            "nigel",
        ),
    ],
)
async def test_execute_target_comes_from_agent_arguments_regardless_of_wake_shape(event, action, target_kind, target_id, provider_account):
    """Repoints the old test_context_derives_provider_targets_server_side:
    that test asserted target *values* the retired wake-side derivation
    produced. Now the agent supplies the value directly -- these fixtures
    keep the same wake shapes (including ones whose own data implies a
    *different* address/phone/channel) to prove the agent's argument wins
    every time, not just when it happens to agree with the wake."""
    event = WakeEventRecord(**{**event.__dict__, "wakeup_event_id": action.wakeup_event_id})
    context = await ActionContextLoader(FakeRepository(event), policy()).load(action)
    assert context.target.kind == target_kind
    assert context.target.target_id == target_id
    assert context.target.verified is True
    assert context.provider_account == provider_account
    assert context.lock_holder == f"outbound-gateway:{context.action_id}"
    assert context.source_message_id == event.message_id
    assert context.conversation_watermark == 700
    assert len(context.payload_hash) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected_hints"),
    [
        (record(), {"to_address": "amanda.abc@convo.zillow.com", "to_phone": "+19085550100"}),
        (
            record(
                raw_payload={"provider": "hotpads", "thread_id": "zrm-thread-44"},
                participant_key="lead.123@convo.zillow.com",
                envelope={
                    "identity": {},
                    "message": {
                        "property": "144 Bullman Street",
                        "proxy_email": "lead.123@convo.zillow.com",
                        "direct_email": "AmandaSnyder@live.com",
                    },
                },
            ),
            {"to_address": "lead.123@convo.zillow.com"},
        ),
        (
            record(
                raw_payload={"provider": "tenantcloud", "thread_id": "tc-lead-1"},
                source_channel_id="tenantcloud-lead-1",
                participant_key="tenant@example.com",
                envelope={
                    "identity": {},
                    "message": {
                        "property": "16 N Main St #16",
                        "direct_email": "tenant@example.com",
                    },
                },
            ),
            {"to_address": "tenant@example.com"},
        ),
        (
            record(
                event_source="quo",
                message_source="quo",
                source_channel_id="quo-conversation-9",
                channel_type="sms",
                participant_type="phone",
                participant_key="+19085550199",
                raw_payload={"provider": "quo", "conversation_id": "quo-conversation-9"},
                envelope={
                    "identity": {},
                    "message": {"property": "16 N Main St #16", "phone": "+1 908 555 0199"},
                },
            ),
            {"to_phone": "+19085550199"},
        ),
    ],
)
async def test_suggest_targets_returns_the_recipient_the_old_derivation_would_have_produced(event, expected_hints):
    """These are the exact wake fixtures the retired execute-gating
    derivation used to enforce for EMAIL_SEND/QUO_SMS_SEND.
    suggest_targets() is the demoted, advisory home for that same
    resolution -- it must keep producing the same values, just as a hint
    instead of a gate."""
    loader = ActionContextLoader(FakeRepository(event), policy())
    hints = await loader.suggest_targets(event.wakeup_event_id)
    for key, value in expected_hints.items():
        assert hints[key] == value


@pytest.mark.asyncio
async def test_suggest_targets_has_no_cliq_or_calendar_hint_since_routing_is_config_not_wake_derived():
    """Cliq channel/chat and calendar selection were never derived from wake
    content -- they were static operator config keyed by intent/profile, the
    same for every wake regardless of what it contains. There is nothing for
    suggest_targets to offer here; the agent supplies channel_or_chat_id and
    calendar_id directly."""
    event = record(
        event_source="zoho_cliq",
        message_source="zoho_cliq",
        source_channel_id="tenant-leads",
        channel_type="channel",
        participant_type="user",
        participant_key="internal-user",
        raw_payload={"provider": "cliq", "channel_id": "tenant-leads"},
        envelope={"identity": {}, "message": {}},
    )
    hints = await ActionContextLoader(FakeRepository(event), policy()).suggest_targets(event.wakeup_event_id)
    assert "channel_or_chat_id" not in hints
    assert "calendar_id" not in hints


@pytest.mark.asyncio
async def test_suggest_returns_provider_routing_capabilities_and_target_hints():
    event = record()
    restricted = replace(
        policy(),
        enabled_operations_by_provider={
            "zillow": frozenset({"email.send", "quo.sms.send"}),
        },
        enabled_intents=frozenset({"inquiry_reply", "showing_offer", "showing_confirmation"}),
        enabled_intents_by_provider={
            "zillow": frozenset({"inquiry_reply", "showing_offer"}),
        },
    )

    result = await ActionContextLoader(FakeRepository(event), restricted).suggest(event.wakeup_event_id)

    assert result == {
        "provider": "zillow",
        "suggestions": {"to_address": "amanda.abc@convo.zillow.com", "to_phone": "+19085550100"},
        "enabled_operations": ["email.send", "quo.sms.send"],
        "enabled_intents": ["inquiry_reply", "showing_offer"],
    }


@pytest.mark.asyncio
async def test_suggest_returns_no_provider_capabilities_or_hints_for_missing_or_providerless_wakes():
    missing = await ActionContextLoader(FakeRepository(record()), policy()).suggest(99999)
    providerless = record(
        event_source="",
        message_source="",
        raw_payload={},
        envelope={"identity": {}, "message": {"direct_email": "secret@example.com", "phone": "+19085550111"}},
    )
    no_provider = await ActionContextLoader(FakeRepository(providerless), policy()).suggest(providerless.wakeup_event_id)

    expected = {
        "provider": None,
        "suggestions": {},
        "enabled_operations": [],
        "enabled_intents": [],
    }
    assert missing == expected
    assert no_provider == expected


@pytest.mark.asyncio
async def test_rollout_policy_rejects_cross_channel_provider_route():
    restricted = replace(
        policy(),
        enabled_operations_by_provider={
            "zillow": frozenset({"email.send"}),
            "hotpads": frozenset({"email.send"}),
            "quo": frozenset({"quo.sms.send"}),
        },
        enabled_intents=frozenset({"inquiry_reply", "showing_offer"}),
    )
    tenantcloud = record(
        raw_payload={"provider": "tenantcloud", "thread_id": "tc-lead-1"},
        participant_key="+19085550199",
        participant_type="phone",
        channel_type="sms",
        envelope={
            "identity": {},
            "message": {
                "property": "16 N Main St #16",
                "phone": "+1 908 555 0199",
            },
        },
    )

    with pytest.raises(ContextDerivationError, match="provider operation is disabled"):
        await ActionContextLoader(FakeRepository(tenantcloud), restricted).load(
            request(operation="quo.sms.send", arguments={"to_phone": "+19085550199", "text": "Friday at 10:30 works.\r\n— Nigel"})
        )


@pytest.mark.asyncio
async def test_rollout_policy_rejects_unapproved_intent():
    restricted = replace(
        policy(),
        enabled_operations_by_provider={"zillow": frozenset({"email.send"})},
        enabled_intents=frozenset({"inquiry_reply", "showing_offer"}),
    )

    with pytest.raises(ContextDerivationError, match="intent is disabled"):
        await ActionContextLoader(FakeRepository(record()), restricted).load(request(intent_kind="showing_confirmation"))


@pytest.mark.asyncio
async def test_participant_only_zillow_proxy_drives_thread_identity_and_matches_suggest():
    """thread_identity/conversation_id bucketing is unrelated to target
    selection and stays wake-derived. The recipient itself now comes from
    the agent's arguments (repointed from the old direct target assertion);
    suggest_targets is checked to still surface the same address as a hint."""
    event = record(
        message_source="zoho_mail",
        source_channel_id="INBOX",
        channel_type="email_thread",
        participant_type="email_address",
        participant_key="relay-only@convo.zillow.com",
        raw_payload={},
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "Amanda Snyder",
                "property": "138 Bullman St #144-A",
            },
        },
    )
    loader = ActionContextLoader(FakeRepository(event), policy())

    context = await loader.load(request(arguments={"to_address": "relay-only@convo.zillow.com", "text": "Friday at 10:30 works.\r\n— Nigel"}))
    hints = await loader.suggest_targets(event.wakeup_event_id)

    assert context.source == "zillow"
    assert context.target.target_id == "relay-only@convo.zillow.com"
    assert context.thread_identity == "relay-only@convo.zillow.com"
    assert context.conversation_id == "conversation:zillow:relay-only@convo.zillow.com"
    assert hints["to_address"] == "relay-only@convo.zillow.com"


@pytest.mark.asyncio
async def test_zillow_email_property_uses_matching_nearby_proxy_thread():
    event = record(
        subject=("Kailani is requesting information about 138 Bullman St #144-A, Phillipsburg, NJ, 08865"),
        participant_key="kailani.abc@convo.zillow.com",
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "Kailani Deleon",
                "proxy_email": "kailani.abc@convo.zillow.com",
            },
            "conversation_context": {
                "nearby_messages": [
                    {
                        "property": "16 N Main St #16",
                        "proxy_email": "different.abc@convo.zillow.com",
                    },
                    {
                        "property": "138 Bullman St #144-A",
                        "proxy_email": "kailani.abc@convo.zillow.com",
                    },
                ]
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.property_label == "138 Bullman St #144-A"
    assert context.property_id == "building:bullman-st"


@pytest.mark.asyncio
async def test_zillow_information_about_subject_derives_listing_address():
    event = record(
        subject=("Kailani is requesting information about 138 Bullman St #144-A, Phillipsburg, NJ, 08865"),
        participant_key="kailani.abc@convo.zillow.com",
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "Kailani Deleon",
                "proxy_email": "kailani.abc@convo.zillow.com",
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.property_label == "138 Bullman St #144-A"
    assert context.property_id == "building:bullman-st"


@pytest.mark.asyncio
async def test_zillow_wake_with_no_proxy_offers_no_email_suggestion_but_execute_still_honors_the_agent():
    """The retired derivation required a Zillow reply to go to the rotating
    @convo.zillow.com proxy; a generic participant address (a plain gmail
    one) produced no safe target and rejected the execute outright. That
    ownership-style gate is gone: suggest_targets faithfully reports it has
    nothing to offer (no to_address hint) for this wake, but the agent's own
    choice of recipient is honored by execute regardless."""
    event = record(
        participant_type="email_address",
        participant_key="prospect@gmail.com",
        raw_payload={"provider": "zillow"},
        envelope={
            "identity": {},
            "message": {"property": "138 Bullman St #144-A"},
        },
    )
    loader = ActionContextLoader(FakeRepository(event), policy())

    hints = await loader.suggest_targets(event.wakeup_event_id)
    context = await loader.load(request(arguments={"to_address": "prospect@gmail.com", "text": "Friday at 10:30 works.\r\n— Nigel"}))

    assert "to_address" not in hints
    assert context.target.target_id == "prospect@gmail.com"
    assert context.target.verified is True


@pytest.mark.asyncio
async def test_live_shape_quo_phone_number_and_nested_conversation_are_canonical():
    event = record(
        event_source="quo",
        message_source="quo",
        source_channel_id="leasing-line-channel",
        channel_type="phone_number",
        participant_type="phone_number",
        participant_key="+19085550199",
        raw_payload={
            "data": {
                "object": {
                    "conversationId": "quo-conversation-live",
                    "phoneNumberId": "leasing-main",
                    "direction": "incoming",
                    "from": "+19085550199",
                    "to": "+19085550000",
                }
            }
        },
        envelope={
            "identity": {},
            "message": {"property": "16 N Main St #16"},
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085550199", "text": "Thanks"},
        )
    )

    assert context.thread_identity == "quo-conversation-live"
    assert context.conversation_id == "conversation:quo:quo-conversation-live"
    assert context.recipient_phone == "+19085550199"
    assert "phone:+19085550199" in context.aliases
    assert context.target.verified is True


@pytest.mark.asyncio
async def test_zillow_linked_missed_call_can_use_server_owned_quo_route():
    event = record(
        wakeup_event_id=23000,
        event_source="zillow_rm_web_extract",
        source_event_id="zrm-event:synthetic-linked-call",
        message_id=197184,
        message_source="zillow_rm_web_extract",
        source_message_id="zrm-msg:synthetic-linked-call",
        source_channel_id="zrm-thread:synthetic-linked-call",
        channel_type="zillow_rm_thread",
        participant_type="phone_number",
        participant_key="+19085550140",
        display_name="Missed Call Prospect",
        raw_payload={
            "source": "zillow_rm_web_extract",
            "phone": "+19085550140",
            "message_kind": "zillow_rm_call_recording",
            "related_call": {
                "call_id": 2549,
                "source_call_id": "synthetic-provider-call-2549",
                "to_number": "+17623726083",
            },
        },
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "Missed Call Prospect",
                "property": "138 Test St #1",
                "phone": "+19085550140",
                "proxy_email": None,
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            wakeup_event_id=23000,
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085550140", "text": "Hi, we missed your call. How can we help? — Nigel"},
        )
    )

    assert context.source == "zillow"
    assert context.target.kind == "quo_conversation"
    assert context.target.target_id == "+19085550140"
    assert context.target.verified is True
    assert context.provider_account == "leasing-main"
    assert context.recipient_phone == "+19085550140"


@pytest.mark.asyncio
async def test_quo_inbound_uses_observed_receiving_line_over_default_route():
    event = record(
        event_source="quo",
        message_source="quo",
        channel_type="phone_number",
        participant_type="phone_number",
        participant_key="+19085550199",
        raw_payload={
            "data": {
                "object": {
                    "conversationId": "quo-conversation-live",
                    "phoneNumberId": "different-line",
                    "direction": "incoming",
                    "from": "+19085550199",
                }
            }
        },
        envelope={"identity": {}, "message": {"property": "16 N Main St #16"}},
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085550199", "text": "Thanks"},
        )
    )

    # The phoneNumberId arrived from the Quo webhook, not agent input.  Reply
    # from that receiving line so multi-line inbound threads remain replyable.
    assert context.target.verified is True
    assert context.provider_account == "different-line"


@pytest.mark.asyncio
async def test_quo_phase_route_allows_inquiry_reply_but_not_propertyless_showing_offer():
    restricted = replace(
        policy(),
        enabled_intents_by_provider={
            "zillow": frozenset({"inquiry_reply", "showing_offer"}),
            "quo": frozenset({"inquiry_reply"}),
        },
    )
    event = record(
        event_source="quo",
        message_source="quo",
        channel_type="phone_number",
        participant_type="phone_number",
        participant_key="+19085550199",
        subject=None,
        raw_payload={
            "data": {
                "object": {
                    "conversationId": "quo-conversation-live",
                    "phoneNumberId": "leasing-main",
                    "direction": "incoming",
                    "from": "+19085550199",
                }
            }
        },
        envelope={"identity": {}, "message": {}},
    )

    inquiry = await ActionContextLoader(FakeRepository(event), restricted).load(
        request(
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085550199", "text": "Thanks"},
        )
    )
    assert inquiry.intent_kind.value == "inquiry_reply"
    with pytest.raises(ContextDerivationError, match="provider intent is disabled"):
        await ActionContextLoader(FakeRepository(event), restricted).load(
            request(operation="quo.sms.send", arguments={"to_phone": "+19085550199", "text": "Thanks"})
        )


@pytest.mark.asyncio
async def test_action_identity_and_payload_hash_are_canonical_and_stable():
    event = record()
    repo = FakeRepository(event)
    loader = ActionContextLoader(repo, policy())
    first = await loader.load(request())
    second = await loader.load(request(arguments={"to_address": "amanda.abc@convo.zillow.com", "text": "Friday at 10:30 works.\n— Nigel"}))
    assert first.action_id == second.action_id
    assert first.action_id.version == 5
    assert first.payload_hash == second.payload_hash
    assert first.arguments == {"to_address": "amanda.abc@convo.zillow.com", "text": "Friday at 10:30 works.\n— Nigel"}
    assert tuple(sorted(first.aliases)) == first.aliases


@pytest.mark.asyncio
async def test_payload_hash_canonicalizes_equivalent_slot_offsets():
    loader = ActionContextLoader(FakeRepository(record()), policy())

    eastern = await loader.load(request(appointment_slot="2026-07-17T10:30:00-04:00"))
    utc = await loader.load(request(appointment_slot="2026-07-17T14:30:00Z"))

    assert eastern.appointment_slot == utc.appointment_slot
    assert eastern.payload_hash == utc.payload_hash


@pytest.mark.asyncio
async def test_near_simultaneous_cross_channel_duplicate_is_canonicalized():
    event = record(
        message_source="zillow_rm_web_extract",
        message_sent_at=datetime(2026, 7, 15, 22, 26, tzinfo=timezone.utc),
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "Kailani Deleon",
                "property": "138 Bullman St #144-A",
                "proxy_email": "kailani.abc@convo.zillow.com",
            },
            "routing_hints": {
                "potential_cross_channel_duplicate": {
                    "duplicate_of_message_id": 196337,
                    "duplicate_of_source": "zoho_mail",
                    "duplicate_of_sent_at": "2026-07-15 22:26:10+00:00",
                }
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.cross_channel_duplicate_message_ids == (196337,)
    assert context.canonical_context["cross_channel_duplicate_message_ids"] == [196337]


@pytest.mark.asyncio
async def test_replay_refresh_certifies_older_zillow_scrape_rows():
    event = record(
        event_source="outbound-replay",
        raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"},
        envelope={
            "identity": {},
            "message": {
                "property": "138 Bullman St #144-A",
                "proxy_email": "amanda.abc@convo.zillow.com",
            },
            "zillow_refresh": {
                "status": "covered",
                "covered_through": "2026-07-16T23:00:00Z",
                "covered_thread_identity": "Amanda Snyder|138 Bullman St #144-A",
                "evidence_sha256": "a" * 64,
                "certified_older_message_ids": [197065, 197067],
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.certified_older_message_ids == (197065, 197067)
    assert context.canonical_context["certified_older_message_ids"] == [197065, 197067]


@pytest.mark.asyncio
async def test_non_replay_event_cannot_certify_older_zillow_scrape_rows():
    event = record(
        envelope={
            "identity": {},
            "message": {
                "property": "138 Bullman St #144-A",
                "proxy_email": "amanda.abc@convo.zillow.com",
            },
            "zillow_refresh": {
                "status": "covered",
                "covered_through": "2026-07-16T23:00:00Z",
                "covered_thread_identity": "Amanda Snyder|138 Bullman St #144-A",
                "evidence_sha256": "a" * 64,
                "certified_older_message_ids": [197065],
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.certified_older_message_ids == ()
    assert context.canonical_context["certified_older_message_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "certified_ids",
    ([197067, 197065], [197065, 197065], [True], [0]),
)
async def test_replay_rejects_invalid_certified_zillow_chronology(certified_ids):
    event = record(
        event_source="outbound-replay",
        raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"},
        envelope={
            "identity": {},
            "message": {
                "property": "138 Bullman St #144-A",
                "proxy_email": "amanda.abc@convo.zillow.com",
            },
            "zillow_refresh": {
                "status": "covered",
                "covered_through": "2026-07-16T23:00:00Z",
                "covered_thread_identity": "Amanda Snyder|138 Bullman St #144-A",
                "evidence_sha256": "a" * 64,
                "certified_older_message_ids": certified_ids,
            },
        },
    )

    with pytest.raises(
        ContextDerivationError,
        match="invalid certified Zillow chronology evidence",
    ):
        await ActionContextLoader(FakeRepository(event), policy()).load(request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duplicate_source", "duplicate_sent_at", "duplicate_message_id"),
    [
        ("zillow_rm_web_extract", "2026-07-15T22:26:10Z", 196337),
        ("zoho_mail", "2026-07-15T22:29:01Z", 196337),
        ("zoho_mail", "not-a-time", 196337),
        ("zoho_mail", "2026-07-15T22:26:10Z", True),
    ],
)
async def test_unverified_cross_channel_duplicate_hint_is_ignored(
    duplicate_source,
    duplicate_sent_at,
    duplicate_message_id,
):
    event = record(
        message_source="zillow_rm_web_extract",
        message_sent_at=datetime(2026, 7, 15, 22, 26, tzinfo=timezone.utc),
        envelope={
            "identity": {},
            "message": {
                "property": "138 Bullman St #144-A",
                "proxy_email": "kailani.abc@convo.zillow.com",
            },
            "routing_hints": {
                "potential_cross_channel_duplicate": {
                    "duplicate_of_message_id": duplicate_message_id,
                    "duplicate_of_source": duplicate_source,
                    "duplicate_of_sent_at": duplicate_sent_at,
                }
            },
        },
    )

    context = await ActionContextLoader(FakeRepository(event), policy()).load(request())

    assert context.cross_channel_duplicate_message_ids == ()
    assert context.canonical_context["cross_channel_duplicate_message_ids"] == []


@pytest.mark.asyncio
async def test_duplicate_provider_and_property_aliases_converge():
    first_repo = FakeRepository(record(), canonical_subject="prospect:amanda")
    second_repo = FakeRepository(
        record(
            raw_payload={"provider": "hotpads", "thread_id": "zrm-thread-44"},
            envelope={
                "identity": {},
                "message": {
                    "property": "144 Bullman Street",
                    "proxy_email": "amanda.abc@convo.zillow.com",
                    "direct_email": "AmandaSnyder@live.com",
                },
            },
        ),
        canonical_subject="prospect:amanda",
    )
    first = await ActionContextLoader(first_repo, policy()).load(request())
    second = await ActionContextLoader(second_repo, policy()).load(request())
    assert first.prospect_id == second.prospect_id == "prospect:amanda"
    assert first.property_id == second.property_id == "building:bullman-st"
    assert first.conversation_id == second.conversation_id


@pytest.mark.asyncio
async def test_ambiguous_aliases_still_fail_closed_but_unresolvable_wake_no_longer_blocks_execute():
    """Alias ambiguity is unrelated to target selection and still fails
    closed. The second half used to also fail closed when the wake itself
    carried no usable email anywhere (participant_key="unknown", no proxy/
    direct email) -- that was the retired target-derivation gate. Now the
    agent's own address is all that is required; suggest_targets honestly
    has nothing to offer, but execute is unaffected."""
    with pytest.raises(ContextDerivationError, match="ambiguous"):
        await ActionContextLoader(FakeRepository(record(), ambiguous=True), policy()).load(request())
    unresolvable = record(
        participant_key="unknown",
        envelope={
            "identity": {"factbook_entity_uuid": "aa1a1515-7929-4f17-a632-ec89c32f5895"},
            "message": {"property": "144 Bullman Street"},
        },
    )
    loader = ActionContextLoader(FakeRepository(unresolvable), policy())

    hints = await loader.suggest_targets(unresolvable.wakeup_event_id)
    context = await loader.load(request(arguments={"to_address": "agent-chosen@example.com", "text": "Friday at 10:30 works.\r\n— Nigel"}))

    assert "to_address" not in hints
    assert context.target.target_id == "agent-chosen@example.com"
    assert context.target.verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_address",
    (
        "no-reply@comet.zillow.com",
        "noreply@tenantcloud.com",
        "postmaster@example.com",
    ),
)
async def test_system_sender_offers_no_suggestion_but_execute_still_honors_the_agent(unsafe_address):
    """The retired derivation refused to let a system/no-reply address (or,
    for Zillow, any non-@convo.zillow.com address) become the target.
    suggest_targets still reports nothing usable here, but execute no
    longer gates on it -- the agent's own choice of recipient controls,
    even one an operator might consider unwise. That is the point of this
    task: the gateway validates format, not judgement."""
    unsafe = record(
        participant_key=unsafe_address,
        envelope={
            "identity": {"factbook_entity_uuid": "aa1a1515-7929-4f17-a632-ec89c32f5895"},
            "message": {
                "property": "138 Bullman St #144-A",
                "direct_email": unsafe_address,
            },
        },
    )
    loader = ActionContextLoader(FakeRepository(unsafe), policy())

    hints = await loader.suggest_targets(unsafe.wakeup_event_id)
    context = await loader.load(request(arguments={"to_address": unsafe_address, "text": "Friday at 10:30 works.\r\n— Nigel"}))

    assert "to_address" not in hints
    assert context.target.target_id == unsafe_address
    assert context.target.verified is True


@pytest.mark.asyncio
async def test_repository_uses_parameterized_queries_for_event_and_alias_reads():
    class Row:
        def __init__(self, cells):
            self.cells = cells

    event = record()
    calls = []

    async def execute(_driver, query, params):
        calls.append((query, params))
        if "FROM hermes_wakeup_events" in query:
            return [Row(event.__dict__)]
        return [Row({"subject_count": 1, "canonical_subject": "prospect:canonical"})]

    repository = OutboundGatewayRepository(object())
    with patch(
        "postgres_mcp.outbound_gateway.repository.SafeSqlDriver.execute_param_query",
        AsyncMock(side_effect=execute),
    ):
        loaded = await repository.load_wake_event(12345)
        resolved = await repository.resolve_canonical_subject(
            ("email:a@example.com",),
            "144 bullman street",
        )

    assert loaded == event
    assert resolved.canonical_subject == "prospect:canonical"
    assert calls[0][1] == [12345]
    assert "12345" not in calls[0][0]
    assert calls[1][1] == [["email:a@example.com"], "144 bullman street"]


@pytest.mark.asyncio
async def test_repository_event_query_survives_literal_empty_json_object():
    class Row:
        def __init__(self, cells):
            self.cells = cells

    class Driver:
        async def execute_query(self, query, *args, **kwargs):
            assert "'{}'::jsonb" in query
            return [Row(record().__dict__)]

    loaded = await OutboundGatewayRepository(Driver()).load_wake_event(12345)

    assert loaded.wakeup_event_id == 12345


# ============================================================================
# Adversarial identity tests (Task 5, Step 5)
#
# These prove that removing server-side target derivation as an execute()
# gate did not reopen the wrong-recipient/duplicate-send hazards that gate
# used to guard against. For each limit-pushing wake shape below: the send
# goes exactly where the agent said and nowhere the wake's own data implies,
# and suggest_targets() still offers the id the retired derivation would
# have produced.
# ============================================================================


@pytest.mark.asyncio
async def test_adversarial_quo_shared_line_send_goes_only_to_agent_supplied_phone():
    """ "Quo channel is a line, not a conversation" (the class of bug that
    killed PR #158): a Quo phone line is shared by many counterparties, so
    any identity derived from the wake's own channel/participant data risks
    collapsing distinct prospects onto each other. This wake's own data
    implies counterparty A's number (+19085550001, the message.phone /
    participant_key the retired derivation would have used to build
    recipient_phone). The agent instead names counterparty B
    (+19085559999) on the very same shared-line wake. The send must go to
    B and only B -- not to A, and not to some channel-wide bucket id."""
    event = record(
        event_source="quo",
        message_source="quo",
        source_channel_id="quo-shared-line-18",
        channel_type="phone_number",
        participant_type="phone_number",
        participant_key="+19085550001",
        raw_payload={
            "data": {
                "object": {
                    "conversationId": "quo-shared-line-18",
                    "phoneNumberId": "leasing-main",
                    "direction": "incoming",
                    "from": "+19085550001",
                }
            }
        },
        envelope={
            "identity": {},
            "message": {"property": "16 N Main St #16", "phone": "+1 908 555 0001"},
        },
    )
    loader = ActionContextLoader(FakeRepository(event), policy())

    hints = await loader.suggest_targets(event.wakeup_event_id)
    context = await loader.load(
        request(
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085559999", "text": "Thanks for reaching out"},
        )
    )

    # The wake's own data (what the retired derivation, and today's
    # suggest_targets, would use) points at counterparty A.
    assert hints["to_phone"] == "+19085550001"
    # The agent named counterparty B -- the send follows the agent exactly,
    # never falling back to the shared line's other-counterparty history.
    assert context.target.kind == "quo_conversation"
    assert context.target.target_id == "+19085559999"
    assert context.recipient_phone == "+19085559999"
    assert context.target.verified is True


@pytest.mark.asyncio
async def test_adversarial_zillow_rotating_proxy_send_goes_only_to_agent_supplied_address():
    """A Zillow lead's reply-to address is a rotating per-lead
    @convo.zillow.com proxy. The retired derivation enforced replies go
    *only* to that exact proxy (require_zillow_proxy=True) and would flatly
    reject any other address, including the prospect's own real inbox. The
    agent here deliberately supplies a different, non-proxy address; the
    send must go there and only there -- the old proxy-only restriction is
    no longer an execute-time gate -- while suggest_targets still reports
    the rotating proxy the wake implies."""
    event = record(
        participant_type="email_address",
        participant_key="rotation-88f3@convo.zillow.com",
        raw_payload={"provider": "zillow", "thread_id": "zrm-thread-88f3"},
        envelope={
            "identity": {},
            "message": {
                "prospect_name": "New Prospect",
                "property": "138 Bullman St #144-A",
                "proxy_email": "rotation-88f3@convo.zillow.com",
            },
        },
    )
    loader = ActionContextLoader(FakeRepository(event), policy())

    hints = await loader.suggest_targets(event.wakeup_event_id)
    context = await loader.load(request(arguments={"to_address": "prospect-direct@gmail.com", "text": "Thanks for reaching out"}))

    # suggest still surfaces exactly the rotating proxy the retired
    # derivation would have enforced.
    assert hints["to_address"] == "rotation-88f3@convo.zillow.com"
    # execute honors the agent's own choice even though it disagrees with
    # (and would have been rejected by) the old Zillow-proxy-only rule.
    assert context.target.kind == "email_thread"
    assert context.target.target_id == "prospect-direct@gmail.com"
    assert context.target.verified is True


@pytest.mark.asyncio
async def test_adversarial_cliq_channel_post_and_chat_post_never_cross_contaminate_ids():
    """Cliq channel posts and direct chat posts are different provider
    surfaces (a public channel vs. a 1:1 DM) that used to share one static,
    intent-keyed config value (cliq_target_by_intent) regardless of which
    of the two operations was invoked. Now each execute carries its own
    channel_or_chat_id. On the very same triggering wake: a channel post
    and a chat post must each land on exactly the id their own request
    carried, never on the other's id and never on the old static config
    default ("tenant-leads")."""
    event = record(
        event_source="zoho_cliq",
        message_source="zoho_cliq",
        source_channel_id="tenant-leads",
        channel_type="channel",
        participant_type="user",
        participant_key="internal-user",
        raw_payload={"provider": "cliq", "channel_id": "tenant-leads"},
        envelope={"identity": {}, "message": {}},
    )
    loader = ActionContextLoader(FakeRepository(event), policy())

    channel_context = await loader.load(
        request(
            action_role="internal_notification",
            operation="cliq.channel.post",
            intent_kind="lead_alert",
            appointment_slot=None,
            arguments={"channel_or_chat_id": "team-leads-public-channel", "text": "New lead"},
        )
    )
    chat_context = await loader.load(
        request(
            action_role="internal_notification",
            operation="cliq.chat.post",
            intent_kind="lead_alert",
            appointment_slot=None,
            arguments={"channel_or_chat_id": "nigel-direct-chat", "text": "New lead"},
        )
    )

    assert channel_context.target.kind == "cliq_channel"
    assert channel_context.target.target_id == "team-leads-public-channel"
    assert chat_context.target.kind == "cliq_chat"
    assert chat_context.target.target_id == "nigel-direct-chat"
    # Neither execute fell back to the old static per-intent config value.
    assert "tenant-leads" not in {channel_context.target.target_id, chat_context.target.target_id}


# --- calendar.update / calendar.delete take event identity from the agent --

_EXAMPLE_EVENT_URL = "https://calendar.zoho.com/caldav/acct/events/3b34ed2d-e2e0-443b-b20a-097c98aebfc3.ics"
_EXAMPLE_EVENT_UID = "3b34ed2d-e2e0-443b-b20a-097c98aebfc3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "intent", "slot"),
    [
        ("calendar.update", "showing_update", "2026-07-17T14:30:00Z"),
        ("calendar.delete", "showing_delete", None),
    ],
)
async def test_calendar_update_and_delete_take_event_target_from_agent_on_a_wake_with_no_calendar_payload(operation, intent, slot):
    """calendar.update and calendar.delete were the last two operations
    still deriving their event identity solely from the wake payload --
    verified against production that zero raw_events rows ever carry
    calendar_event_url, so that path was permanently dead. The agent now
    names the exact event directly; this wake carries no calendar_event_uid/
    url/etag at all, and the action must still resolve. The uid is derived
    from the CalDAV URL's basename (confirmed from a real create response)."""
    event = record(raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"})
    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            action_role="calendar_mutation",
            operation=operation,
            intent_kind=intent,
            appointment_slot=slot,
            arguments={"calendar_id": "nigel", "event_url": _EXAMPLE_EVENT_URL, "etag": '"etag-1"'},
        )
    )
    assert context.calendar_event_url == _EXAMPLE_EVENT_URL
    assert context.calendar_event_etag == '"etag-1"'
    assert context.calendar_event_uid == _EXAMPLE_EVENT_UID
    assert context.target.kind == "calendar"
    assert context.target.target_id == "nigel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "intent", "slot"),
    [
        ("calendar.update", "showing_update", "2026-07-17T14:30:00Z"),
        ("calendar.delete", "showing_delete", None),
    ],
)
async def test_calendar_update_and_delete_explicit_event_uid_overrides_the_url_derived_one(operation, intent, slot):
    event = record(raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"})
    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            action_role="calendar_mutation",
            operation=operation,
            intent_kind=intent,
            appointment_slot=slot,
            arguments={
                "calendar_id": "nigel",
                "event_url": _EXAMPLE_EVENT_URL,
                "etag": '"etag-1"',
                "event_uid": "explicit-uid-override",
            },
        )
    )
    assert context.calendar_event_uid == "explicit-uid-override"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "intent", "slot"),
    [
        ("calendar.update", "showing_update", "2026-07-17T14:30:00Z"),
        ("calendar.delete", "showing_delete", None),
    ],
)
async def test_calendar_update_and_delete_fall_back_to_the_wake_derived_event_when_arguments_omit_it(operation, intent, slot):
    """The wake path stays as a fallback -- unreachable against today's real
    production data (verified zero raw_events rows carry
    calendar_event_url) but not deleted, for any caller that still routes
    event identity through the wake the way the pre-agent-targets code did."""
    event = record(
        raw_payload={
            "provider": "zillow",
            "thread_id": "zrm-thread-44",
            "calendar_event_uid": "wake-derived-uid",
            "calendar_event_url": "https://calendar.local/events/wake-derived-uid.ics",
            "calendar_event_etag": '"wake-etag"',
        }
    )
    context = await ActionContextLoader(FakeRepository(event), policy()).load(
        request(
            action_role="calendar_mutation",
            operation=operation,
            intent_kind=intent,
            appointment_slot=slot,
            arguments={"calendar_id": "nigel"},
        )
    )
    assert context.calendar_event_uid == "wake-derived-uid"
    assert context.calendar_event_url == "https://calendar.local/events/wake-derived-uid.ics"
    assert context.calendar_event_etag == '"wake-etag"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "intent", "slot"),
    [
        ("calendar.update", "showing_update", "2026-07-17T14:30:00Z"),
        ("calendar.delete", "showing_delete", None),
    ],
)
async def test_calendar_update_and_delete_still_fail_closed_when_neither_agent_nor_wake_supplies_the_event(operation, intent, slot):
    event = record(raw_payload={"provider": "zillow", "thread_id": "zrm-thread-44"})
    with pytest.raises(ContextDerivationError, match="calendar event"):
        await ActionContextLoader(FakeRepository(event), policy()).load(
            request(
                action_role="calendar_mutation",
                operation=operation,
                intent_kind=intent,
                appointment_slot=slot,
                arguments={"calendar_id": "nigel"},
            )
        )
