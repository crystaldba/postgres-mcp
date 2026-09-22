"""Strict public contracts for the outbound action gateway."""

from __future__ import annotations

import re
from datetime import date
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from typing import Annotated
from typing import Any
from typing import Literal
from typing import TypeAlias
from unicodedata import category
from unicodedata import normalize
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import field_validator
from pydantic import model_validator


class ActionRole(StrEnum):
    PROSPECT_REPLY = "prospect_reply"
    CALENDAR_MUTATION = "calendar_mutation"
    INTERNAL_NOTIFICATION = "internal_notification"
    PROVIDER_MUTATION = "provider_mutation"


class Operation(StrEnum):
    EMAIL_SEND = "email.send"
    QUO_SMS_SEND = "quo.sms.send"
    CLIQ_CHANNEL_POST = "cliq.channel.post"
    CLIQ_CHAT_POST = "cliq.chat.post"
    CALENDAR_CREATE = "calendar.create"
    CALENDAR_UPDATE = "calendar.update"
    CALENDAR_DELETE = "calendar.delete"
    TENANTCLOUD_MESSAGE_SEND = "tenantcloud.message.send"
    TENANTCLOUD_LEAD_STATUS_UPDATE = "tenantcloud.lead.status.update"
    TENANTCLOUD_MAINTENANCE_CREATE = "tenantcloud.maintenance.create"
    TENANTCLOUD_MAINTENANCE_STATUS_UPDATE = "tenantcloud.maintenance.status.update"


class IntentKind(StrEnum):
    INQUIRY_REPLY = "inquiry_reply"
    SHOWING_OFFER = "showing_offer"
    SHOWING_CONFIRMATION = "showing_confirmation"
    SHOWING_RESCHEDULE = "showing_reschedule"
    SHOWING_CANCELLATION = "showing_cancellation"
    SHOWING_CREATE = "showing_create"
    SHOWING_UPDATE = "showing_update"
    SHOWING_DELETE = "showing_delete"
    LEAD_ALERT = "lead_alert"
    MANUAL_REVIEW_ALERT = "manual_review_alert"
    TENANTCLOUD_LEAD_STATUS = "tenantcloud_lead_status"
    TENANTCLOUD_MAINTENANCE_CREATE = "tenantcloud_maintenance_create"
    TENANTCLOUD_MAINTENANCE_STATUS = "tenantcloud_maintenance_status"


class ActionState(StrEnum):
    RECEIVED = "received"
    DEPENDENCY_WAIT = "dependency_wait"
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    PROVIDER_ACCEPTED = "provider_accepted"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    RETRY_READY = "retry_ready"
    COMPLETED = "completed"
    STALE = "stale"
    REJECTED = "rejected"
    DEFINITIVE_FAILED = "definitive_failed"
    DEAD_LETTER = "dead_letter"
    MANUAL_REVIEW = "manual_review"


class CompletionKind(StrEnum):
    SENT = "sent"
    DUPLICATE = "duplicate"


class PublicStatus(StrEnum):
    SENT = "sent"
    DUPLICATE = "duplicate"
    PENDING = "pending"
    STALE = "stale"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MANUAL_REVIEW = "manual_review"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def normalize_public_text(value: Any, *, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"{field} length must be between {minimum} and {maximum}")
    return normalized


_EMAIL_ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_E164_PHONE = re.compile(r"^\+[1-9]\d{1,14}$")


def normalize_target_email(value: Any, *, field: str) -> str:
    """Format-only email check: syntactically an address, nothing more.
    Never checks that the address belongs to any prospect, wake, or thread --
    the agent asserts the recipient, this only rejects garbage."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    candidate = normalize("NFC", value).strip()
    if not _EMAIL_ADDRESS.fullmatch(candidate):
        raise ValueError(f"{field} must look like an email address")
    return candidate


def normalize_target_phone(value: Any, *, field: str) -> str:
    """Format-only E.164 check. Never checks that the number belongs to any
    prospect, wake, or conversation."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    candidate = value.strip()
    if not _E164_PHONE.fullmatch(candidate):
        raise ValueError(f"{field} must be E.164 formatted, e.g. +19085550100")
    return candidate


def normalize_target_id(value: Any, *, field: str) -> str:
    """Format-only non-empty-string check for opaque provider target ids
    (Cliq channel/chat ids, calendar ids). No ownership or membership check."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    candidate = normalize("NFC", value).strip()
    if not candidate:
        raise ValueError(f"{field} must not be empty")
    return candidate


def normalize_optional_target_id(value: Any, *, field: str) -> str | None:
    """Same format-only check as normalize_target_id, but None means the
    agent omitted the field entirely (a caller-side fallback may still
    apply) rather than supplying a blank value."""
    if value is None:
        return None
    return normalize_target_id(value, field=field)


_ABSOLUTE_HTTP_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


def normalize_event_url(value: Any, *, field: str) -> str | None:
    """Format-only check that the value is a plausible absolute http(s)
    URL. Never verifies the event exists or belongs to any wake, prospect,
    or calendar -- the agent asserts the event, this only rejects garbage.
    None means the field was omitted (a caller-side fallback may apply)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    candidate = normalize("NFC", value).strip()
    if not _ABSOLUTE_HTTP_URL.fullmatch(candidate):
        raise ValueError(f"{field} must be an absolute http(s) URL")
    return candidate


