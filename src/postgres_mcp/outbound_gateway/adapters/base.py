"""Provider-neutral adapter protocol and result normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from typing import Any
from typing import Mapping
from typing import Protocol
from uuid import UUID

from ..context import ActionContext
from ..provider_client import McpCallResult
from ..provider_client import McpProviderClient
from ..provider_client import TransportErrorKind


class ProviderDisposition(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DEFINITIVE_NON_ACCEPTANCE = "definitive_non_acceptance"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ProviderRequest:
    server_name: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderObservation:
    disposition: ProviderDisposition
    detail_code: str
    provider_request_ref: str | None = None
    provider_call_id: str | None = None
    message_id: str | None = None
    accepted_at: datetime | None = None
    category: str | None = None
    retryable: bool = False
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProviderReceipt:
    provider_request_ref: str
    provider_message_id: str
    accepted_at: datetime
    evidence: Mapping[str, Any]


class ProviderAdapter(Protocol):
    def validate(self, context: ActionContext) -> None: ...

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest: ...

    async def invoke(self, client: McpProviderClient, request: ProviderRequest) -> ProviderObservation: ...

    async def poll(self, client: McpProviderClient, observation: ProviderObservation) -> ProviderObservation: ...

    def parse_receipt(self, context: ActionContext, observation: ProviderObservation) -> ProviderReceipt | None: ...

    async def reconcile(
        self,
        client: McpProviderClient,
        context: ActionContext,
        action_uid: UUID,
        observation: ProviderObservation,
    ) -> ProviderObservation: ...


def request_ref(payload: Mapping[str, Any] | None) -> str | None:
    if not payload:
        return None
    for key in ("request_id", "call_id", "provider_request_ref"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def terminal_content(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not payload:
        return None
    # Agent Email exposes queued terminal data as `result`; older releases and
    # recorded evidence used `completed_result`. Accept both envelopes so the
    # gateway follows the provider contract without coupling every adapter to a
    # queue-server version.
    for envelope_key in ("result", "completed_result"):
        envelope = payload.get(envelope_key)
        if not isinstance(envelope, Mapping):
            continue
        structured = envelope.get("structured_content")
        if isinstance(structured, Mapping):
            return structured
    return payload


def json_objects(value: Any):
    """Yield nested objects plus JSON objects encoded in MCP text blocks."""
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from json_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from json_objects(nested)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return
        yield from json_objects(decoded)


def mcp_text_parts(result: McpCallResult) -> tuple[str, ...]:
    """Extract distinct text blocks from direct or queued MCP envelopes."""
    parts = [result.text] if result.text else []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                parts.append(value["text"])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(terminal_content(result.structured_content))
    return tuple(dict.fromkeys(part for part in parts if part))


def mcp_text(result: McpCallResult) -> str:
    """Join direct and queued MCP text blocks for diagnostics."""

    return "\n".join(mcp_text_parts(result))


def transport_observation(result: McpCallResult) -> ProviderObservation | None:
    if result.error_kind is TransportErrorKind.AUTH_REJECTED:
        return ProviderObservation(
            ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
            "provider_auth_rejected",
            category="provider_authentication",
            retryable=True,
            evidence={"kind": "http_auth_rejection"},
        )
    if result.error_kind is TransportErrorKind.TIMEOUT:
        return ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_timeout")
    if result.error_kind is TransportErrorKind.CONNECTION_LOST:
        return ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_connection_lost")
    if result.error_kind is TransportErrorKind.TRANSPORT:
        return ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_transport_error")
    return None


def initial_observation(result: McpCallResult) -> ProviderObservation | None:
    transport = transport_observation(result)
    if transport is not None:
        return transport
    payload = result.structured_content
    ref = request_ref(payload)
    status = payload.get("status") if payload else None
    if status in {"pending", "running"}:
        return ProviderObservation(
            ProviderDisposition.PENDING,
            "provider_pending",
            provider_request_ref=ref,
            provider_call_id=str(payload.get("call_id") or ref) if payload else ref,
        )
    if status in {"failed", "lost"}:
        category = payload.get("category") if payload else None
        category = category if isinstance(category, str) else "request_lost" if status == "lost" else "provider_failure"
        # Queue failure proves only that the provider call did not produce a
        # usable receipt. It does not prove non-acceptance: SMTP, calendar, or
        # messaging providers can accept an effect before the queue reports a
        # terminal error. Keep the lock and reconcile. Individual adapters may
        # emit DEFINITIVE_NON_ACCEPTANCE only when their provider contract
        # supplies authoritative evidence that no effect occurred.
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "provider_request_lost" if status == "lost" else f"provider_{category}",
            provider_request_ref=ref,
            category=category,
            retryable=bool(payload.get("retryable")) if payload else False,
            evidence={"status": status, "category": category},
        )
    if result.is_error:
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "provider_mcp_error",
            provider_request_ref=ref,
        )
    return None


def accepted_observation(
    *,
    request_ref_value: str | None,
    message_id: str,
    detail_code: str = "provider_accepted",
    evidence: Mapping[str, Any] | None = None,
) -> ProviderObservation:
    ref = request_ref_value or message_id
    return ProviderObservation(
        ProviderDisposition.ACCEPTED,
        detail_code,
        provider_request_ref=ref,
        provider_call_id=request_ref_value,
        message_id=message_id,
        accepted_at=datetime.now(timezone.utc),
        evidence=evidence or {"kind": "provider_message_id", "provider_message_id": message_id},
    )


def receipt_from_observation(observation: ProviderObservation) -> ProviderReceipt | None:
    if (
        observation.disposition is not ProviderDisposition.ACCEPTED
        or not observation.provider_request_ref
        or not observation.message_id
        or not observation.accepted_at
    ):
        return None
    return ProviderReceipt(
        provider_request_ref=observation.provider_request_ref,
        provider_message_id=observation.message_id,
        accepted_at=observation.accepted_at,
        evidence=observation.evidence or {},
    )
