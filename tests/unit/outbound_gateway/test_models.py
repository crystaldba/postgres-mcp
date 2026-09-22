# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from datetime import timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from postgres_mcp.outbound_gateway.models import CalendarCreateArguments
from postgres_mcp.outbound_gateway.models import CalendarDeleteArguments
from postgres_mcp.outbound_gateway.models import CalendarUpdateArguments
from postgres_mcp.outbound_gateway.models import CliqArguments
from postgres_mcp.outbound_gateway.models import EmailArguments
from postgres_mcp.outbound_gateway.models import ExecuteRequest
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.models import QuoSmsArguments
from postgres_mcp.outbound_gateway.models import StatusRequest
from postgres_mcp.outbound_gateway.models import SuggestRequest
from postgres_mcp.outbound_gateway.models import parse_outbound_request


def execute_payload(**overrides):
    payload = {
        "op": "execute",
        "wakeup_event_id": 12345,
        "action_role": "prospect_reply",
        "operation": "email.send",
        "intent_kind": "showing_offer",
        "appointment_slot": "2026-07-17T10:30:00-04:00",
        "arguments": {"to_address": "prospect@example.com", "text": "Hello"},
    }
    payload.update(overrides)
    return payload


def test_execute_has_exact_required_top_level_contract():
    request = parse_outbound_request(execute_payload())
    assert isinstance(request, ExecuteRequest)
    assert request.wakeup_event_id == 12345
    assert request.appointment_slot.isoformat() == "2026-07-17T14:30:00+00:00"
    assert request.appointment_slot.tzinfo == timezone.utc
    assert isinstance(request.arguments, EmailArguments)

    for field in (
        "op",
        "wakeup_event_id",
        "action_role",
        "operation",
        "intent_kind",
        "arguments",
    ):
        invalid = execute_payload()
        invalid.pop(field)
        with pytest.raises(ValidationError):
            parse_outbound_request(invalid)


def test_execute_rejects_unknown_fields_and_non_positive_or_non_integer_wake_ids():
    with pytest.raises(ValidationError, match="extra"):
        parse_outbound_request(execute_payload(recipient="victim@example.com"))
    for value in (0, -1, 1.5, "123"):
        with pytest.raises(ValidationError):
            parse_outbound_request(execute_payload(wakeup_event_id=value))


@pytest.mark.parametrize(
    ("operation", "role", "intent", "slot", "arguments", "argument_type"),
    [
        ("email.send", "prospect_reply", "inquiry_reply", None, {"to_address": "prospect@example.com", "text": "Email"}, EmailArguments),
        (
            "quo.sms.send",
            "prospect_reply",
            "showing_offer",
            "2026-07-17T14:30:00Z",
            {"to_phone": "+19085550100", "text": "SMS"},
            QuoSmsArguments,
        ),
        (
            "cliq.channel.post",
            "internal_notification",
            "lead_alert",
            None,
            {"channel_or_chat_id": "tenant-leads", "text": "Lead"},
            CliqArguments,
        ),
        (
            "cliq.chat.post",
            "internal_notification",
            "manual_review_alert",
            None,
            {"channel_or_chat_id": "chat-42", "text": "Review"},
            CliqArguments,
        ),
        (
            "calendar.create",
            "calendar_mutation",
            "showing_create",
            "2026-07-17T14:30:00Z",
            {"calendar_id": "nigel", "description": "Tour"},
            CalendarCreateArguments,
        ),
        (
            "calendar.update",
            "calendar_mutation",
            "showing_update",
            "2026-07-17T14:30:00Z",
            {
                "calendar_id": "nigel",
                "event_url": "https://calendar.zoho.com/caldav/acct/events/3b34ed2d-e2e0-443b-b20a-097c98aebfc3.ics",
                "etag": '"etag-1"',
            },
            CalendarUpdateArguments,
        ),
        (
            "calendar.delete",
            "calendar_mutation",
            "showing_delete",
            None,
            {
                "calendar_id": "nigel",
                "event_url": "https://calendar.zoho.com/caldav/acct/events/3b34ed2d-e2e0-443b-b20a-097c98aebfc3.ics",
                "etag": '"etag-1"',
            },
            CalendarDeleteArguments,
        ),
    ],
)
def test_all_seven_operations_use_adapter_owned_strict_argument_schemas(operation, role, intent, slot, arguments, argument_type):
    request = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role=role,
            intent_kind=intent,
            appointment_slot=slot,
            arguments=arguments,
        )
    )
    assert request.operation == Operation(operation)
    assert isinstance(request.arguments, argument_type)


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "email.send", "arguments": {"text": "x", "to": "a@example.com"}},
        {"operation": "calendar.create", "arguments": {"description": "x", "calendar": "nigel"}},
        {"operation": "calendar.delete", "arguments": {"event_id": "raw-id"}},
        {"operation": "not.a.provider"},
        {"action_role": "staff_approval"},
    ],
)
def test_adapter_arguments_and_enums_reject_unknown_values(overrides):
    with pytest.raises(ValidationError):
        parse_outbound_request(execute_payload(**overrides))