class EmailArguments(StrictModel):
    to_address: str
    text: str

    @field_validator("to_address", mode="before")
    @classmethod
    def normalize_to_address(cls, value: Any) -> str:
        return normalize_target_email(value, field="to_address")

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_public_text(value, field="text", minimum=1, maximum=10_000)


class QuoSmsArguments(StrictModel):
    to_phone: str
    text: str

    @field_validator("to_phone", mode="before")
    @classmethod
    def normalize_to_phone(cls, value: Any) -> str:
        return normalize_target_phone(value, field="to_phone")

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_public_text(value, field="text", minimum=1, maximum=10_000)


class CliqArguments(StrictModel):
    channel_or_chat_id: str
    text: str

    @field_validator("channel_or_chat_id", mode="before")
    @classmethod
    def normalize_channel_or_chat_id(cls, value: Any) -> str:
        return normalize_target_id(value, field="channel_or_chat_id")

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_public_text(value, field="text", minimum=1, maximum=10_000)


class CalendarCreateArguments(StrictModel):
    calendar_id: str
    description: str | None = None

    @field_validator("calendar_id", mode="before")
    @classmethod
    def normalize_calendar_id(cls, value: Any) -> str:
        return normalize_target_id(value, field="calendar_id")

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_public_text(value, field="description", minimum=0, maximum=10_000)


class CalendarUpdateArguments(StrictModel):
    calendar_id: str
    event_url: str | None = None
    etag: str | None = None
    event_uid: str | None = None
    description: str | None = None

    @field_validator("calendar_id", mode="before")
    @classmethod
    def normalize_calendar_id(cls, value: Any) -> str:
        return normalize_target_id(value, field="calendar_id")

    @field_validator("event_url", mode="before")
    @classmethod
    def normalize_event_url_field(cls, value: Any) -> str | None:
        return normalize_event_url(value, field="event_url")

    @field_validator("etag", mode="before")
    @classmethod
    def normalize_etag_field(cls, value: Any) -> str | None:
        return normalize_optional_target_id(value, field="etag")

    @field_validator("event_uid", mode="before")
    @classmethod
    def normalize_event_uid_field(cls, value: Any) -> str | None:
        return normalize_optional_target_id(value, field="event_uid")

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_public_text(value, field="description", minimum=0, maximum=10_000)


class CalendarDeleteArguments(StrictModel):
    calendar_id: str
    event_url: str | None = None
    etag: str | None = None
    event_uid: str | None = None

    @field_validator("calendar_id", mode="before")
    @classmethod
    def normalize_calendar_id(cls, value: Any) -> str:
        return normalize_target_id(value, field="calendar_id")

    @field_validator("event_url", mode="before")
    @classmethod
    def normalize_event_url_field(cls, value: Any) -> str | None:
        return normalize_event_url(value, field="event_url")

    @field_validator("etag", mode="before")
    @classmethod
    def normalize_etag_field(cls, value: Any) -> str | None:
        return normalize_optional_target_id(value, field="etag")

    @field_validator("event_uid", mode="before")
    @classmethod
    def normalize_event_uid_field(cls, value: Any) -> str | None:
        return normalize_optional_target_id(value, field="event_uid")


PositiveBigInt = Annotated[int, Field(strict=True, gt=0, le=9_223_372_036_854_775_807)]


def normalize_tenantcloud_text(value: Any, *, field: str, maximum: int) -> str:
    normalized = normalize_public_text(value, field=field, minimum=1, maximum=maximum)
    if normalized != normalized.strip():
        raise ValueError(f"{field} must not have surrounding whitespace")
    if any(category(character) == "Cc" and character not in {"\n", "\t"} for character in normalized):
        raise ValueError(f"{field} contains unsupported control characters")
    return normalized


def parse_iso_date(value: Any, *, field: str) -> date:
    if type(value) is not str:
        raise ValueError(f"{field} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a real YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date")
    return parsed


class TenantCloudMessageArguments(StrictModel):
    thread_id: PositiveBigInt
    text: str

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_tenantcloud_text(value, field="text", maximum=10_000)


class LeadStatusArguments(StrictModel):
    lead_id: PositiveBigInt
    status: Literal["working"]


class MaintenanceCreateArguments(StrictModel):
    property_id: PositiveBigInt
    unit_id: PositiveBigInt
    category_id: PositiveBigInt
    title: str
    priority: Literal["normal"]
    initiated_at: date
    text: str
    entry_allowed: Annotated[bool, Field(strict=True)]
    available_on: date | None = None

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return normalize_tenantcloud_text(value, field="title", maximum=255)

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return normalize_tenantcloud_text(value, field="text", maximum=10_000)

    @field_validator("initiated_at", "available_on", mode="before")
    @classmethod
    def normalize_date(cls, value: Any, info: Any) -> date | None:
        if value is None and info.field_name == "available_on":
            return None
        return parse_iso_date(value, field=info.field_name)


