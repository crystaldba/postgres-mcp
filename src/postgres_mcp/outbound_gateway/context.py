"""Server-owned canonical context derivation for outbound actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from datetime import timezone
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from typing import Mapping
from typing import TypedDict
from unicodedata import normalize
from uuid import UUID
from uuid import uuid5

from .models import ActionRole
from .models import CalendarCreateArguments
from .models import CalendarDeleteArguments
from .models import CalendarUpdateArguments
from .models import CliqArguments
from .models import EmailArguments
from .models import ExecuteRequest
from .models import IntentKind
from .models import LeadStatusArguments
from .models import MaintenanceCreateArguments
from .models import MaintenanceStatusArguments
from .models import Operation
from .models import QuoSmsArguments
from .models import TenantCloudMessageArguments
from .repository import ContextRepository
from .repository import WakeEventRecord

ACTION_NAMESPACE = UUID("ed6fcf85-39e7-5cdf-9fb8-ccca32a62e8d")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SYSTEM_EMAIL_LOCAL_PARTS = frozenset({"mailer-daemon", "no-reply", "noreply", "do-not-reply", "donotreply", "postmaster"})
_ZILLOW_PROVIDER_FAMILY = frozenset({"hotpads", "zillow", "zumper"})
_EMAIL_PARTICIPANT_TYPES = frozenset({"email", "email_address"})
_PHONE_PARTICIPANT_TYPES = frozenset({"phone", "phone_number", "sms", "tel"})
_CROSS_CHANNEL_DUPLICATE_MAX_SECONDS = 120
_CERTIFIED_OLDER_MESSAGE_LIMIT = 100
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TENANTCLOUD_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_TENANTCLOUD_OPERATIONS = frozenset(
    {
        Operation.TENANTCLOUD_MESSAGE_SEND,
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        Operation.TENANTCLOUD_MAINTENANCE_CREATE,
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE,
    }
)
_ADDRESS_WORDS = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "st": "street",
    "rd": "road",
    "ave": "avenue",
    "blvd": "boulevard",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
}


class ContextDerivationError(ValueError):
    pass


@dataclass(frozen=True)
class DerivedTarget:
    kind: str
    target_id: str
    verified: bool


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    email_account_by_provider: Mapping[str, str]
    quo_line_by_provider: Mapping[str, str]
    calendar_by_profile: Mapping[str, str]
    cliq_target_by_intent: Mapping[str, str]
    property_aliases: Mapping[str, str]
    conversation_aliases: Mapping[str, str]
    calendar_account_by_profile: Mapping[str, str] = dataclass_field(default_factory=dict)
    enabled_operations_by_provider: Mapping[str, frozenset[str]] = dataclass_field(default_factory=dict)
    enabled_intents: frozenset[str] = frozenset()
    enabled_intents_by_provider: Mapping[str, frozenset[str]] = dataclass_field(default_factory=dict)


class SuggestionData(TypedDict):
    provider: str | None
    suggestions: dict[str, str]
    enabled_operations: list[str]
    enabled_intents: list[str]


@dataclass(frozen=True)
class ActionContext:
    action_id: UUID
    wakeup_event_id: int
    action_role: ActionRole
    operation: Operation
    intent_kind: IntentKind
    appointment_slot: datetime | None
    arguments: Mapping[str, Any]
    source: str
    source_message_id: int
    source_message_key: str
    source_sent_at: datetime
    conversation_id: str
    conversation_watermark: int
    prospect_id: str
    aliases: tuple[str, ...]
    property_id: str | None
    property_label: str | None
    target: DerivedTarget
    provider_account: str
    routing_policy_version: str
    canonical_scope: Mapping[str, Any]
    canonical_context: Mapping[str, Any]
    payload_hash: str
    lock_holder: str
    thread_identity: str
    showing_lifecycle_id: str
    calendar_event_uid: str | None
    source_subject: str | None = None
    prospect_name: str | None = None
    recipient_phone: str | None = None
    calendar_event_url: str | None = None
    calendar_event_etag: str | None = None
    channel_id: int = 0
    refresh_evidence: Mapping[str, Any] = dataclass_field(default_factory=dict)
    cross_channel_duplicate_message_ids: tuple[int, ...] = ()
    certified_older_message_ids: tuple[int, ...] = ()


def normalize_string(value: str) -> str:
    return normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def normalize_property_key(value: str | None) -> str:
    tokens = _NON_ALNUM.sub(" ", normalize_string(value or "").casefold()).split()
    return " ".join(_ADDRESS_WORDS.get(token, token) for token in tokens)


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if 10 < len(digits) <= 15 else None


def replyable_email(value: str | None, *, require_zillow_proxy: bool = False) -> str | None:
    address = _nonblank(value)
    if not address:
        return None
    address = address.casefold()
    if not _EMAIL.fullmatch(address):
        return None
    local_part, domain = address.rsplit("@", 1)
    normalized_local = local_part.replace("_", "-").replace(".", "-")
    if normalized_local in _SYSTEM_EMAIL_LOCAL_PARTS or any(
        token in normalized_local for token in ("do-not-reply", "donotreply", "no-reply", "noreply")
    ):
        return None
    if require_zillow_proxy and domain != "convo.zillow.com":
        return None
    return address


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = normalize_string(value).strip()
    return result or None


def _calendar_event_uid_from_url(event_url: str | None) -> str | None:
    """The CalDAV event URL's basename IS the event uid, e.g.
    https://calendar.zoho.com/caldav/<acct>/events/<uid>.ics -> <uid>
    (verified from a real create response). Format-only: does not fetch or
    otherwise verify the URL."""
    if not event_url:
        return None
    basename = event_url.rstrip("/").rsplit("/", 1)[-1]
    if basename.lower().endswith(".ics"):
        basename = basename[: -len(".ics")]
    return _nonblank(basename)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, str):
        return normalize_string(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonicalize(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class ActionContextLoader:
    def __init__(self, repository: ContextRepository, policy: RoutingPolicy):
        self._repository = repository
        self._policy = policy

    async def load(self, request: ExecuteRequest) -> ActionContext:
        record = await self._repository.load_wake_event(request.wakeup_event_id)
        if record is None:
            raise ContextDerivationError("wakeup event does not exist")
        envelope = _mapping(record.envelope)
        message = _mapping(envelope.get("message"))
        raw = _mapping(record.raw_payload)
        # The operation names its own provider ("tenantcloud.*" is always
        # provider "tenantcloud") -- for these four operations the wake's
        # message shape must not override that, or an execute against a
        # non-TenantCloud-shaped wake gets rejected by the allowlist gate
        # below, reintroducing exactly the wake-shape coupling this task
        # removes one layer up.
        provider = "tenantcloud" if request.operation in _TENANTCLOUD_OPERATIONS else self._provider(record, raw, message)
        if self._policy.enabled_operations_by_provider:
            allowed_operations = self._policy.enabled_operations_by_provider.get(
                provider,
                frozenset(),
            )
            if request.operation.value not in allowed_operations:
                raise ContextDerivationError("provider operation is disabled")
        if self._policy.enabled_intents and request.intent_kind.value not in self._policy.enabled_intents:
            raise ContextDerivationError("intent is disabled")
        if self._policy.enabled_intents_by_provider:
            provider_intents = self._policy.enabled_intents_by_provider.get(
                provider,
                frozenset(),
            )
            if request.intent_kind.value not in provider_intents:
                raise ContextDerivationError("provider intent is disabled")
        property_label = self._property_label(record, envelope, raw, message)
        property_scope = normalize_property_key(property_label)
        property_id = self._policy.property_aliases.get(property_scope)
        if property_scope and property_id is None:
            property_id = f"property:{property_scope.replace(' ', '-')}"
        if request.operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
            assert isinstance(request.arguments, MaintenanceCreateArguments)
            property_id = str(request.arguments.property_id)

        aliases = self._aliases(record, envelope, message, raw)
        if (
            not aliases
            and request.action_role is not ActionRole.INTERNAL_NOTIFICATION
            and request.operation not in _TENANTCLOUD_OPERATIONS
        ):
            raise ContextDerivationError("no stable prospect aliases")
        if aliases:
            resolved = await self._repository.resolve_canonical_subject(aliases, property_scope)
            if resolved.ambiguous:
                raise ContextDerivationError("prospect aliases are ambiguous")
            prospect_id = resolved.canonical_subject or f"prospect:{self._preferred_alias(aliases)}"
        else:
            prospect_id = (
                f"tenantcloud:claim:{record.tenantcloud_claim_id}"
                if record.tenantcloud_claim_id is not None
                else "internal:none"
            )

        thread_identity = self._thread_identity(record, raw, message, provider)
        conversation_provider = "zillow" if provider in _ZILLOW_PROVIDER_FAMILY else provider
        raw_conversation = f"{conversation_provider}:{thread_identity}"
        conversation_id = self._policy.conversation_aliases.get(
            raw_conversation,
            f"conversation:{raw_conversation}",
        )
        target, provider_account = self._target(
            request,
            record,
            provider,
            thread_identity,
            message,
            raw,
        )
        if not target.verified:
            raise ContextDerivationError("verified target could not be derived")

        requires_property = request.intent_kind not in {
            IntentKind.INQUIRY_REPLY,
            IntentKind.LEAD_ALERT,
            IntentKind.MANUAL_REVIEW_ALERT,
            IntentKind.TENANTCLOUD_LEAD_STATUS,
            IntentKind.TENANTCLOUD_MAINTENANCE_CREATE,
            IntentKind.TENANTCLOUD_MAINTENANCE_STATUS,
        }
        if requires_property and property_id is None:
            raise ContextDerivationError("verified property could not be derived")

        action_id = uuid5(
            ACTION_NAMESPACE,
            f"v1:wakeup:{request.wakeup_event_id}:role:{request.action_role}:ordinal:0",
        )
        showing_lifecycle_id = (
            _nonblank(raw.get("showing_lifecycle_id")) or _nonblank(raw.get("booking_id")) or f"showing:wake:{request.wakeup_event_id}"
        )
        booking = _mapping(raw.get("booking"))
        calendar_event_uid = _nonblank(raw.get("calendar_event_uid")) or _nonblank(booking.get("calendar_event_uid"))
        calendar_event_url = _nonblank(raw.get("calendar_event_url")) or _nonblank(booking.get("calendar_event_url"))
        calendar_event_etag = _nonblank(raw.get("calendar_event_etag")) or _nonblank(booking.get("calendar_event_etag"))
        if request.operation in {Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE}:
            # The agent names the exact event it means to mutate. The wake
            # payload -- verified against production to never carry
            # calendar_event_url on a single raw_events row -- is kept only
            # as a fallback for callers that still route event identity
            # through the wake, same as before this operation took agent-
            # supplied arguments.
            assert isinstance(request.arguments, (CalendarUpdateArguments, CalendarDeleteArguments))
            calendar_event_url = request.arguments.event_url or calendar_event_url
            calendar_event_etag = request.arguments.etag or calendar_event_etag
            calendar_event_uid = (
                request.arguments.event_uid
                or _calendar_event_uid_from_url(calendar_event_url)
                or calendar_event_uid
            )
            if not calendar_event_uid:
                raise ContextDerivationError("canonical calendar event UID is required")
            if not calendar_event_url or not calendar_event_etag:
                raise ContextDerivationError("canonical calendar event revision is required")

        source_subject = _nonblank(record.subject)
        prospect_name = _nonblank(message.get("prospect_name")) or _nonblank(record.display_name)
        if request.operation is Operation.QUO_SMS_SEND:
            # The agent's target is the actual SMS destination -- do not fall
            # back to a wake-derived phone, which is exactly the "channel is
            # a line, not a conversation" hazard this task removes.
            assert isinstance(request.arguments, QuoSmsArguments)
            recipient_phone = request.arguments.to_phone
        else:
            recipient_phone = normalize_phone(
                _nonblank(message.get("phone")) or (record.participant_key if record.participant_type in _PHONE_PARTICIPANT_TYPES else None)
            )
        refresh_evidence = _mapping(raw.get("zillow_refresh") or envelope.get("zillow_refresh"))
        cross_channel_duplicate_message_ids = self._cross_channel_duplicate_message_ids(
            record,
            envelope,
        )
        certified_older_message_ids = self._certified_older_message_ids(
            record,
            provider,
            refresh_evidence,
        )

        arguments = MappingProxyType(request.arguments.model_dump(mode="json", exclude_none=False))
        if request.operation in _TENANTCLOUD_OPERATIONS:
            desired_state_hash = canonical_payload_hash(dict(arguments))
            canonical_scope = {
                "version": "v1",
                "tenantcloud_claim_id": record.tenantcloud_claim_id or "",
                "source_event_id": record.source_event_id,
                "operation": request.operation.value,
                "target_id": target.target_id,
                "desired_state_hash": desired_state_hash,
            }
        else:
            canonical_scope = self._scope(
                request,
                record,
                prospect_id,
                conversation_id,
                property_id,
                target,
                showing_lifecycle_id,
                calendar_event_uid,
            )
        appointment_slot = request.appointment_slot
        canonical_message_id = record.canonical_message_id or record.message_id
        conversation_watermark = self._event_conversation_watermark(record, envelope)
        canonical_context_data = {
            "identity_version": "v1",
            "source_message_id": canonical_message_id,
            "source_message_key": f"{record.message_source}:{record.source_message_id}",
            "conversation_id": conversation_id,
            "conversation_watermark": conversation_watermark,
            "prospect_id": prospect_id,
            "property_id": property_id,
            "target": {"kind": target.kind, "target_id": target.target_id},
            "provider_account": provider_account,
            "routing_policy_version": self._policy.version,
            "showing_lifecycle_id": showing_lifecycle_id,
            "calendar_event_uid": calendar_event_uid,
            "calendar_event_url": calendar_event_url,
            "calendar_event_etag": calendar_event_etag,
            "source_subject": source_subject,
            "prospect_name": prospect_name,
            "recipient_phone": recipient_phone,
            "channel_id": record.channel_id,
            "refresh_evidence": refresh_evidence,
            "cross_channel_duplicate_message_ids": list(cross_channel_duplicate_message_ids),
            "certified_older_message_ids": list(certified_older_message_ids),
        }
        if request.operation in _TENANTCLOUD_OPERATIONS:
            canonical_context_data.update(
                tenantcloud_claim_id=record.tenantcloud_claim_id or "",
                tenantcloud_claim_family=record.tenantcloud_claim_family,
                source_event_id=record.source_event_id,
                operation_target={"kind": target.kind, "target_id": target.target_id},
            )
            if request.operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
                assert isinstance(request.arguments, MaintenanceCreateArguments)
                canonical_context_data["provider_ids"] = {
                    "property_id": str(request.arguments.property_id),
                    "unit_id": str(request.arguments.unit_id),
                }
        canonical_context = MappingProxyType(canonical_context_data)
        payload_hash = canonical_payload_hash(
            {
                "action_role": request.action_role.value,
                "operation": request.operation.value,
                "intent_kind": request.intent_kind.value,
                "appointment_slot": appointment_slot,
                "arguments": arguments,
                "canonical_context": canonical_context,
            }
        )
        return ActionContext(
            action_id=action_id,
            wakeup_event_id=request.wakeup_event_id,
            action_role=request.action_role,
            operation=request.operation,
            intent_kind=request.intent_kind,
            appointment_slot=appointment_slot,
            arguments=arguments,
            source=provider,
            source_message_id=canonical_message_id,
            source_message_key=f"{record.message_source}:{record.source_message_id}",
            source_sent_at=record.message_sent_at,
            conversation_id=conversation_id,
            conversation_watermark=conversation_watermark,
            prospect_id=prospect_id,
            aliases=aliases,
            property_id=property_id,
            property_label=property_label,
            target=target,
            provider_account=provider_account,
            routing_policy_version=self._policy.version,
            canonical_scope=MappingProxyType(canonical_scope),
            canonical_context=canonical_context,
            payload_hash=payload_hash,
            lock_holder=f"outbound-gateway:{action_id}",
            thread_identity=thread_identity,
            showing_lifecycle_id=showing_lifecycle_id,
            calendar_event_uid=calendar_event_uid,
            source_subject=source_subject,
            prospect_name=prospect_name,
            recipient_phone=recipient_phone,
            calendar_event_url=calendar_event_url,
            calendar_event_etag=calendar_event_etag,
            channel_id=record.channel_id,
            refresh_evidence=MappingProxyType(dict(refresh_evidence)),
            cross_channel_duplicate_message_ids=cross_channel_duplicate_message_ids,
            certified_older_message_ids=certified_older_message_ids,
        )

    async def suggest_targets(self, wakeup_event_id: int) -> dict[str, str]:
        """Best-effort target ids implied by a wake. Advisory only: never raises,
        returns {} when the wake implies nothing."""
        record = await self._repository.load_wake_event(wakeup_event_id)
        if record is None:
            return {}
        return self._suggest_targets(record)

    async def suggest(self, wakeup_event_id: int) -> SuggestionData:
        """Return advisory target hints and static routing capabilities."""
        record = await self._repository.load_wake_event(wakeup_event_id)
        if record is None:
            return self._empty_suggestion()
        envelope = _mapping(record.envelope)
        message = _mapping(envelope.get("message"))
        raw = _mapping(record.raw_payload)
        provider = self._provider(record, raw, message)
        if not provider:
            return self._empty_suggestion()
        return {
            "provider": provider,
            "suggestions": self._suggest_targets(record),
            "enabled_operations": self._enabled_operations_for_provider(provider),
            "enabled_intents": self._enabled_intents_for_provider(provider),
        }

    @staticmethod
    def _empty_suggestion() -> SuggestionData:
        return {
            "provider": None,
            "suggestions": {},
            "enabled_operations": [],
            "enabled_intents": [],
        }

    def _suggest_targets(self, record: WakeEventRecord) -> dict[str, str]:
        hints = dict(self._wake_tenantcloud_hints(record))
        hints.update(self._wake_recipient_hints(record))
        return hints

    def _enabled_operations_for_provider(self, provider: str) -> list[str]:
        if self._policy.enabled_operations_by_provider:
            operations = self._policy.enabled_operations_by_provider.get(provider, frozenset())
        else:
            operations = frozenset(operation.value for operation in Operation)
        return sorted(operations)

    def _enabled_intents_for_provider(self, provider: str) -> list[str]:
        intents = self._policy.enabled_intents or frozenset(intent.value for intent in IntentKind)
        if self._policy.enabled_intents_by_provider:
            intents = intents & self._policy.enabled_intents_by_provider.get(provider, frozenset())
        return sorted(intents)

    def _wake_recipient_hints(self, record: WakeEventRecord) -> dict[str, str]:
        """Best-effort email/phone recipient a wake implies -- the same
        proxy/direct/participant resolution (Zillow-proxy-only rule
        included) that used to gate EMAIL_SEND and QUO_SMS_SEND's execute().
        Advisory only: keys are present only when a value can be derived,
        never raises, and disagreeing with what the agent sends to execute
        is expected and not an error."""
        envelope = _mapping(record.envelope)
        message = _mapping(envelope.get("message"))
        raw = _mapping(record.raw_payload)
        provider = self._provider(record, raw, message)
        hints: dict[str, str] = {}
        to_address = self._wake_email_hint(record, provider, message, raw)
        if to_address:
            hints["to_address"] = to_address
        to_phone = normalize_phone(
            _nonblank(message.get("phone")) or (record.participant_key if record.participant_type in _PHONE_PARTICIPANT_TYPES else None)
        )
        if to_phone:
            hints["to_phone"] = to_phone
        return hints

    @staticmethod
    def _wake_email_hint(
        record: WakeEventRecord,
        provider: str,
        message: Mapping[str, Any],
        raw: Mapping[str, Any],
    ) -> str | None:
        proxy = replyable_email(
            _nonblank(message.get("proxy_email") or message.get("zillow_proxy_email") or raw.get("proxy_email") or raw.get("zillow_proxy_email")),
            require_zillow_proxy=provider in _ZILLOW_PROVIDER_FAMILY,
        )
        direct = replyable_email(_nonblank(message.get("direct_email")))
        participant = replyable_email(
            record.participant_key
            if record.participant_type in _EMAIL_PARTICIPANT_TYPES and record.channel_type in {"email", "email_thread"}
            else None,
            require_zillow_proxy=provider in _ZILLOW_PROVIDER_FAMILY,
        )
        return (proxy or participant) if provider in _ZILLOW_PROVIDER_FAMILY else (proxy or direct or participant)

    @staticmethod
    def _certified_older_message_ids(
        record: WakeEventRecord,
        provider: str,
        refresh_evidence: Mapping[str, Any],
    ) -> tuple[int, ...]:
        """Read replay-approved Zillow chronology corrections, fail closed."""

        raw_ids = refresh_evidence.get("certified_older_message_ids")
        if raw_ids is None:
            return ()
        if record.event_source != "outbound-replay" or provider not in _ZILLOW_PROVIDER_FAMILY:
            return ()
        evidence_hash = str(refresh_evidence.get("evidence_sha256") or "").casefold()
        if (
            refresh_evidence.get("status") not in {"covered", "browser_covered"}
            or not _SHA256_HEX.fullmatch(evidence_hash)
            or not _nonblank(refresh_evidence.get("covered_through"))
            or not _nonblank(refresh_evidence.get("covered_thread_identity"))
            or not isinstance(raw_ids, list)
            or len(raw_ids) > _CERTIFIED_OLDER_MESSAGE_LIMIT
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in raw_ids)
            or raw_ids != sorted(set(raw_ids))
        ):
            raise ContextDerivationError("invalid certified Zillow chronology evidence")
        return tuple(raw_ids)

    @staticmethod
    def _cross_channel_duplicate_message_ids(
        record: WakeEventRecord,
        envelope: Mapping[str, Any],
    ) -> tuple[int, ...]:
        routing_hints = _mapping(envelope.get("routing_hints"))
        hint = _mapping(routing_hints.get("potential_cross_channel_duplicate"))
        duplicate_id = hint.get("duplicate_of_message_id")
        duplicate_source = _nonblank(hint.get("duplicate_of_source"))
        duplicate_sent_at = _nonblank(hint.get("duplicate_of_sent_at"))
        if (
            isinstance(duplicate_id, bool)
            or not isinstance(duplicate_id, int)
            or duplicate_id <= 0
            or not duplicate_source
            or duplicate_source.casefold() == record.message_source.casefold()
            or not duplicate_sent_at
            or record.message_sent_at.tzinfo is None
        ):
            return ()
        try:
            parsed_sent_at = datetime.fromisoformat(duplicate_sent_at.replace("Z", "+00:00"))
        except ValueError:
            return ()
        if parsed_sent_at.tzinfo is None:
            return ()
        delta_seconds = abs(
            (
                parsed_sent_at.astimezone(timezone.utc)
                - record.message_sent_at.astimezone(timezone.utc)
            ).total_seconds()
        )
        if delta_seconds > _CROSS_CHANNEL_DUPLICATE_MAX_SECONDS:
            return ()
        return (duplicate_id,)

    @staticmethod
    def _provider(
        record: WakeEventRecord,
        raw: Mapping[str, Any],
        message: Mapping[str, Any],
    ) -> str:
        explicit = _nonblank(raw.get("provider"))
        if explicit:
            return explicit.casefold()
        participant_proxy = replyable_email(
            record.participant_key if record.participant_type in _EMAIL_PARTICIPANT_TYPES else None,
            require_zillow_proxy=True,
        )
        if (
            record.message_source == "zillow_rm_web_extract"
            or message.get("proxy_email")
            or message.get("zillow_proxy_email")
            or raw.get("proxy_email")
            or raw.get("zillow_proxy_email")
            or participant_proxy
        ):
            return "zillow"
        if record.message_source == "quo":
            return "quo"
        if record.message_source == "zoho_cliq":
            return "cliq"
        if record.message_source == "tenantcloud_api":
            return "tenantcloud"
        return record.message_source.casefold()

    @staticmethod
    def _property_label(
        record: WakeEventRecord,
        envelope: Mapping[str, Any],
        raw: Mapping[str, Any],
        message: Mapping[str, Any],
    ) -> str | None:
        direct = _nonblank(message.get("property")) or _nonblank(raw.get("property"))
        if direct:
            return direct
        proxy_email = replyable_email(
            _nonblank(message.get("proxy_email"))
            or _nonblank(message.get("zillow_proxy_email"))
            or _nonblank(raw.get("proxy_email"))
            or _nonblank(raw.get("zillow_proxy_email"))
            or (
                record.participant_key
                if record.participant_type in _EMAIL_PARTICIPANT_TYPES
                else None
            ),
            require_zillow_proxy=True,
        )
        nearby_messages = _mapping(envelope.get("conversation_context")).get(
            "nearby_messages",
            (),
        )
        if proxy_email and isinstance(nearby_messages, (list, tuple)):
            for nearby_value in nearby_messages:
                nearby = _mapping(nearby_value)
                nearby_proxy = replyable_email(
                    _nonblank(nearby.get("proxy_email"))
                    or _nonblank(nearby.get("zillow_proxy_email")),
                    require_zillow_proxy=True,
                )
                nearby_property = _nonblank(nearby.get("property"))
                if nearby_proxy == proxy_email and nearby_property:
                    return nearby_property
        subject = record.subject or ""
        match = re.search(r"\b(?:for|about)\s+(.+)$", subject, re.IGNORECASE)
        if not match:
            return None
        subject_property = re.sub(
            r",\s*[^,]+,\s*[A-Z]{2},\s*\d{5}(?:-\d{4})?\s*$",
            "",
            match.group(1),
            flags=re.IGNORECASE,
        )
        return _nonblank(subject_property)

    @staticmethod
    def _aliases(
        record: WakeEventRecord,
        envelope: Mapping[str, Any],
        message: Mapping[str, Any],
        raw: Mapping[str, Any],
    ) -> tuple[str, ...]:
        values: set[str] = set()
        identity = _mapping(envelope.get("identity"))
        factbook = _nonblank(identity.get("factbook_entity_uuid"))
        if factbook:
            values.add(f"factbook:{factbook.casefold()}")
        for candidate in (
            message.get("direct_email"),
            message.get("proxy_email"),
            record.participant_key if record.participant_type in _EMAIL_PARTICIPANT_TYPES else None,
        ):
            email = _nonblank(candidate)
            if email and _EMAIL.fullmatch(email):
                values.add(f"email:{email.casefold()}")
        phone = normalize_phone(
            _nonblank(message.get("phone")) or (record.participant_key if record.participant_type in _PHONE_PARTICIPANT_TYPES else None)
        )
        if phone:
            values.add(f"phone:{phone}")
        for field in ("prospect_id", "contact_id", "zillow_contact_id"):
            value = _nonblank(raw.get(field))
            if value:
                values.add(f"{field}:{value.casefold()}")
        return tuple(sorted(values))

    @staticmethod
    def _preferred_alias(aliases: tuple[str, ...]) -> str:
        for prefix in ("factbook:", "prospect_id:", "contact_id:", "email:", "phone:"):
            match = next((alias for alias in aliases if alias.startswith(prefix)), None)
            if match:
                return match
        return aliases[0]

    @staticmethod
    def _thread_identity(
        record: WakeEventRecord,
        raw: Mapping[str, Any],
        message: Mapping[str, Any],
        provider: str,
    ) -> str:
        if provider in _ZILLOW_PROVIDER_FAMILY:
            for candidate in (
                message.get("proxy_email"),
                message.get("zillow_proxy_email"),
                raw.get("proxy_email"),
                raw.get("zillow_proxy_email"),
                record.participant_key if record.participant_type in _EMAIL_PARTICIPANT_TYPES else None,
            ):
                proxy = replyable_email(
                    _nonblank(candidate),
                    require_zillow_proxy=True,
                )
                if proxy:
                    return proxy
        nested = _mapping(_mapping(raw.get("data")).get("object"))
        if provider == "quo":
            conversation = _nonblank(nested.get("conversationId") or nested.get("conversation_id"))
            if conversation:
                return conversation
            line = _nonblank(nested.get("phoneNumberId") or nested.get("phone_number_id") or nested.get("to"))
            prospect = normalize_phone(
                _nonblank(message.get("phone")) or (record.participant_key if record.participant_type in _PHONE_PARTICIPANT_TYPES else None)
            )
            if line and prospect:
                return f"line:{line.casefold()}:prospect:{prospect}"
        for field in ("thread_id", "conversation_id", "channel_id"):
            value = _nonblank(raw.get(field))
            if value:
                return value
        return record.source_channel_id

    @staticmethod
    def _event_conversation_watermark(
        record: WakeEventRecord,
        envelope: Mapping[str, Any],
    ) -> int:
        conversation = _mapping(envelope.get("conversation_context"))
        nearby = conversation.get("nearby_messages")
        message_ids = [record.message_id]
        if isinstance(nearby, list):
            message_ids.extend(value for item in nearby if isinstance(item, Mapping) and isinstance((value := item.get("id")), int))
        return max(message_ids)

    def _target(
        self,
        request: ExecuteRequest,
        record: WakeEventRecord,
        provider: str,
        thread_identity: str,
        message: Mapping[str, Any],
        raw: Mapping[str, Any],
    ) -> tuple[DerivedTarget, str]:
        if request.operation is Operation.TENANTCLOUD_MESSAGE_SEND:
            assert isinstance(request.arguments, TenantCloudMessageArguments)
            return DerivedTarget("tenantcloud_thread", str(request.arguments.thread_id), True), "tenantcloud"
        if request.operation is Operation.TENANTCLOUD_LEAD_STATUS_UPDATE:
            assert isinstance(request.arguments, LeadStatusArguments)
            return DerivedTarget("tenantcloud_lead", str(request.arguments.lead_id), True), "tenantcloud"
        if request.operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
            assert isinstance(request.arguments, MaintenanceCreateArguments)
            target_id = f"property:{request.arguments.property_id}:unit:{request.arguments.unit_id}"
            return DerivedTarget("tenantcloud_property_unit", target_id, True), "tenantcloud"
        if request.operation is Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE:
            assert isinstance(request.arguments, MaintenanceStatusArguments)
            return DerivedTarget("tenantcloud_maintenance_request", str(request.arguments.request_id), True), "tenantcloud"
        if request.operation is Operation.EMAIL_SEND:
            assert isinstance(request.arguments, EmailArguments)
            account = self._policy.email_account_by_provider.get(provider, "")
            return DerivedTarget("email_thread", request.arguments.to_address, True), account
        if request.operation is Operation.QUO_SMS_SEND:
            assert isinstance(request.arguments, QuoSmsArguments)
            configured_account = self._policy.quo_line_by_provider.get(provider, "")
            nested = _mapping(_mapping(raw.get("data")).get("object"))
            observed_account = _nonblank(nested.get("phoneNumberId") or nested.get("phone_number_id"))
            observed_direction = _nonblank(nested.get("direction"))
            observed_inbound = str(observed_direction or "").casefold() in {
                "inbound",
                "incoming",
                "received",
            }
            # A Quo inbound webhook is server-side provider evidence for the
            # receiving line.  Use that exact line for replies; one
            # configured default cannot represent multiple PFG lines. This is
            # account/line selection, not recipient identity -- the agent's
            # to_phone (above) is the only thing that decides who receives
            # the message.
            account = (
                observed_account
                if provider == "quo" and observed_account and observed_inbound
                else configured_account
            )
            return DerivedTarget("quo_conversation", request.arguments.to_phone, True), account
        if request.operation in {Operation.CLIQ_CHANNEL_POST, Operation.CLIQ_CHAT_POST}:
            assert isinstance(request.arguments, CliqArguments)
            kind = "cliq_channel" if request.operation is Operation.CLIQ_CHANNEL_POST else "cliq_chat"
            return DerivedTarget(kind, request.arguments.channel_or_chat_id, True), request.arguments.channel_or_chat_id
        assert isinstance(request.arguments, (CalendarCreateArguments, CalendarUpdateArguments, CalendarDeleteArguments))
        profile = "appointment-setter"
        configured_calendar = self._policy.calendar_by_profile.get(profile, "")
        account = self._policy.calendar_account_by_profile.get(profile, configured_calendar)
        return DerivedTarget("calendar", request.arguments.calendar_id, True), account

    def _wake_tenantcloud_hints(self, record: WakeEventRecord) -> dict[str, str]:
        """Best-effort provider ids implied by a wake's TenantCloud claim
        linkage. Advisory only -- never raises, and unlike the retired
        execute-blocking gate this does NOT require the claim to be
        tenantcloud_api-owned, claimed/running, or event-source/message-
        source/channel-consistent: a read-only hint is exactly as safe to
        offer for a legacy-owned or completed claim as for an active one
        (in production 120 of 142 real claims are legacy-owned or
        inactive). It still refuses to emit an id it cannot parse, or one
        that might belong to the wrong claim/family."""
        provider_ids: dict[str, str] = {}
        provider_ids.update(self._tenantcloud_entity_ids_hint(record))
        provider_ids.update(self._tenantcloud_entity_scope_key_hint(record))
        return provider_ids

    @staticmethod
    def _tenantcloud_entity_ids_hint(record: WakeEventRecord) -> dict[str, str]:
        """Belt-and-braces reading of the claim's own entity_ids, kept for
        any claim that happens to carry them (0 of 142 real claims do --
        see _tenantcloud_entity_scope_key_hint for where ids actually
        live). Claim-identity and family agreement are id-*correctness*
        checks, not ownership gating, so they still apply: entity_ids that
        can't be tied to this exact claim, or whose keys don't belong to
        the claim's own family, are not trustworthy enough to hint from."""
        envelope = _mapping(record.envelope)
        trusted = _mapping(envelope.get("tenantcloud"))
        row_claim_id = record.tenantcloud_claim_id
        envelope_claim_id = trusted.get("claim_id")
        if (
            type(row_claim_id) is not int
            or row_claim_id <= 0
            or row_claim_id > 9_223_372_036_854_775_807
            or type(envelope_claim_id) is not int
            or envelope_claim_id != row_claim_id
        ):
            return {}
        family = record.tenantcloud_claim_family
        if family not in {"lead", "maintenance"} or trusted.get("event_family") != family:
            return {}

        raw_ids = trusted.get("entity_ids")
        if not isinstance(raw_ids, Mapping):
            return {}
        allowed_keys = {"lead_id", "listing_id", "thread_id"} if family == "lead" else {"request_id", "property_id", "unit_id", "thread_id"}
        if any(type(key) is not str or key not in allowed_keys for key in raw_ids):
            return {}
        provider_ids: dict[str, str] = {}
        for key, value in raw_ids.items():
            if type(value) is int:
                candidate = str(value)
            elif type(value) is str:
                candidate = value
            else:
                return {}
            if not _TENANTCLOUD_ID.fullmatch(candidate):
                return {}
            parsed = int(candidate)
            if parsed > 9_223_372_036_854_775_807:
                return {}
            provider_ids[key] = str(parsed)
        return provider_ids

    @staticmethod
    def _tenantcloud_entity_scope_key_hint(record: WakeEventRecord) -> dict[str, str]:
        """Parse tenantcloud_event_claims.entity_scope_key's `prefix:id`
        shape -- this is where production ids actually live (FIX 1(b): 0 of
        142 real claims carry entity_ids). The scope key is read straight
        off the claim row joined by the wake's own tenantcloud_claim_id, so
        (unlike entity_ids, which arrives via the envelope) there is no
        separate claim-identity to agree with. Only a prefix that names a
        lead or a maintenance request contributes an id; a property-address
        slug or a phone number contributes nothing even when its tail is
        numeric."""
        scope_key = _nonblank(getattr(record, "tenantcloud_entity_scope_key", None))
        if not scope_key:
            return {}
        *prefix_parts, tail = scope_key.split(":")
        if not prefix_parts or not _TENANTCLOUD_ID.fullmatch(tail):
            return {}
        if int(tail) > 9_223_372_036_854_775_807:
            return {}
        prefix = "-".join(prefix_parts).casefold()
        is_lead = "lead" in prefix or "prospect" in prefix
        is_maintenance = "maintenance" in prefix and "request" in prefix
        if is_lead and not is_maintenance:
            return {"lead_id": tail}
        if is_maintenance and not is_lead:
            return {"request_id": tail}
        return {}

    @staticmethod
    def _scope(
        request: ExecuteRequest,
        record: WakeEventRecord,
        prospect_id: str,
        conversation_id: str,
        property_id: str | None,
        target: DerivedTarget,
        showing_lifecycle_id: str,
        calendar_event_uid: str | None,
    ) -> dict[str, Any]:
        if request.action_role is ActionRole.PROSPECT_REPLY:
            return {
                "version": "v1",
                "prospect_id": prospect_id,
                "conversation_id": conversation_id,
                "source_message_id": record.canonical_message_id or record.message_id,
                "role": request.action_role.value,
            }
        if request.action_role is ActionRole.CALENDAR_MUTATION:
            return {
                "version": "v1",
                "prospect_id": prospect_id,
                "property_id": property_id,
                "showing_lifecycle_id": showing_lifecycle_id,
                "calendar_event_uid": calendar_event_uid,
                "trigger_event_id": record.wakeup_event_id,
                "operation": request.operation.value,
            }
        return {
            "version": "v1",
            "source_message_id": record.canonical_message_id or record.message_id,
            "target_id": target.target_id,
            "intent_kind": request.intent_kind.value,
            "role": request.action_role.value,
        }
