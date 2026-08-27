"""Durable provider-neutral outbound action orchestration."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from hashlib import sha256
from typing import Any
from typing import Mapping
from typing import Protocol
from uuid import UUID

from .adapters.base import ProviderAdapter
from .adapters.base import ProviderDisposition
from .adapters.base import ProviderObservation
from .adapters.base import ProviderReceipt
from .context import ActionContext
from .context import ActionContextLoader
from .context import ContextDerivationError
from .context import SuggestionData
from .context import canonical_payload_hash
from .metrics import CircuitStatus
from .metrics import bounded_backoff_seconds
from .models import ActionRole
from .models import ActionState
from .models import CompletionKind
from .models import ExecuteRequest
from .models import IntentKind
from .models import Operation
from .models import PublicResult
from .preflight import PreflightDecision
from .preflight import PreflightEvidence
from .preflight import PreflightOutcome
from .preflight import SafetyPreflight
from .state_machine import public_result
from .tenantcloud_shared import EVIDENCE_KIND_VERIFIED_READBACK
from .tenantcloud_shared import READBACK_OBSERVATION_KEYS
from .tenantcloud_shared import TENANTCLOUD_OPERATIONS
from .tenantcloud_shared import strip_tenantcloud_persisted_argument_keys

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class OutboundActionRecord:
    action_id: UUID
    wakeup_event_id: int
    action_role: ActionRole
    operation: Operation
    intent_kind: IntentKind
    appointment_slot: datetime | None
    arguments: dict[str, Any]
    state: ActionState
    action_uid: UUID | None
    provider_request_ref: str | None
    provider_message_id: str | None
    provider_accepted_at: datetime | None
    completion_kind: CompletionKind | None
    detail_code: str
    attempt_count: int
    next_attempt_at: datetime
    payload_hash: str
    canonical_context: Mapping[str, Any]
    canonical_scope: Mapping[str, Any]
    recipient_scope: Mapping[str, Any]
    provider_account: str
    routing_policy_version: str
    provider_evidence_kind: str | None = None
    provider_evidence_reference: str | None = None
    provider_evidence_hash: str | None = None
    provider_readback_evidence: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def execute_request(self) -> ExecuteRequest:
        # create_or_load() persists TenantCloud arguments enriched with
        # desired_state/target_reference/idempotency_key (migration 118
        # reads those directly off outbound_actions.arguments). Every
        # ArgumentModel is a StrictModel with extra="forbid", so those
        # gateway-owned keys must be stripped back out before they reach
        # model_validate() here -- this method rebuilds context on every
        # reconcile()/resume() call, including the crash-recovery path.
        arguments = strip_tenantcloud_persisted_argument_keys(self.operation, self.arguments)
        return ExecuteRequest.model_validate(
            {
                "op": "execute",
                "wakeup_event_id": self.wakeup_event_id,
                "action_role": self.action_role,
                "operation": self.operation,
                "intent_kind": self.intent_kind,
                "appointment_slot": self.appointment_slot,
                "arguments": arguments,
            }
        )


class ActionStore(Protocol):
    async def create_or_load(self, context: ActionContext) -> OutboundActionRecord: ...

    async def prepare(
        self,
        context: ActionContext,
        expected_state: ActionState,
    ) -> OutboundActionRecord: ...

    async def claim(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str,
        lease_seconds: int,
    ) -> OutboundActionRecord: ...

    async def record_provider_request(
        self,
        action_id: UUID,
        lease_owner: str,
        observation: ProviderObservation,
    ) -> OutboundActionRecord: ...

    async def transition(
        self,
        action_id: UUID,
        expected_state: ActionState,
        next_state: ActionState,
        lease_owner: str | None,
        observation: ProviderObservation,
    ) -> OutboundActionRecord: ...

    async def complete(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str | None,
        receipt: ProviderReceipt,
        completion_kind: CompletionKind,
        detail_code: str,
    ) -> OutboundActionRecord: ...

    async def definitive_fail(
        self,
        action_id: UUID,
        expected_state: ActionState,
        lease_owner: str,
        observation: ProviderObservation,
    ) -> OutboundActionRecord: ...

    async def get(self, action_id: UUID) -> OutboundActionRecord | None: ...

    async def schedule_next_attempt(
        self,
        action_id: UUID,
        expected_state: ActionState,
        delay_seconds: int,
        detail_code: str,
    ) -> OutboundActionRecord: ...


class PreflightEvidenceLoader(Protocol):
    async def load(self, context: ActionContext) -> PreflightEvidence: ...


class CircuitGuard(Protocol):
    async def circuit_status(self, operation: Operation) -> CircuitStatus: ...


class ClosedCircuitGuard:
    async def circuit_status(self, operation: Operation) -> CircuitStatus:
        del operation
        return CircuitStatus(is_open=False, retry_after_seconds=0, failure_count=0)


Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class OutboundActionService:
    """State machine coordinator. Contains no provider-specific branches."""

    def __init__(
        self,
        *,
        store: ActionStore,
        context_loader: ActionContextLoader,
        evidence_loader: PreflightEvidenceLoader,
        adapters: Mapping[Operation, ProviderAdapter],
        provider_client: Any,
        clock: Clock,
        lease_owner: str,
        response_budget_seconds: float = 25,
        lease_seconds: int = 60,
        sleep: Sleeper = asyncio.sleep,
        circuit_guard: CircuitGuard | None = None,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 900,
    ):
        self._store = store
        self._context_loader = context_loader
        self._evidence_loader = evidence_loader
        self._adapters = dict(adapters)
        self._provider_client = provider_client
        self._clock = clock
        self._lease_owner = lease_owner
        self._response_budget_seconds = max(0, min(response_budget_seconds, 29))
        self._lease_seconds = lease_seconds
        self._sleep = sleep
        self._circuit_guard = circuit_guard or ClosedCircuitGuard()
        self._retry_base_seconds = max(1, retry_base_seconds)
        self._retry_max_seconds = max(self._retry_base_seconds, retry_max_seconds)

    async def execute(self, request: ExecuteRequest) -> PublicResult:
        context = await self._context_loader.load(request)
        action = None
        if context.prospect_id.startswith("subject:"):
            existing = await self._store.get(context.action_id)
            if existing is not None and self._matches_durable_subject_alias_promotion(
                existing,
                context,
            ):
                action = existing
        if action is None:
            action = await self._store.create_or_load(context)
        if action.state is ActionState.COMPLETED:
            return self._result(action, repeated=True)
        if not self._is_due(action):
            return self._result(action)
        if action.state in {
            ActionState.STALE,
            ActionState.REJECTED,
            ActionState.DEFINITIVE_FAILED,
            ActionState.DEAD_LETTER,
            ActionState.MANUAL_REVIEW,
            ActionState.UNKNOWN,
            ActionState.RECONCILING,
            ActionState.DISPATCHING,
            ActionState.PROVIDER_ACCEPTED,
        }:
            return self._result(action)
        if action.state is ActionState.DEPENDENCY_WAIT:
            return await self._resume_dependency(action, context)
        if action.state in {ActionState.PREPARED, ActionState.RETRY_READY}:
            return await self._dispatch(action, context)
        return await self._preflight(action, context)

    async def status(self, action_id: UUID) -> PublicResult:
        return self._result(await self._require_action(action_id))

    async def suggest_targets(self, wakeup_event_id: int) -> dict[str, str]:
        return await self._context_loader.suggest_targets(wakeup_event_id)

    async def suggest(self, wakeup_event_id: int) -> SuggestionData:
        return await self._context_loader.suggest(wakeup_event_id)

    async def resume(self, action_id: UUID) -> PublicResult:
        action = await self._require_action(action_id)
        if not self._is_due(action):
            return self._result(action)
        context, context_detail = await self._verified_context(action)
        if context is None:
            return await self._manual_review(action, context_detail)
        if action.state is ActionState.DEPENDENCY_WAIT:
            return await self._resume_dependency(action, context)
        if action.state in {ActionState.PREPARED, ActionState.RETRY_READY}:
            return await self._dispatch(action, context)
        return self._result(action)

    async def reconcile(self, action_id: UUID) -> PublicResult:
        action = await self._require_action(action_id)
        if not self._is_due(action):
            return self._result(action)
        recovered = await self._recover_persisted_acceptance(action)
        if recovered is not None:
            return recovered
        if action.state in {
            ActionState.DISPATCHING,
            ActionState.PROVIDER_ACCEPTED,
            ActionState.RECONCILING,
        }:
            await self._store.claim(action.action_id, action.state, self._lease_owner, self._lease_seconds)
            action = await self._store.transition(
                action.action_id,
                action.state,
                ActionState.UNKNOWN,
                self._lease_owner,
                ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    "expired_dispatch_requires_reconciliation",
                    provider_request_ref=action.provider_request_ref,
                ),
            )
        if action.state is not ActionState.UNKNOWN:
            return self._result(action)
        context, context_detail = await self._verified_context(action)
        if context is None:
            return await self._manual_review(action, context_detail)
        adapter = self._adapter(context.operation)
        await self._store.claim(action.action_id, action.state, self._lease_owner, self._lease_seconds)
        reconciling = await self._store.transition(
            action.action_id,
            ActionState.UNKNOWN,
            ActionState.RECONCILING,
            self._lease_owner,
            ProviderObservation(
                ProviderDisposition.AMBIGUOUS,
                "reconciliation_started",
                provider_request_ref=action.provider_request_ref,
            ),
        )
        if reconciling.action_uid is None:
            raise RuntimeError("reconciling action has no deterministic action UID")
        observation = await adapter.reconcile(
            self._provider_client,
            context,
            reconciling.action_uid,
            ProviderObservation(
                ProviderDisposition.AMBIGUOUS,
                "prior_dispatch_ambiguous",
                provider_request_ref=reconciling.provider_request_ref,
            ),
        )
        return await self._finish_observation(reconciling, context, adapter, observation)

    async def exhaust(self, action_id: UUID) -> PublicResult:
        """Close exhausted work without another provider invocation."""
        action = await self._require_action(action_id)
        recovered = await self._recover_persisted_acceptance(action)
        if recovered is not None:
            return recovered
        lease_held = False
        observation = ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "retry_budget_exhausted",
            provider_request_ref=action.provider_request_ref,
        )
        if action.state in {ActionState.DISPATCHING, ActionState.PROVIDER_ACCEPTED}:
            await self._store.claim(
                action.action_id,
                action.state,
                self._lease_owner,
                self._lease_seconds,
            )
            lease_held = True
            action = await self._store.transition(
                action.action_id,
                action.state,
                ActionState.UNKNOWN,
                self._lease_owner,
                observation,
            )
            lease_held = False
        if action.state is ActionState.UNKNOWN:
            await self._store.claim(
                action.action_id,
                action.state,
                self._lease_owner,
                self._lease_seconds,
            )
            lease_held = True
            action = await self._store.transition(
                action.action_id,
                ActionState.UNKNOWN,
                ActionState.RECONCILING,
                self._lease_owner,
                ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    "retry_budget_exhausted_reconciliation",
                    provider_request_ref=action.provider_request_ref,
                ),
            )
        if action.state in {ActionState.RECONCILING, ActionState.DEPENDENCY_WAIT}:
            if not lease_held:
                await self._store.claim(
                    action.action_id,
                    action.state,
                    self._lease_owner,
                    self._lease_seconds,
                )
            action = await self._store.transition(
                action.action_id,
                action.state,
                ActionState.DEAD_LETTER,
                self._lease_owner,
                observation,
            )
            action = await self._store.transition(
                action.action_id,
                ActionState.DEAD_LETTER,
                ActionState.MANUAL_REVIEW,
                None,
                ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    "retry_budget_exhausted_manual_review",
                    provider_request_ref=action.provider_request_ref,
                ),
            )
            return self._result(action)
        if action.state in {ActionState.PREPARED, ActionState.RETRY_READY}:
            await self._store.claim(
                action.action_id,
                action.state,
                self._lease_owner,
                self._lease_seconds,
            )
            failed = await self._store.definitive_fail(
                action.action_id,
                action.state,
                self._lease_owner,
                ProviderObservation(
                    ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
                    "retry_budget_exhausted",
                    provider_request_ref=action.provider_request_ref,
                    category="retry_budget_exhausted",
                    retryable=False,
                    evidence={"kind": "retry_budget"},
                ),
            )
            return self._result(failed)
        return self._result(action)

    async def _recover_persisted_acceptance(
        self,
        action: OutboundActionRecord,
    ) -> PublicResult | None:
        """Complete a durable provider acceptance without provider I/O."""
        if not (
            action.state is ActionState.PROVIDER_ACCEPTED
            and action.provider_request_ref
            and action.provider_message_id
            and action.provider_accepted_at
        ):
            return None
        if action.operation in TENANTCLOUD_OPERATIONS and not self._verified_tenantcloud_evidence(action):
            # TenantCloud writes are irreversible provider-side actions
            # (lead status, maintenance requests). The generic ref/id/accepted_at
            # heuristic above is not proof enough here: only durable evidence
            # that says "verified readback" AND whose hash matches the
            # persisted canonical state may complete without provider I/O.
            # Anything else -- including a crash between the PROVIDER_ACCEPTED
            # transition and the evidence write -- must go through bounded
            # reconciliation instead of being trusted blindly.
            return None
        provider_request_ref = action.provider_request_ref
        provider_message_id = action.provider_message_id
        provider_accepted_at = action.provider_accepted_at
        action = await self._store.claim(
            action.action_id,
            action.state,
            self._lease_owner,
            self._lease_seconds,
        )
        completed = await self._store.complete(
            action.action_id,
            ActionState.PROVIDER_ACCEPTED,
            self._lease_owner,
            ProviderReceipt(
                provider_request_ref=provider_request_ref,
                provider_message_id=provider_message_id,
                accepted_at=provider_accepted_at,
                evidence={"kind": "persisted_provider_acceptance"},
            ),
            CompletionKind.SENT,
            "persisted_provider_acceptance_recovered",
        )
        return self._result(completed)

    async def _preflight(self, action: OutboundActionRecord, context: ActionContext) -> PublicResult:
        evidence = await self._evidence_loader.load(context)
        decision = SafetyPreflight.evaluate(context, evidence, now=self._clock())
        if decision.outcome is PreflightOutcome.READY:
            prepared = await self._store.prepare(context, action.state)
            if prepared.state is ActionState.COMPLETED:
                return self._result(prepared, repeated=True)
            if prepared.state is ActionState.DEPENDENCY_WAIT:
                return self._result(prepared)
            return await self._dispatch(prepared, context)
        return await self._apply_preflight_decision(action, evidence, decision)

    async def _resume_dependency(self, action: OutboundActionRecord, context: ActionContext) -> PublicResult:
        action = await self._store.claim(
            action.action_id,
            action.state,
            self._lease_owner,
            self._lease_seconds,
        )
        evidence = await self._evidence_loader.load(context)
        decision = SafetyPreflight.evaluate(context, evidence, now=self._clock())
        if decision.outcome is PreflightOutcome.READY:
            prepared = await self._store.prepare(context, action.state)
            if prepared.state is ActionState.COMPLETED:
                return self._result(prepared, repeated=True)
            if prepared.state is ActionState.DEPENDENCY_WAIT:
                return self._result(prepared)
            return await self._dispatch(prepared, context)
        if decision.outcome is PreflightOutcome.DEPENDENCY_WAIT:
            scheduled = await self._schedule(action, decision.detail_code)
            return self._result(scheduled)
        return await self._apply_preflight_decision(
            action,
            evidence,
            decision,
            lease_owner=self._lease_owner,
        )

    async def _apply_preflight_decision(
        self,
        action: OutboundActionRecord,
        evidence: PreflightEvidence,
        decision: PreflightDecision,
        *,
        lease_owner: str | None = None,
    ) -> PublicResult:
        if decision.outcome is PreflightOutcome.DUPLICATE:
            assert evidence.verified_outbound_request_ref is not None
            assert evidence.verified_outbound_message_id is not None
            receipt = ProviderReceipt(
                provider_request_ref=evidence.verified_outbound_request_ref,
                provider_message_id=evidence.verified_outbound_request_ref,
                accepted_at=self._clock(),
                evidence={
                    "kind": "verified_existing_outbound",
                    "cds_message_id": evidence.verified_outbound_message_id,
                },
            )
            completed = await self._store.complete(
                action.action_id,
                action.state,
                lease_owner,
                receipt,
                CompletionKind.DUPLICATE,
                decision.detail_code,
            )
            return self._result(completed, repeated=True)
        if decision.outcome in {PreflightOutcome.STALE, PreflightOutcome.REJECTED}:
            target = ActionState.STALE if decision.outcome is PreflightOutcome.STALE else ActionState.REJECTED
            transitioned = await self._store.transition(
                action.action_id,
                action.state,
                target,
                lease_owner,
                ProviderObservation(ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE, decision.detail_code),
            )
            return self._result(transitioned)
        if decision.outcome is PreflightOutcome.MANUAL_REVIEW and action.state is ActionState.DEPENDENCY_WAIT:
            transitioned = await self._store.transition(
                action.action_id,
                action.state,
                ActionState.DEAD_LETTER,
                lease_owner,
                ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    decision.detail_code,
                ),
            )
            return self._result(transitioned)
        dependency_code = decision.detail_code
        transitioned = await self._store.transition(
            action.action_id,
            action.state,
            ActionState.DEPENDENCY_WAIT,
            lease_owner,
            ProviderObservation(ProviderDisposition.PENDING, dependency_code),
        )
        return self._result(await self._schedule(transitioned, dependency_code))

    async def _dispatch(self, action: OutboundActionRecord, context: ActionContext) -> PublicResult:
        adapter = self._adapter(context.operation)
        circuit = await self._circuit_guard.circuit_status(context.operation)
        if circuit.is_open:
            scheduled = await self._store.schedule_next_attempt(
                action.action_id,
                action.state,
                max(1, circuit.retry_after_seconds),
                "provider_circuit_open",
            )
            return self._result(scheduled)
        claimed = await self._store.claim(action.action_id, action.state, self._lease_owner, self._lease_seconds)
        dispatching = await self._store.transition(
            claimed.action_id,
            claimed.state,
            ActionState.DISPATCHING,
            self._lease_owner,
            ProviderObservation(ProviderDisposition.PENDING, "dispatch_started"),
        )
        if dispatching.action_uid is None:
            raise RuntimeError("prepared action has no deterministic action UID")
        provider_request = adapter.build_request(context, dispatching.action_uid)
        observation = await adapter.invoke(self._provider_client, provider_request)
        if observation.provider_request_ref:
            dispatching = await self._store.record_provider_request(
                dispatching.action_id,
                self._lease_owner,
                observation,
            )
        if observation.disposition is ProviderDisposition.PENDING:
            observation = await adapter.poll(self._provider_client, observation)
            if observation.provider_request_ref and observation.provider_request_ref != dispatching.provider_request_ref:
                dispatching = await self._store.record_provider_request(
                    dispatching.action_id,
                    self._lease_owner,
                    observation,
                )
            if observation.disposition is ProviderDisposition.PENDING:
                observation = ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    "provider_queue_timeout",
                    provider_request_ref=observation.provider_request_ref,
                    provider_call_id=observation.provider_call_id,
                )
        return await self._finish_observation(dispatching, context, adapter, observation)

    async def _finish_observation(
        self,
        action: OutboundActionRecord,
        context: ActionContext,
        adapter: ProviderAdapter,
        observation: ProviderObservation,
    ) -> PublicResult:
        expected_state = action.state
        if observation.disposition is ProviderDisposition.ACCEPTED:
            receipt = adapter.parse_receipt(context, observation)
            if receipt is None:
                observation = ProviderObservation(
                    ProviderDisposition.AMBIGUOUS,
                    "provider_receipt_missing",
                    provider_request_ref=observation.provider_request_ref,
                )
            else:
                if expected_state is ActionState.DISPATCHING:
                    accepted = await self._store.transition(
                        action.action_id,
                        expected_state,
                        ActionState.PROVIDER_ACCEPTED,
                        self._lease_owner,
                        observation,
                    )
                else:
                    accepted = action
                completed = await self._store.complete(
                    accepted.action_id,
                    accepted.state,
                    self._lease_owner,
                    receipt,
                    CompletionKind.SENT,
                    "provider_receipt_verified",
                )
                return self._result(completed)
        if observation.disposition is ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE:
            if observation.retryable:
                retry = await self._store.transition(
                    action.action_id,
                    expected_state,
                    ActionState.RETRY_READY,
                    self._lease_owner,
                    observation,
                )
                return self._result(await self._schedule(retry, observation.detail_code))
            failed = await self._store.definitive_fail(
                action.action_id,
                expected_state,
                self._lease_owner,
                observation,
            )
            return self._result(failed)
        unknown = await self._store.transition(
            action.action_id,
            expected_state,
            ActionState.UNKNOWN,
            self._lease_owner,
            observation,
        )
        return self._result(await self._schedule(unknown, observation.detail_code))

    async def _schedule(
        self,
        action: OutboundActionRecord,
        detail_code: str,
    ) -> OutboundActionRecord:
        return await self._store.schedule_next_attempt(
            action.action_id,
            action.state,
            bounded_backoff_seconds(
                action.attempt_count,
                base_seconds=self._retry_base_seconds,
                max_seconds=self._retry_max_seconds,
            ),
            detail_code,
        )

    def _adapter(self, operation: Operation) -> ProviderAdapter:
        adapter = self._adapters.get(operation)
        if adapter is None:
            raise ValueError(f"no outbound provider adapter configured for {operation.value}")
        return adapter

    async def _require_action(self, action_id: UUID) -> OutboundActionRecord:
        action = await self._store.get(action_id)
        if action is None:
            raise LookupError("outbound action does not exist")
        return action

    async def _verified_context(
        self,
        action: OutboundActionRecord,
    ) -> tuple[ActionContext | None, str]:
        try:
            context = await self._context_loader.load(action.execute_request())
        except ContextDerivationError:
            return None, "persisted_context_unavailable"
        if not action.payload_hash:
            return context, "context_verified"
        expected_recipient = {
            "kind": context.target.kind,
            "target_id": context.target.target_id,
            "verified": context.target.verified,
        }
        if (
            context.action_id != action.action_id
            or context.provider_account != action.provider_account
            or context.routing_policy_version != action.routing_policy_version
            or expected_recipient != dict(action.recipient_scope)
        ):
            return None, "persisted_context_mismatch"
        if (
            context.payload_hash == action.payload_hash
            and dict(context.canonical_context) == dict(action.canonical_context)
            and dict(context.canonical_scope) == dict(action.canonical_scope)
        ):
            return context, "context_verified"
        if self._matches_durable_subject_alias_promotion(action, context):
            return context, "context_verified_alias_promotion"
        return None, "persisted_context_mismatch"

    @staticmethod
    def _matches_durable_subject_alias_promotion(
        action: OutboundActionRecord,
        context: ActionContext,
    ) -> bool:
        expected_recipient = {
            "kind": context.target.kind,
            "target_id": context.target.target_id,
            "verified": context.target.verified,
        }
        if (
            context.action_id != action.action_id
            or context.provider_account != action.provider_account
            or context.routing_policy_version != action.routing_policy_version
            or expected_recipient != dict(action.recipient_scope)
        ):
            return False
        stored_context = dict(action.canonical_context)
        current_context = dict(context.canonical_context)
        stored_prospect = stored_context.get("prospect_id")
        current_prospect = current_context.get("prospect_id")
        if not (
            isinstance(stored_prospect, str)
            and stored_prospect.startswith("prospect:")
            and isinstance(current_prospect, str)
            and current_prospect.startswith("subject:")
        ):
            return False
        normalized_context = {**current_context, "prospect_id": stored_prospect}
        if normalized_context != stored_context:
            return False
        stored_scope = dict(action.canonical_scope)
        current_scope = dict(context.canonical_scope)
        if "prospect_id" in current_scope:
            current_scope["prospect_id"] = stored_prospect
        if current_scope != stored_scope:
            return False
        normalized_hash = canonical_payload_hash(
            {
                "action_role": context.action_role.value,
                "operation": context.operation.value,
                "intent_kind": context.intent_kind.value,
                "appointment_slot": context.appointment_slot,
                "arguments": context.arguments,
                "canonical_context": normalized_context,
            }
        )
        return normalized_hash == action.payload_hash

    async def _manual_review(
        self,
        action: OutboundActionRecord,
        detail_code: str,
    ) -> PublicResult:
        claimed = await self._store.claim(
            action.action_id,
            action.state,
            self._lease_owner,
            self._lease_seconds,
        )
        dead_letter = await self._store.transition(
            claimed.action_id,
            claimed.state,
            ActionState.DEAD_LETTER,
            self._lease_owner,
            ProviderObservation(
                ProviderDisposition.AMBIGUOUS,
                detail_code,
                provider_request_ref=claimed.provider_request_ref,
            ),
        )
        manual = await self._store.transition(
            dead_letter.action_id,
            ActionState.DEAD_LETTER,
            ActionState.MANUAL_REVIEW,
            None,
            ProviderObservation(
                ProviderDisposition.AMBIGUOUS,
                detail_code,
                provider_request_ref=dead_letter.provider_request_ref,
            ),
        )
        return self._result(manual)

    def _is_due(self, action: OutboundActionRecord) -> bool:
        return action.next_attempt_at <= self._clock()

    @staticmethod
    def evidence_hash(evidence: Mapping[str, Any] | None) -> str:
        return sha256(json.dumps(evidence or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _verified_tenantcloud_evidence(action: OutboundActionRecord) -> bool:
        """Migration 118's transition_outbound_action already enforced the
        full acceptance guard (evidence_kind literal, six-key observation
        shape, per-key type/format checks, and equality against the
        persisted arguments' desired_state/target_reference/operation)
        atomically, in the same statement that wrote evidence_kind =
        'verified_provider_readback'. So a row bearing that literal is only
        reachable through that guarded write. This re-checks the literal,
        the evidence_hash's own format, and structural completeness of the
        persisted six-key observation -- defense against a corrupted or
        partial read, not a re-derivation of the facade's own opaque hash
        (which, for maintenance create, is computed over a target_reference
        that differs by design from what is persisted here -- see
        tenantcloud_shared.py)."""
        if action.provider_evidence_kind != EVIDENCE_KIND_VERIFIED_READBACK:
            return False
        if not action.provider_evidence_hash or not _HEX64.fullmatch(action.provider_evidence_hash):
            return False
        evidence = dict(action.provider_readback_evidence)
        if set(evidence) != READBACK_OBSERVATION_KEYS:
            return False
        if not isinstance(evidence.get("canonical_observed_state"), Mapping):
            return False
        if evidence.get("readback_verified") is not True:
            return False
        for key in ("operation", "provider_object_id", "target_reference", "readback_timestamp"):
            if not isinstance(evidence.get(key), str) or not evidence[key]:
                return False
        return True

    @staticmethod
    def _result(action: OutboundActionRecord, *, repeated: bool = False) -> PublicResult:
        return public_result(
            state=action.state,
            action_id=action.action_id,
            action_uid=action.action_uid,
            provider_request_ref=action.provider_request_ref,
            detail_code=action.detail_code,
            completion_kind=action.completion_kind,
            repeated_execute=repeated,
        )