class MaintenanceStatusArguments(StrictModel):
    request_id: PositiveBigInt
    status: Literal[1, 2, 3]

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, value: Any) -> int:
        if type(value) is not int or value not in {1, 2, 3}:
            raise ValueError("status must be exactly 1, 2, or 3")
        return value


ArgumentModel: TypeAlias = (
    EmailArguments
    | QuoSmsArguments
    | CliqArguments
    | CalendarCreateArguments
    | CalendarUpdateArguments
    | CalendarDeleteArguments
    | TenantCloudMessageArguments
    | LeadStatusArguments
    | MaintenanceCreateArguments
    | MaintenanceStatusArguments
)


ARGUMENT_MODELS: dict[Operation, type[StrictModel]] = {
    Operation.EMAIL_SEND: EmailArguments,
    Operation.QUO_SMS_SEND: QuoSmsArguments,
    Operation.CLIQ_CHANNEL_POST: CliqArguments,
    Operation.CLIQ_CHAT_POST: CliqArguments,
    Operation.CALENDAR_CREATE: CalendarCreateArguments,
    Operation.CALENDAR_UPDATE: CalendarUpdateArguments,
    Operation.CALENDAR_DELETE: CalendarDeleteArguments,
    Operation.TENANTCLOUD_MESSAGE_SEND: TenantCloudMessageArguments,
    Operation.TENANTCLOUD_LEAD_STATUS_UPDATE: LeadStatusArguments,
    Operation.TENANTCLOUD_MAINTENANCE_CREATE: MaintenanceCreateArguments,
    Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE: MaintenanceStatusArguments,
}


SLOT_REQUIRED_INTENTS = frozenset(
    {
        IntentKind.SHOWING_OFFER,
        IntentKind.SHOWING_CONFIRMATION,
        IntentKind.SHOWING_RESCHEDULE,
        IntentKind.SHOWING_CREATE,
        IntentKind.SHOWING_UPDATE,
    }
)


_KNOWN_INTENT_KINDS = frozenset(member.value for member in IntentKind)


class ExecuteRequest(StrictModel):
    op: Literal["execute"]
    wakeup_event_id: PositiveBigInt
    action_role: ActionRole
    operation: Operation
    intent_kind: str
    arguments: ArgumentModel
    appointment_slot: datetime | None = None
    override: bool = False

    @model_validator(mode="before")
    @classmethod
    def validate_adapter_arguments(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        operation_value = raw.get("operation")
        try:
            operation = Operation(operation_value)
        except (TypeError, ValueError):
            return raw
        data = dict(raw)
        data["arguments"] = ARGUMENT_MODELS[operation].model_validate(raw.get("arguments"))
        return data

    @field_validator("intent_kind", mode="before")
    @classmethod
    def normalize_intent_kind(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("intent_kind must be a string")
        candidate = value.strip().casefold()
        if not candidate:
            raise ValueError("intent_kind must not be empty")
        if len(candidate) > 64:
            raise ValueError("intent_kind must be at most 64 characters")
        return candidate

    @field_validator("appointment_slot", mode="before")
    @classmethod
    def normalize_appointment_slot(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("appointment_slot must be RFC 3339") from exc
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise ValueError("appointment_slot must be an RFC 3339 string")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("appointment_slot requires an explicit UTC offset")
        return parsed.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_matrix(self) -> ExecuteRequest:
        if self.intent_kind in SLOT_REQUIRED_INTENTS and self.appointment_slot is None:
            raise ValueError("appointment_slot is required for this intent")
        known_intent = self.intent_kind in _KNOWN_INTENT_KINDS
        if (
            self.appointment_slot is not None
            and self.intent_kind not in SLOT_REQUIRED_INTENTS
            and known_intent
        ):
            raise ValueError("appointment_slot is forbidden for this intent")
        return self


class StatusRequest(StrictModel):
    op: Literal["status"]
    action_id: UUID


class SuggestRequest(StrictModel):
    op: Literal["suggest"]
    wakeup_event_id: PositiveBigInt


OutboundRequest: TypeAlias = Annotated[
    ExecuteRequest | StatusRequest | SuggestRequest, Field(discriminator="op")
]
_REQUEST_ADAPTER = TypeAdapter(OutboundRequest)


def parse_outbound_request(payload: Any) -> OutboundRequest:
    return _REQUEST_ADAPTER.validate_python(payload)


class PublicResult(StrictModel):
    status: PublicStatus
    action_id: UUID
    action_uid: UUID | None
    provider_request_ref: str | None
    retryable: Literal[False] = False
    detail_code: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")]
    # Human-readable elaboration of detail_code. None everywhere except traffic-control
    # blocks: that is the one path where the calling agent must read *why* (which
    # in-flight action or newer message) to decide skip vs. resend with override=true --
    # detail_code alone ("lease_held"/"stale_context") does not carry that. Left unset
    # (None) for every other result so existing consumers see no new key on the wire
    # (server.py omits it from the response payload when None).
    detail: str | None = None