def test_appointment_slot_matrix_requires_explicit_offset_and_normalizes_utc():
    with pytest.raises(ValidationError, match="appointment_slot is required"):
        parse_outbound_request(execute_payload(appointment_slot=None))
    with pytest.raises(ValidationError, match="explicit UTC offset"):
        parse_outbound_request(execute_payload(appointment_slot="2026-07-17T10:30:00"))
    with pytest.raises(ValidationError, match="forbidden"):
        parse_outbound_request(execute_payload(intent_kind="inquiry_reply", appointment_slot="2026-07-17T14:30:00Z"))


def test_text_is_nfc_lf_normalized_and_length_bounded():
    request = parse_outbound_request(
        execute_payload(arguments={"to_address": "prospect@example.com", "text": "Cafe\u0301\r\nTour"})
    )
    assert request.arguments.text == "Café\nTour"
    for value in ("", "x" * 10001):
        with pytest.raises(ValidationError):
            parse_outbound_request(execute_payload(arguments={"to_address": "prospect@example.com", "text": value}))


def test_status_accepts_only_op_and_uuid_action_id():
    action_id = "8f8f1a45-13a7-4bd3-a15a-f8d265bbc567"
    request = parse_outbound_request({"op": "status", "action_id": action_id})
    assert isinstance(request, StatusRequest)
    assert request.action_id == UUID(action_id)
    with pytest.raises(ValidationError):
        parse_outbound_request({"op": "status", "action_id": "bad"})
    with pytest.raises(ValidationError, match="extra"):
        parse_outbound_request({"op": "status", "action_id": action_id, "wake": 1})


def test_suggest_accepts_only_op_and_wakeup_event_id():
    request = parse_outbound_request({"op": "suggest", "wakeup_event_id": 1})
    assert isinstance(request, SuggestRequest)
    assert request.wakeup_event_id == 1
    with pytest.raises(ValidationError):
        parse_outbound_request({"op": "suggest", "wakeup_event_id": -1})
    with pytest.raises(ValidationError, match="extra"):
        parse_outbound_request({"op": "suggest", "wakeup_event_id": 1, "lead_id": "123"})


@pytest.mark.parametrize(
    ("operation", "role", "intent", "arguments", "argument_type"),
    [
        (
            "tenantcloud.message.send",
            "prospect_reply",
            "inquiry_reply",
            {"thread_id": 8001, "text": "Reply"},
            "TenantCloudMessageArguments",
        ),
        (
            "tenantcloud.lead.status.update",
            "provider_mutation",
            "tenantcloud_lead_status",
            {"lead_id": 6001, "status": "working"},
            "LeadStatusArguments",
        ),
        (
            "tenantcloud.maintenance.create",
            "provider_mutation",
            "tenantcloud_maintenance_create",
            {
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
            "MaintenanceCreateArguments",
        ),
        (
            "tenantcloud.maintenance.status.update",
            "provider_mutation",
            "tenantcloud_maintenance_status",
            {"request_id": 81, "status": 3},
            "MaintenanceStatusArguments",
        ),
    ],
)
def test_tenantcloud_operations_use_exact_strict_argument_models(operation, role, intent, arguments, argument_type):
    parsed = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role=role,
            intent_kind=intent,
            appointment_slot=None,
            arguments=arguments,
        )
    )

    assert type(parsed.arguments).__name__ == argument_type
    assert parsed.arguments.model_config["frozen"] is True
    if operation == "tenantcloud.maintenance.create":
        assert parsed.arguments.text == "Pipe is leaking\nunder sink"


def test_tenantcloud_arguments_carry_agent_supplied_targets():
    msg = parse_outbound_request({
        "op": "execute", "wakeup_event_id": 1, "action_role": "prospect_reply",
        "operation": "tenantcloud.message.send", "intent_kind": "inquiry_reply",
        "appointment_slot": None,
        "arguments": {"thread_id": 2002331, "text": "Thanks"},
    })
    assert msg.arguments.thread_id == 2002331

    lead = parse_outbound_request({
        "op": "execute", "wakeup_event_id": 1, "action_role": "provider_mutation",
        "operation": "tenantcloud.lead.status.update", "intent_kind": "tenantcloud_lead_status",
        "appointment_slot": None,
        "arguments": {"lead_id": 2405115, "status": "working"},
    })
    assert lead.arguments.lead_id == 2405115


