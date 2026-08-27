"""PostgreSQL persistence adapter for durable outbound actions."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from typing import Mapping
from uuid import UUID

from postgres_mcp.sql import SafeSqlDriver

from .adapters.base import ProviderObservation
from .adapters.base import ProviderReceipt
from .context import ActionContext
from .models import ActionRole
from .models import ActionState
from .models import CompletionKind
from .models import IntentKind
from .models import Operation
from .service import OutboundActionRecord
from .tenantcloud_shared import EVIDENCE_KIND_VERIFIED_READBACK
from .tenantcloud_shared import READBACK_OBSERVATION_KEYS
from .tenantcloud_shared import TENANTCLOUD_OPERATIONS
from .tenantcloud_shared import tenantcloud_persisted_arguments

# The seven keys the TenantCloud adapter attaches to an ACCEPTED
# ProviderObservation's evidence: migration 118's required six-key
# p_observation shape (READBACK_OBSERVATION_KEYS) plus the facade's own
# opaque evidence_hash, which this store strips out and sends separately as
# p_evidence_hash (118_...sql:353-364 rejects any extra key inside
# p_observation itself).
_ADAPTER_EVIDENCE_KEYS = READBACK_OBSERVATION_KEYS | {"evidence_hash"}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Mapping[str, Any]) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _observation(value: ProviderObservation) -> dict[str, Any]:
    return {
        "detail_code": value.detail_code,
        "disposition": value.disposition.value,
    }


def _verified_readback_evidence(value: ProviderObservation) -> Mapping[str, Any] | None:
    evidence = value.evidence
    if not isinstance(evidence, Mapping) or set(evidence) != _ADAPTER_EVIDENCE_KEYS:
        return None
    return evidence


class PostgresActionStore:
    def __init__(self, driver: Any):
        self._driver = driver

    async def _one(self, query: str, params: list[Any]) -> OutboundActionRecord:
        rows = await SafeSqlDriver.execute_param_query(self._driver, query, params)  # type: ignore[arg-type]
        if not rows:
            raise LookupError("outbound action database function returned no row")
        return await self._hydrated_record(rows[0].cells)

    async def _hydrated_record(self, cells: Mapping[str, Any]) -> OutboundActionRecord:
        """Merge in the TenantCloud acceptance-attempt evidence that
        ``transition_outbound_action`` writes to ``outbound_action_attempts``
        (evidence_kind/evidence_reference/evidence_hash/provider_observation)
        -- migration 118 adds no such columns to ``outbound_actions`` itself,
        and ``transition_outbound_action`` RETURNS SETOF outbound_actions, so
        this evidence is only reachable via a follow-up read of the attempt
        row the transition just (or previously) wrote."""
        merged = dict(cells)
        if str(cells.get("state")) == ActionState.PROVIDER_ACCEPTED.value and str(cells.get("operation")) in {
            operation.value for operation in TENANTCLOUD_OPERATIONS
        }:
            attempt = await self._latest_provider_accepted_attempt(
                UUID(str(cells["action_id"])), int(cells.get("attempt_count") or 0)
            )
            if attempt is not None:
                merged.update(attempt)
        return self._record(merged)

    async def _latest_provider_accepted_attempt(
        self, action_id: UUID, attempt_number: int
    ) -> dict[str, Any] | None:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT evidence_kind, evidence_reference, evidence_hash, provider_observation
            FROM outbound_action_attempts
            WHERE action_id = {} AND attempt_number = {} AND to_state = 'provider_accepted'
            ORDER BY attempt_id DESC
            LIMIT 1
            """,
            [action_id, attempt_number],
        )
        if not rows:
            return None
        cells = rows[0].cells
        return {
            "evidence_kind": cells.get("evidence_kind"),
            "evidence_reference": cells.get("evidence_reference"),
            "evidence_hash": cells.get("evidence_hash"),
            "provider_observation": cells.get("provider_observation"),
        }

    async def create_or_load(self, context: ActionContext) -> OutboundActionRecord:
        recipient_scope = {
            "kind": context.target.kind,
            "target_id": context.target.target_id,
            "verified": context.target.verified,
        }
        return await self._one(
            """
            SELECT * FROM create_or_load_outbound_action(
                {}, {}, {}, {}, {}, {}, {}::jsonb, {}, {}::jsonb,
                {}::jsonb, {}, {}, {}::jsonb
            )
            """,
            [
                context.wakeup_event_id,
                context.action_role.value,
                context.operation.value,
                context.intent_kind.value,
                context.appointment_slot,
                context.payload_hash,
                _json(context.canonical_context),
                context.source_message_id,
                _json(context.canonical_scope),
                _json(recipient_scope),
                context.provider_account,
                context.routing_policy_version,
                # Non-TenantCloud operations get context.arguments back
                # unchanged. TenantCloud operations are enriched with
                # desired_state/target_reference/idempotency_key so
                # migration 118's acceptance guard (which reads these
                # straight off outbound_actions.arguments) can compare
                # against them later -- see tenantcloud_shared.py.
                _json(tenantcloud_persisted_arguments(context)),
            ],
        )

    async def prepare(self, context: ActionContext, expected_state: ActionState) -> OutboundActionRecord:
        slot = context.appointment_slot.isoformat() if context.appointment_slot else ""
        if context.action_role is ActionRole.PROSPECT_REPLY:
            lock_intent = f"{context.intent_kind.value}:turn:{context.source_message_id}"
        elif context.action_role is ActionRole.CALENDAR_MUTATION:
            lock_intent = f"{context.intent_kind.value}:lifecycle:{context.showing_lifecycle_id}"
        elif context.action_role is ActionRole.PROVIDER_MUTATION:
            claim_id = context.canonical_context["tenantcloud_claim_id"]
            source_id = context.canonical_context["source_event_id"]
            desired_hash = context.canonical_scope["desired_state_hash"]
            prefix = f"v1:claim:{claim_id}:source:{source_id}:op:{context.operation.value}:target:{context.target.target_id}"
            if context.operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
                provider_ids = context.canonical_context["provider_ids"]
                normalized_text_hash = sha256(str(context.arguments["text"]).encode("utf-8")).hexdigest()
                lock_intent = (
                    f"{prefix}:property:{provider_ids['property_id']}:unit:{provider_ids['unit_id']}:"
                    f"category:{context.arguments['category_id']}:"
                    f"initiated:{context.arguments['initiated_at']}:"
                    f"text:{normalized_text_hash}:state:{desired_hash}"
                )
            else:
                lock_intent = f"{prefix}:state:{desired_hash}"
        else:
            lock_intent = f"{context.intent_kind.value}:event:{context.wakeup_event_id}"
        return await self._one(
            """
            SELECT * FROM prepare_outbound_action_and_acquire_lock(
                {}, {}, {}, {}, {}, {}, {}, {}, 900, 86400
            )
            """,
            [
                context.action_id,
                expected_state.value,
                context.prospect_id,
                context.property_id or context.property_label or "",
                lock_intent,
                slot,
                list(context.aliases),
                bool(slot and context.property_id),
            ],
        )

    async def claim(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str,
        lease_seconds: int,
    ) -> OutboundActionRecord:
        return await self._one(
            "SELECT * FROM claim_outbound_action({}, {}, {}, {})",
            [action_id, expected_state.value, lease_owner, lease_seconds],
        )

    async def record_provider_request(
        self,
        action_id: UUID,
        lease_owner: str,
        observation: ProviderObservation,
    ) -> OutboundActionRecord:
        return await self._one(
            "SELECT * FROM record_outbound_provider_request({}, {}, {}, {}, {}::jsonb)",
            [
                action_id,
                lease_owner,
                observation.provider_call_id,
                observation.provider_request_ref,
                _json(_observation(observation)),
            ],
        )

    async def transition(
        self,
        action_id: UUID,
        expected_state: ActionState,
        next_state: ActionState,
        lease_owner: str | None,
        observation: ProviderObservation,
    ) -> OutboundActionRecord:
        authoritative = next_state is ActionState.RETRY_READY
        verified_readback = (
            _verified_readback_evidence(observation) if next_state is ActionState.PROVIDER_ACCEPTED else None
        )
        sanitized = _observation(observation)
        if authoritative:
            evidence = dict(observation.evidence or {})
            evidence_kind = "authoritative_non_acceptance"
            evidence_reference = observation.provider_request_ref or observation.detail_code
            evidence_hash = _hash(evidence)
        elif verified_readback is not None:
            # Migration 118's acceptance guard requires p_observation to
            # contain *exactly* six keys (READBACK_OBSERVATION_KEYS) -- the
            # adapter's evidence carries those six plus the facade's own
            # opaque evidence_hash as a seventh; that seventh key must be
            # stripped out before it becomes p_observation, and forwarded
            # unmodified as p_evidence_hash (118_...sql:353-364,
            # transition_outbound_action's INSERT into
            # tenantcloud_gateway_acceptance_bindings correlates on this
            # exact opaque value, not a store-derived one).
            if not observation.provider_request_ref:
                raise ValueError(
                    "verified TenantCloud readback requires a provider_request_ref "
                    "(no fallback -- migration 118 requires evidence_reference == provider_request_ref)"
                )
            evidence_kind = EVIDENCE_KIND_VERIFIED_READBACK
            evidence_reference = observation.provider_request_ref
            evidence_hash = verified_readback["evidence_hash"]
            sanitized = {key: verified_readback[key] for key in READBACK_OBSERVATION_KEYS}
        else:
            evidence_kind = None
            evidence_reference = None
            evidence_hash = None
        return await self._one(
            """
            SELECT * FROM transition_outbound_action(
                {}, {}, {}, {}, {}, {}, {}, {}, {}::jsonb,
                {}, {}, {}, {}, {}
            )
            """,
            [
                action_id,
                expected_state.value,
                next_state.value,
                lease_owner,
                observation.detail_code,
                observation.provider_call_id,
                observation.provider_request_ref,
                observation.message_id,
                _json(sanitized),
                evidence_kind,
                evidence_reference,
                evidence_hash,
                observation.category,
                observation.detail_code if next_state is ActionState.UNKNOWN else None,
            ],
        )

    async def complete(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str | None,
        receipt: ProviderReceipt,
        completion_kind: CompletionKind,
        detail_code: str,
    ) -> OutboundActionRecord:
        evidence = {
            "accepted_at": receipt.accepted_at.isoformat(),
            "evidence": dict(receipt.evidence),
            "provider_message_id": receipt.provider_message_id,
            "provider_request_ref": receipt.provider_request_ref,
        }
        return await self._one(
            """
            SELECT * FROM complete_outbound_action(
                {}, {}, {}, {}, {}, {}::jsonb, {}, {}, {}
            )
            """,
            [
                action_id,
                expected_state.value,
                lease_owner,
                receipt.provider_request_ref,
                receipt.provider_message_id,
                _json(evidence),
                completion_kind.value,
                _hash(evidence),
                detail_code,
            ],
        )

    async def definitive_fail(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str,
        observation: ProviderObservation,
    ) -> OutboundActionRecord:
        evidence = dict(observation.evidence or {})
        reference = observation.provider_request_ref or observation.detail_code
        return await self._one(
            """
            SELECT * FROM definitively_fail_outbound_action(
                {}, {}, {}, 'authoritative_non_acceptance', {}, {}, {}, {}
            )
            """,
            [
                action_id,
                expected_state.value,
                lease_owner,
                reference,
                _hash(evidence),
                observation.category or "provider_non_acceptance",
                observation.detail_code,
            ],
        )

    async def get(self, action_id: UUID) -> OutboundActionRecord | None:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            "SELECT * FROM outbound_actions WHERE action_id = {}",
            [action_id],
        )
        return await self._hydrated_record(rows[0].cells) if rows else None

    async def schedule_next_attempt(
        self,
        action_id: UUID,
        expected_state: ActionState,
        delay_seconds: int,
        detail_code: str,
    ) -> OutboundActionRecord:
        return await self._one(
            "SELECT * FROM schedule_outbound_action_attempt({}, {}, {}, {})",
            [action_id, expected_state.value, delay_seconds, detail_code],
        )

    async def list_work(self, limit: int, max_attempts: int) -> list[tuple[UUID, ActionState]]:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT action_id, state
            FROM outbound_actions
            WHERE state IN (
                'dependency_wait', 'prepared', 'dispatching', 'provider_accepted',
                'unknown', 'reconciling', 'retry_ready'
            )
              AND (lease_owner IS NULL OR lease_expires_at <= now())
              AND next_attempt_at <= now()
              AND attempt_count < {}
            ORDER BY updated_at, action_id
            LIMIT {}
            """,
            [max_attempts, limit],
        )
        return [(UUID(str(row.cells["action_id"])), ActionState(str(row.cells["state"]))) for row in rows or []]

    async def list_exhausted(self, limit: int, max_attempts: int) -> list[tuple[UUID, ActionState]]:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT action_id, state
            FROM outbound_actions
            WHERE state IN (
                'dependency_wait', 'prepared', 'dispatching', 'provider_accepted',
                'unknown', 'reconciling', 'retry_ready'
            )
              AND (lease_owner IS NULL OR lease_expires_at <= now())
              AND attempt_count >= {}
            ORDER BY updated_at, action_id
            LIMIT {}
            """,
            [max_attempts, limit],
        )
        return [(UUID(str(row.cells["action_id"])), ActionState(str(row.cells["state"]))) for row in rows or []]

    @staticmethod
    def _record(cells: Mapping[str, Any]) -> OutboundActionRecord:
        completion = cells.get("completion_kind")
        action_uid = cells.get("action_uid")
        # provider_observation comes straight from outbound_action_attempts
        # (see _latest_provider_accepted_attempt) and, for a TenantCloud
        # verified-readback attempt, *is* the six-key payload itself -- no
        # wrapper key to unwrap.
        provider_observation = cells.get("provider_observation")
        readback_evidence = provider_observation if isinstance(provider_observation, Mapping) else None
        return OutboundActionRecord(
            action_id=UUID(str(cells["action_id"])),
            wakeup_event_id=int(cells["wakeup_event_id"]),
            action_role=ActionRole(str(cells["action_role"])),
            operation=Operation(str(cells["operation"])),
            intent_kind=IntentKind(str(cells["intent_kind"])),
            appointment_slot=cells.get("appointment_slot"),
            arguments=dict(cells.get("arguments") or {}),
            state=ActionState(str(cells["state"])),
            action_uid=UUID(str(action_uid)) if action_uid else None,
            provider_request_ref=cells.get("provider_request_ref"),
            provider_message_id=cells.get("provider_message_id"),
            provider_accepted_at=cells.get("provider_accepted_at"),
            completion_kind=CompletionKind(str(completion)) if completion else None,
            detail_code=str(cells["detail_code"]),
            attempt_count=int(cells.get("attempt_count") or 0),
            next_attempt_at=cells["next_attempt_at"],
            payload_hash=str(cells.get("payload_hash") or ""),
            canonical_context=dict(cells.get("canonical_context") or {}),
            canonical_scope=dict(cells.get("canonical_scope") or {}),
            recipient_scope=dict(cells.get("recipient_scope") or {}),
            provider_account=str(cells.get("provider_account") or ""),
            routing_policy_version=str(cells.get("routing_policy_version") or ""),
            provider_evidence_kind=cells.get("evidence_kind"),
            provider_evidence_reference=cells.get("evidence_reference"),
            provider_evidence_hash=cells.get("evidence_hash"),
            provider_readback_evidence=dict(readback_evidence) if readback_evidence else {},
        )
