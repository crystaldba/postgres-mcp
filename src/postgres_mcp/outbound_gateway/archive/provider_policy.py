"""Archive of the retired provider-policy layer (staged-rollout blast-radius control).

Origin:
- src/postgres_mcp/outbound_gateway/server.py, lines 89-97 and 277-335
- src/postgres_mcp/outbound_gateway/models.py, lines 407-452

Archived: 2026-08-27

See ../archive/README.md for failure narrative and revival requirements.
"""

from __future__ import annotations

import json
import os

from postgres_mcp.outbound_gateway.models import ActionRole
from postgres_mcp.outbound_gateway.models import IntentKind
from postgres_mcp.outbound_gateway.models import Operation

DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER = {
    "hotpads": frozenset({Operation.EMAIL_SEND.value}),
    "zillow": frozenset({Operation.EMAIL_SEND.value}),
}
DEFAULT_ENABLED_INTENTS = frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value})
DEFAULT_ENABLED_INTENTS_BY_PROVIDER = {
    "hotpads": frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value}),
    "zillow": frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value}),
}

ALLOWED_COMBINATIONS: frozenset[tuple[ActionRole, Operation, IntentKind]] = frozenset(
    {
        (role, operation, intent)
        for role, operations, intents in (
            (
                ActionRole.PROSPECT_REPLY,
                (Operation.EMAIL_SEND, Operation.QUO_SMS_SEND),
                (
                    IntentKind.INQUIRY_REPLY,
                    IntentKind.SHOWING_OFFER,
                    IntentKind.SHOWING_CONFIRMATION,
                    IntentKind.SHOWING_RESCHEDULE,
                    IntentKind.SHOWING_CANCELLATION,
                ),
            ),
            (
                ActionRole.INTERNAL_NOTIFICATION,
                (Operation.CLIQ_CHANNEL_POST, Operation.CLIQ_CHAT_POST),
                (IntentKind.LEAD_ALERT, IntentKind.MANUAL_REVIEW_ALERT),
            ),
        )
        for operation in operations
        for intent in intents
    }
    | {
        (ActionRole.CALENDAR_MUTATION, Operation.CALENDAR_CREATE, IntentKind.SHOWING_CREATE),
        (ActionRole.CALENDAR_MUTATION, Operation.CALENDAR_UPDATE, IntentKind.SHOWING_UPDATE),
        (ActionRole.CALENDAR_MUTATION, Operation.CALENDAR_DELETE, IntentKind.SHOWING_DELETE),
        (ActionRole.PROSPECT_REPLY, Operation.TENANTCLOUD_MESSAGE_SEND, IntentKind.INQUIRY_REPLY),
        (
            ActionRole.PROVIDER_MUTATION,
            Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
            IntentKind.TENANTCLOUD_LEAD_STATUS,
        ),
        (
            ActionRole.PROVIDER_MUTATION,
            Operation.TENANTCLOUD_MAINTENANCE_CREATE,
            IntentKind.TENANTCLOUD_MAINTENANCE_CREATE,
        ),
        (
            ActionRole.PROVIDER_MUTATION,
            Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE,
            IntentKind.TENANTCLOUD_MAINTENANCE_STATUS,
        ),
    }
)


def _enabled_operations_by_provider() -> dict[str, frozenset[str]]:
    raw = os.environ.get("OUTBOUND_PROVIDER_OPERATIONS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER
    value = json.loads(raw)
    if not isinstance(value, dict) or not value:
        raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON must be a non-empty JSON object")
    parsed: dict[str, frozenset[str]] = {}
    for provider, operations in value.items():
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(operations, list)
            or not operations
            or not all(isinstance(item, str) for item in operations)
        ):
            raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON values must be non-empty string arrays")
        try:
            parsed[provider.casefold()] = frozenset(Operation(item).value for item in operations)
        except ValueError as exc:
            raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON contains an unsupported operation") from exc
    return parsed


def _enabled_intents() -> frozenset[str]:
    raw = os.environ.get("OUTBOUND_ENABLED_INTENTS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_INTENTS
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("OUTBOUND_ENABLED_INTENTS_JSON must be a non-empty string array")
    try:
        return frozenset(IntentKind(item).value for item in value)
    except ValueError as exc:
        raise ValueError("OUTBOUND_ENABLED_INTENTS_JSON contains an unsupported intent") from exc


def _enabled_intents_by_provider() -> dict[str, frozenset[str]]:
    raw = os.environ.get("OUTBOUND_PROVIDER_INTENTS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_INTENTS_BY_PROVIDER
    value = json.loads(raw)
    if not isinstance(value, dict) or not value:
        raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON must be a non-empty JSON object")
    parsed: dict[str, frozenset[str]] = {}
    for provider, intents in value.items():
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(intents, list)
            or not intents
            or not all(isinstance(item, str) for item in intents)
        ):
            raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON values must be non-empty string arrays")
        try:
            parsed[provider.casefold()] = frozenset(IntentKind(item).value for item in intents)
        except ValueError as exc:
            raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON contains an unsupported intent") from exc
    return parsed
