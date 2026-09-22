"""Durable provider-neutral outbound action orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace as dataclass_replace
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
from .context import canonical_payload_hash
from .metrics import CircuitStatus
from .metrics import bounded_backoff_seconds
from .models import ActionRole
from .models import ActionState
from .models import CompletionKind
from .models import ExecuteRequest
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
from .traffic_control import VALID_TRAFFIC_MODES
from .traffic_control import TrafficProbe
from .traffic_control import check_traffic

logger = logging.getLogger(__name__)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class OutboundActionRecord:
    action_id: UUID
    wakeup_event_id: int
    action_role: ActionRole
    operation: Operation
    intent_kind: str
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
    error_category: str | None = None
    retry_of_action_id: UUID | None = None

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

    async def remediate_traffic_block(
        self,
        action_id: UUID,
        *,
        operator_identity: str,
        reason: str,
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
        traffic_mode: str = "shadow",
        traffic_probe: TrafficProbe | None = None,
    ):
        if traffic_mode not in VALID_TRAFFIC_MODES:
            raise ValueError(f"traffic_mode must be one of {sorted(VALID_TRAFFIC_MODES)}, got {traffic_mode!r}")
        if traffic_mode != "off" and traffic_probe is None:
            # Not a hard failure -- off/shadow/enforce is a legitimate
            # operational rollout switch and a missing probe must not crash
            # the gateway -- but silently behaving like "off" is exactly the
            # kind of wiring bug (env var set, probe forgotten in
            # build_runtime()) that should be loud, not invisible.
            logger.warning(
                "traffic_mode=%r configured with no traffic_probe -- the gate will never run and this will silently behave like traffic_mode='off'",
                traffic_mode,
            )
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
        self._traffic_mode = traffic_mode
        self._traffic_probe = traffic_probe

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
        if action.state is ActionState.DEFINITIVE_FAILED and action.error_category == "traffic_blocked" and request.override:
            remediated = await self._remediate_traffic_block(action, context)
            if remediated is not None:
                action, context = remediated
            else:
                return self._result(
                    action,
                    detail=(
                        "override cannot resend this action yet: it is blocked by traffic "
                        "control and has no evidence-resolved operator remediation on file. "
                        "Escalate for manual review before retrying."
                    ),
                )
        elif action.state in {
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
        blocked = await self._check_traffic(action, context, override=request.override)
        if blocked is not None:
            return blocked

        async def _preflight_fallback() -> PublicResult:
            return await self._preflight(action, context)

        return await self._dispatch_stage(action, context, otherwise=_preflight_fallback)

    async def enqueue(self, request: ExecuteRequest) -> PublicResult:
        """Persist and preflight one action without provider I/O.

        Restate owns every later advance. Repeated calls return the same CDS
        action, so ingress loss never loses work and never creates a new send.
        """
        context = await self._context_loader.load(request)
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
            ActionState.PREPARED,
            ActionState.RETRY_READY,
            ActionState.DEPENDENCY_WAIT,
        }:
            return self._result(action)
        blocked = await self._check_traffic(action, context, override=request.override)
        if blocked is not None:
            return blocked
        evidence = await self._evidence_loader.load(context)
        decision = SafetyPreflight.evaluate(context, evidence, now=self._clock())
        if decision.outcome is PreflightOutcome.READY:
            prepared = await self._store.prepare(context, action.state)
            return self._result(prepared, repeated=prepared.state is ActionState.COMPLETED)
        return await self._apply_preflight_decision(action, evidence, decision)

    async def prepare(self, action_id: UUID) -> PublicResult:
        """Preflight a persisted remediation successor without provider I/O."""
        action = await self._require_action(action_id)
        if action.state is not ActionState.RECEIVED or not self._is_due(action):
            return self._result(action)
        context, context_detail = await self._verified_context(action)
        if context is None:
            return self._result(action, detail=context_detail)
        blocked = await self._check_traffic(action, context, override=False)
        if blocked is not None:
            return blocked
        evidence = await self._evidence_loader.load(context)
        decision = SafetyPreflight.evaluate(context, evidence, now=self._clock())
        if decision.outcome is PreflightOutcome.READY:
            prepared = await self._store.prepare(context, action.state)
            return self._result(prepared, repeated=prepared.state is ActionState.COMPLETED)
        return await self._apply_preflight_decision(action, evidence, decision)

    async def _dispatch_stage(
        self,
        action: OutboundActionRecord,
        context: ActionContext,
        *,
        otherwise: Callable[[], Awaitable[PublicResult]],
    ) -> PublicResult:
        """Routes to _resume_dependency/_dispatch for the states both
        execute() and resume() share, falling back to `otherwise()` for
        anything else -- execute()'s catch-all is _preflight() (a fresh
        RECEIVED action); resume()'s is just returning the row's current
        result unchanged (worker.py only ever calls resume() for
        DEPENDENCY_WAIT/PREPARED/RETRY_READY, but resume() is a public
        method with no such guarantee from other callers, so its historical
        "return the row as-is" fallback for any other state must not
        silently become _preflight()). All three routes -- including
        `otherwise` -- can reach _dispatch()'s adapter.invoke()/
        adapter.poll() provider I/O, so all three are covered by the same
        try/except below.

        A post-dispatch exception (e.g. a network timeout *after* the
        provider already accepted the HTTP request -- the row is durably
        DISPATCHING with a lease by the time adapter.invoke() runs, since
        claim()+transition() to DISPATCHING happen before it) must never
        escape to the MCP caller as a raised error: FastMCP wraps any
        uncaught exception as "Error executing tool outbound_action: ...",
        and the CDS reconciler's rejection-prefix rule treats that wrapper
        as proof nothing was sent. If a real send's post-accept timeout
        propagated that far, the reconciler would uncount a REAL send and
        let the wake complete while the message was actually delivered --
        exactly the false negative the prefix rule is only safe without.

        So: catch broadly here, log at ERROR (wake + action id, for
        operator visibility), and return whatever the row's durable state
        already is -- DISPATCHING/RECONCILING with a lease, recovered by the
        existing lease-expiry/reconcile/worker machinery, same as any other
        expired-lease crash recovery. This restores the invariant that an
        MCP error wrapper strictly implies "rejected before any provider
        interaction": context load/validation (in execute(), everything
        before this call) is deliberately NOT covered by this except clause
        and still raises, so a true pre-dispatch rejection keeps the error
        wrapper the reconciler depends on.
        """
        try:
            if action.state is ActionState.DEPENDENCY_WAIT:
                return await self._resume_dependency(action, context)
            if action.state in {ActionState.PREPARED, ActionState.RETRY_READY}:
                return await self._dispatch(action, context)
            return await otherwise()
        except Exception:
            logger.error(
                "post-dispatch exception on wake %s action %s -- provider call outcome "
                "unknown, returning durable row state for lease-expiry/reconcile recovery",
                context.wakeup_event_id,
                action.action_id,
                exc_info=True,
            )
            return self._result(await self._require_action(action.action_id))

    async def _remediate_traffic_block(
        self,
        action: OutboundActionRecord,
        context: ActionContext,
    ) -> tuple[OutboundActionRecord, ActionContext] | None:
        """override=true resend of a traffic-blocked DEFINITIVE_FAILED action.

        Reuses the same successor-action machinery operator remediation uses
        (create_outbound_remediation_context / retry_of_action_id / next
        effect_ordinal -- Comm-Data-Store migrations/067_outbound_action_gateway.sql:1200-1267,
        granted to the gateway's runtime role in migrations/120_outbound_action_terminal_wake_boundary.sql:550-551)
        rather than inventing a new one. That function's own precondition --
        an evidence-resolved `outbound_action_resolutions` row for this
        action (067:1221-1228) -- is written by resolve_outbound_action_from_evidence,
        which IS granted to the gateway's runtime role (migrations/
        079_runtime_tenantcloud_privilege_boundary.sql:427,459-475), so this
        is not a privilege wall. The real blocker is a lifecycle/evidence
        mismatch: resolve_outbound_action_from_evidence only accepts a row
        already in 'manual_review' (067:1160-1163) and requires real
        provider-side non-acceptance evidence (a 64-hex hash, a non-empty
        reference, evidence_kind='authoritative_non_acceptance', 067:1164-1173)
        -- neither of which a traffic-control block has: it goes straight to
        'definitive_failed' from a live/pending state, never through
        'manual_review', and there is no provider disposition to attest to,
        only an internal recipient-safety policy decision. So an override
        resend can only succeed once an operator has independently routed
        this action through manual_review and evidence-resolved it; until
        then this returns None and the caller stays on the original terminal
        result instead of crashing on the unhandled precondition-violation
        exception. Closing that gap for a fully autonomous, zero-operator
        unblock needs a new Comm-Data-Store migration (e.g. a successor path
        keyed on error_category='traffic_blocked' instead of an evidence
        resolution) -- out of scope for this worktree.

        The successor's context is the SAME `context` already loaded for
        this call (not re-derived via `_verified_context`): the successor
        copies wakeup_event_id/action_role/canonical_scope/canonical_context/
        recipient_scope/provider_account/routing_policy_version/operation/
        intent_kind/appointment_slot/arguments/payload_hash verbatim from the
        parent row (067:1247-1264), so nothing about the wake-derived context
        actually changed -- only `action_id` (next effect_ordinal) did.
        `_verified_context()` would be the wrong tool here: it re-derives
        action_id from the wake via the client-side uid formula, which is
        always ordinal 0 (context.py:299), so it would report every
        successor as a context mismatch.
        """
        try:
            successor = await self._store.remediate_traffic_block(
                action.action_id,
                operator_identity=self._lease_owner,
                reason="traffic_control_override_resend",
            )
        except Exception:
            logger.warning(
                "traffic control override could not remediate blocked action %s (no evidence-resolved remediation on file yet)",
                action.action_id,
                exc_info=True,
            )
            return None
        return successor, dataclass_replace(context, action_id=successor.action_id)

    async def _check_traffic(
        self,
        action: OutboundActionRecord,
        context: ActionContext,
        *,
        override: bool,
    ) -> PublicResult | None:
        """Per-recipient traffic gate. Returns a blocking PublicResult when
        enforce mode must stop dispatch; returns None (proceed) otherwise --
        including shadow mode (which only logs) and off mode (no probe call
        at all)."""
        if self._traffic_mode == "off" or self._traffic_probe is None:
            return None
        verdict = await check_traffic(
            self._traffic_probe,
            recipient_key=context.prospect_id,
            channel_id=context.channel_id,
            wakeup_event_id=context.wakeup_event_id,
            action_id=context.action_id,
            override=override,
            logger=logger,
        )
        if not verdict.allowed:
            if self._traffic_mode == "enforce":
                claimable = action
                if action.state is ActionState.RECEIVED:
                    # claim_outbound_action's live whitelist (Comm-Data-Store
                    # migrations/068_outbound_gateway_observability.sql:156-159) excludes
                    # 'received' -- a fresh row must first move through
                    # prepare_outbound_action_and_acquire_lock (067:524-528 only accepts
                    # 'received'/'dependency_wait'), the same call the normal
                    # _preflight() READY path uses, before it is claimable at all.
                    claimable = await self._store.prepare(context, action.state)
                    if claimable.state is ActionState.COMPLETED:
                        return self._result(claimable, repeated=True)
                if claimable.state is ActionState.DEPENDENCY_WAIT or verdict.reason == "lease_held":
                    # Two independent reasons land here, both deferring
                    # instead of terminalizing:
                    #
                    # 1. claimable.state is DEPENDENCY_WAIT: DB-forced, not a
                    #    policy choice. outbound_action_transition_allowed()
                    #    has no dependency_wait -> definitive_failed edge
                    #    (Comm-Data-Store migrations/067_outbound_action_gateway.sql:346-389)
                    #    -- forcing a terminal here would raise the DB's
                    #    'invalid outbound definitive failure state'
                    #    uncaught. Two ways to land here: a fresh RECEIVED row
                    #    whose prepare() above hit a contended intent lock
                    #    (067:556-564), or resume() being called on an
                    #    already-dependency_wait row. Same pattern
                    #    _preflight()'s READY branch already uses for lock
                    #    contention (~line 643).
                    #
                    # 2. verdict.reason == "lease_held": a policy choice
                    #    (Important 6), true regardless of claimable.state. A
                    #    lease block is inherently short-lived -- the other
                    #    in-flight action will reach a terminal state on its
                    #    own -- unlike a stale-context block, which needs a
                    #    conscious agent decision (skip or override) because
                    #    nothing else is going to resolve it. Deterministic
                    #    action_id + a terminal DEFINITIVE_FAILED meant a
                    #    seconds-long lease overlap would brick that resend
                    #    forever (override never bypasses a lease, by
                    #    design -- only staleness).
                    #
                    # Both are still a block (do-not-dispatch-now), and the
                    # row stays legally re-drivable: the worker's next
                    # resume() (PREPARED/RETRY_READY/DEPENDENCY_WAIT are all
                    # in worker.py's list_work -> resume() routing) re-runs
                    # this same gate on its next poll, so the lease clearing,
                    # the lock contention clearing, or an override resend all
                    # self-heal it with no manual intervention needed.
                    return self._result(claimable, detail_code=verdict.reason, detail=verdict.detail)
                claimed = await self._store.claim(
                    claimable.action_id,
                    claimable.state,
                    self._lease_owner,
                    self._lease_seconds,
                )
                failed = await self._store.definitive_fail(
                    claimed.action_id,
                    claimed.state,
                    self._lease_owner,
                    ProviderObservation(
                        ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
                        verdict.reason,
                        category="traffic_blocked",
                        retryable=False,
                        evidence={"detail": verdict.detail},
                    ),
                )
                return self._result(failed, detail=verdict.detail)
            logger.warning(
                "traffic control shadow would-block: %s %s wake=%s recipient=%s",
                verdict.reason,
                verdict.detail,
                context.wakeup_event_id,
                context.prospect_id,
            )
        elif verdict.check_failed:
            logger.warning("traffic control fail-open on wake %s", context.wakeup_event_id)
        return None

    async def status(self, action_id: UUID) -> PublicResult:
        return self._result(await self._require_action(action_id))

    async def suggest_targets(self, wakeup_event_id: int) -> dict[str, str]:
        return await self._context_loader.suggest_targets(wakeup_event_id)

    async def resume(self, action_id: UUID) -> PublicResult:
        action = await self._require_action(action_id)
        if not self._is_due(action):
            return self._result(action)
        context, context_detail = await self._verified_context(action)
        if context is None:
            return await self._manual_review(action, context_detail)
        # Worker-driven resume (worker.py's list_work -> resume for
        # dependency_wait/prepared/retry_ready) has no ExecuteRequest and
        # therefore no caller-supplied override -- a long-waited action
        # never gets to skip staleness just because nobody re-asked with
        # override=true. Same off/shadow/enforce semantics as execute().
        blocked = await self._check_traffic(action, context, override=False)
        if blocked is not None:
            return blocked

        async def _unchanged_fallback() -> PublicResult:
            return self._result(action)

        return await self._dispatch_stage(action, context, otherwise=_unchanged_fallback)

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
                if expected_state is ActionState.DISPATCHING or (
                    expected_state is ActionState.RECONCILING and context.operation in TENANTCLOUD_OPERATIONS
                ):
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
        if action.retry_of_action_id is not None:
            context = dataclass_replace(
                context,
                action_id=action.action_id,
                lock_holder=f"outbound-gateway:{action.action_id}",
            )
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
                "intent_kind": context.intent_kind,
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
    def _result(
        action: OutboundActionRecord,
        *,
        repeated: bool = False,
        detail: str | None = None,
        detail_code: str | None = None,
    ) -> PublicResult:
        return public_result(
            state=action.state,
            action_id=action.action_id,
            action_uid=action.action_uid,
            provider_request_ref=action.provider_request_ref,
            detail_code=detail_code or action.detail_code,
            completion_kind=action.completion_kind,
            repeated_execute=repeated,
            detail=detail,
        )