@pytest.mark.parametrize("bad", [0, -1, "12", 1.5, None, True])
def test_tenantcloud_target_ids_reject_non_positive_integers(bad):
    with pytest.raises(ValueError):
        parse_outbound_request({
            "op": "execute", "wakeup_event_id": 1, "action_role": "prospect_reply",
            "operation": "tenantcloud.message.send", "intent_kind": "inquiry_reply",
            "appointment_slot": None,
            "arguments": {"thread_id": bad, "text": "Thanks"},
        })


@pytest.mark.parametrize("field", ["thread_id", "request_id", "property_id", "unit_id"])
def test_tenantcloud_arguments_reject_cross_operation_target_smuggling(field):
    """lead_id is now legitimate on LeadStatusArguments (it's the agent-
    supplied target), but the *other* three operations' target-id field
    names must still be rejected as extra -- an agent can't smuggle a
    maintenance/message target onto a lead status update."""
    with pytest.raises(ValidationError, match="extra"):
        parse_outbound_request(
            execute_payload(
                operation="tenantcloud.lead.status.update",
                action_role="provider_mutation",
                intent_kind="tenantcloud_lead_status",
                appointment_slot=None,
                arguments={"lead_id": 6001, "status": "working", field: 7},
            )
        )


@pytest.mark.parametrize("status", ["new", "closed", "WORKING", 1, True])
def test_tenantcloud_lead_status_is_exact(status):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="tenantcloud.lead.status.update",
                action_role="provider_mutation",
                intent_kind="tenantcloud_lead_status",
                appointment_slot=None,
                arguments={"status": status},
            )
        )


@pytest.mark.parametrize("status", [True, False, 0, 4, "1", None])
def test_tenantcloud_maintenance_status_is_strict(status):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="tenantcloud.maintenance.status.update",
                action_role="provider_mutation",
                intent_kind="tenantcloud_maintenance_status",
                appointment_slot=None,
                arguments={"status": status},
            )
        )


def maintenance_create_arguments(**overrides):
    arguments = {
        "property_id": 12,
        "unit_id": 34,
        "category_id": 57,
        "title": "Kitchen leak",
        "priority": "normal",
        "initiated_at": "2026-08-04",
        "text": "Pipe is leaking",
        "entry_allowed": False,
        "available_on": None,
    }
    arguments.update(overrides)
    return arguments


@pytest.mark.parametrize("category_id", [True, False, 0, -1, 1.5, "57", 9_223_372_036_854_775_808])
def test_maintenance_create_category_id_is_positive_strict_bigint(category_id):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="tenantcloud.maintenance.create",
                action_role="provider_mutation",
                intent_kind="tenantcloud_maintenance_create",
                appointment_slot=None,
                arguments=maintenance_create_arguments(category_id=category_id),
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"priority": "high"},
        {"priority": "NORMAL"},
        {"entry_allowed": 0},
        {"entry_allowed": "false"},
        {"initiated_at": "08/04/2026"},
        {"initiated_at": "2026-02-30"},
        {"available_on": "08/05/2026"},
        {"available_on": "2026-02-30"},
        {"title": ""},
        {"title": " Kitchen leak"},
        {"title": "x" * 256},
        {"text": ""},
        {"text": "Pipe is leaking "},
        {"text": "x" * 10_001},
        {"unknown": "value"},
    ],
)
def test_maintenance_create_rejects_noncanonical_fields(overrides):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="tenantcloud.maintenance.create",
                action_role="provider_mutation",
                intent_kind="tenantcloud_maintenance_create",
                appointment_slot=None,
                arguments=maintenance_create_arguments(**overrides),
            )
        )


def test_maintenance_create_normalizes_unicode_and_newlines_before_hashing():
    parsed = parse_outbound_request(
        execute_payload(
            operation="tenantcloud.maintenance.create",
            action_role="provider_mutation",
            intent_kind="tenantcloud_maintenance_create",
            appointment_slot=None,
            arguments=maintenance_create_arguments(text="Cafe\u0301\r\nPipe"),
        )
    )

    assert parsed.arguments.text == "Café\nPipe"


# --- Task 5: agent-supplied targets for email, Quo, Cliq, and calendar -----


def test_email_arguments_carry_the_agent_supplied_to_address():
    parsed = parse_outbound_request(
        execute_payload(arguments={"to_address": "prospect@example.com", "text": "Thanks"})
    )
    assert parsed.arguments.to_address == "prospect@example.com"


@pytest.mark.parametrize("bad", ["", "   ", "no-at-sign", None, 5, True])
def test_email_to_address_rejects_malformed_values(bad):
    with pytest.raises(ValidationError):
        parse_outbound_request(execute_payload(arguments={"to_address": bad, "text": "Thanks"}))


def test_quo_arguments_carry_the_agent_supplied_to_phone():
    parsed = parse_outbound_request(
        execute_payload(
            operation="quo.sms.send",
            intent_kind="inquiry_reply",
            appointment_slot=None,
            arguments={"to_phone": "+19085550100", "text": "Thanks"},
        )
    )
    assert parsed.arguments.to_phone == "+19085550100"


@pytest.mark.parametrize("bad", ["", "   ", "908-555-0199", "9085550199", "+1abc5550199", None, 5, True])
def test_quo_to_phone_rejects_non_e164_values(bad):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="quo.sms.send",
                intent_kind="inquiry_reply",
                appointment_slot=None,
                arguments={"to_phone": bad, "text": "Thanks"},
            )
        )


@pytest.mark.parametrize("operation", ["cliq.channel.post", "cliq.chat.post"])
def test_cliq_arguments_carry_the_agent_supplied_channel_or_chat_id(operation):
    role = "internal_notification"
    intent = "lead_alert" if operation == "cliq.channel.post" else "manual_review_alert"
    parsed = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role=role,
            intent_kind=intent,
            appointment_slot=None,
            arguments={"channel_or_chat_id": "tenant-leads-7", "text": "New lead"},
        )
    )
    assert parsed.arguments.channel_or_chat_id == "tenant-leads-7"


@pytest.mark.parametrize("bad", ["", "   ", None, 5, True])
def test_cliq_channel_or_chat_id_rejects_empty_or_wrong_type(bad):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation="cliq.channel.post",
                action_role="internal_notification",
                intent_kind="lead_alert",
                appointment_slot=None,
                arguments={"channel_or_chat_id": bad, "text": "New lead"},
            )
        )


@pytest.mark.parametrize(
    ("operation", "role", "intent", "slot", "arguments"),
    [
        (
            "calendar.create",
            "calendar_mutation",
            "showing_create",
            "2026-07-17T14:30:00Z",
            {"calendar_id": "nigel", "description": "Tour"},
        ),
        (
            "calendar.update",
            "calendar_mutation",
            "showing_update",
            "2026-07-17T14:30:00Z",
            {"calendar_id": "nigel"},
        ),
        ("calendar.delete", "calendar_mutation", "showing_delete", None, {"calendar_id": "nigel"}),
    ],
)
def test_calendar_arguments_carry_the_agent_supplied_calendar_id(operation, role, intent, slot, arguments):
    parsed = parse_outbound_request(
        execute_payload(operation=operation, action_role=role, intent_kind=intent, appointment_slot=slot, arguments=arguments)
    )
    assert parsed.arguments.calendar_id == "nigel"


@pytest.mark.parametrize("operation", ["calendar.create", "calendar.update", "calendar.delete"])
@pytest.mark.parametrize("bad", ["", "   ", None, 5, True])
def test_calendar_id_rejects_empty_or_wrong_type(operation, bad):
    role = "calendar_mutation"
    intent = {"calendar.create": "showing_create", "calendar.update": "showing_update", "calendar.delete": "showing_delete"}[operation]
    slot = None if operation == "calendar.delete" else "2026-07-17T14:30:00Z"
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(operation=operation, action_role=role, intent_kind=intent, appointment_slot=slot, arguments={"calendar_id": bad})
        )


# --- calendar.update / calendar.delete take event identity from the agent --


_CALENDAR_UPDATE_DELETE = [
    ("calendar.update", "showing_update", "2026-07-17T14:30:00Z"),
    ("calendar.delete", "showing_delete", None),
]
_EXAMPLE_EVENT_URL = "https://calendar.zoho.com/caldav/acct/events/3b34ed2d-e2e0-443b-b20a-097c98aebfc3.ics"


@pytest.mark.parametrize(("operation", "intent", "slot"), _CALENDAR_UPDATE_DELETE)
def test_calendar_update_and_delete_carry_the_agent_supplied_event_target(operation, intent, slot):
    parsed = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role="calendar_mutation",
            intent_kind=intent,
            appointment_slot=slot,
            arguments={"calendar_id": "nigel", "event_url": _EXAMPLE_EVENT_URL, "etag": '"etag-1"'},
        )
    )
    assert parsed.arguments.calendar_id == "nigel"
    assert parsed.arguments.event_url == _EXAMPLE_EVENT_URL
    assert parsed.arguments.etag == '"etag-1"'
    assert parsed.arguments.event_uid is None


@pytest.mark.parametrize(("operation", "intent", "slot"), _CALENDAR_UPDATE_DELETE)
def test_calendar_update_and_delete_allow_the_event_target_to_be_omitted_for_the_wake_fallback(operation, intent, slot):
    """event_url/etag/event_uid are optional on the model itself -- context
    derivation (not the model) decides whether an omitted value can fall
    back to the wake, or must fail closed."""
    parsed = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role="calendar_mutation",
            intent_kind=intent,
            appointment_slot=slot,
            arguments={"calendar_id": "nigel"},
        )
    )
    assert parsed.arguments.event_url is None
    assert parsed.arguments.etag is None
    assert parsed.arguments.event_uid is None


@pytest.mark.parametrize(("operation", "intent", "slot"), _CALENDAR_UPDATE_DELETE)
def test_calendar_update_and_delete_explicit_event_uid_overrides_the_url_derived_one(operation, intent, slot):
    parsed = parse_outbound_request(
        execute_payload(
            operation=operation,
            action_role="calendar_mutation",
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
    assert parsed.arguments.event_uid == "explicit-uid-override"


@pytest.mark.parametrize(("operation", "intent", "slot"), _CALENDAR_UPDATE_DELETE)
@pytest.mark.parametrize("bad_url", ["", "   ", "not-a-url"])
def test_calendar_update_and_delete_reject_a_malformed_event_url(operation, intent, slot, bad_url):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation=operation,
                action_role="calendar_mutation",
                intent_kind=intent,
                appointment_slot=slot,
                arguments={"calendar_id": "nigel", "event_url": bad_url, "etag": '"etag-1"'},
            )
        )


@pytest.mark.parametrize(("operation", "intent", "slot"), _CALENDAR_UPDATE_DELETE)
@pytest.mark.parametrize("bad_etag", ["", "   "])
def test_calendar_update_and_delete_reject_an_empty_etag(operation, intent, slot, bad_etag):
    with pytest.raises(ValidationError):
        parse_outbound_request(
            execute_payload(
                operation=operation,
                action_role="calendar_mutation",
                intent_kind=intent,
                appointment_slot=slot,
                arguments={"calendar_id": "nigel", "event_url": _EXAMPLE_EVENT_URL, "etag": bad_etag},
            )
        )


def test_calendar_create_still_works_without_event_fields_and_rejects_them_as_extra():
    parsed = parse_outbound_request(
        execute_payload(
            operation="calendar.create",
            action_role="calendar_mutation",
            intent_kind="showing_create",
            appointment_slot="2026-07-17T14:30:00Z",
            arguments={"calendar_id": "nigel", "description": "Tour"},
        )
    )
    assert isinstance(parsed.arguments, CalendarCreateArguments)
    assert parsed.arguments.calendar_id == "nigel"

    with pytest.raises(ValidationError, match="extra"):
        parse_outbound_request(
            execute_payload(
                operation="calendar.create",
                action_role="calendar_mutation",
                intent_kind="showing_create",
                appointment_slot="2026-07-17T14:30:00Z",
                arguments={"calendar_id": "nigel", "event_url": _EXAMPLE_EVENT_URL, "etag": '"etag-1"'},
            )
        )


# --- Task 3: override flag, free-form intent metadata ----------------------


def test_execute_request_accepts_override_flag():
    req = execute_payload()
    req["override"] = True
    assert ExecuteRequest.model_validate(req).override is True


def test_execute_request_default_override_false():
    assert ExecuteRequest.model_validate(execute_payload()).override is False


def test_unknown_intent_is_stored_not_rejected():
    req = execute_payload()
    req["intent_kind"] = "escalation"  # the string that burned wake 25789
    parsed = ExecuteRequest.model_validate(req)
    assert parsed.intent_kind == "escalation"


def test_role_operation_intent_matrix_no_longer_enforced():
    req = execute_payload()
    req["action_role"] = "internal_notification"
    req["operation"] = "cliq.channel.post"
    req["intent_kind"] = "escalation"
    req["arguments"] = {"channel_or_chat_id": "tenantleads", "text": "hi"}
    ExecuteRequest.model_validate(req)  # must not raise


def test_slot_still_required_for_showing_offer():
    req = execute_payload()
    req["intent_kind"] = "showing_offer"
    req.pop("appointment_slot", None)
    with pytest.raises(ValidationError, match="appointment_slot is required"):
        ExecuteRequest.model_validate(req)
