"""TenantCloud provider adapter.

Adapts the shared, readback-verified ``TenantCloudMutations`` facade (owned by
a different repository -- see ``scripts/tenantcloud_mutations.py`` in
Comm-Data-Store) to this gateway's provider-neutral ``ProviderAdapter``
contract. This module contains no URLs, no HTTP, and no authentication: all
of that lives behind the facade instance injected at construction.

The facade already performs, for every write: an idempotency pre-check via
its named ``reconcile_*`` methods, at most one exact write, and an exact
post-write readback. This adapter only translates facade results
(``MutationExecution`` / ``ReconciliationResult`` / ``MutationObservation``)
into ``ProviderObservation``. It never imports the facade's
``MutationOperation`` enum -- doing so would require a cross-repo import --
so it relies exclusively on the facade's per-operation named methods and
duck-types disposition checks off the ``.value`` string (``MutationDisposition``
mixes in ``str``, so this is stable across repos).

The durable schema source of truth is migration 118
(``118_tenantcloud_outbound_gateway_operations.sql`` in Comm-Data-Store).
Its ``transition_outbound_action`` acceptance guard requires the persisted
provider observation to contain *exactly* six keys (see
``tenantcloud_shared.READBACK_OBSERVATION_KEYS``) and requires
``target_reference`` to match what was persisted into
``outbound_actions.arguments`` at enqueue time -- which, for maintenance
create, is *not* the same string the facade uses as its own per-write
reference (the eventual provider object id does not exist at enqueue time).
See ``tenantcloud_shared.py`` for why these two "target reference" concepts
are kept separate.
"""

from __future__ import annotations

from typing import Any
from typing import Callable
from typing import Mapping
from typing import Protocol
from uuid import UUID

from ..context import ActionContext
from ..models import Operation
from ..tenantcloud_shared import TENANTCLOUD_OPERATIONS
from ..tenantcloud_shared import tenantcloud_target_reference
from .base import ProviderDisposition
from .base import ProviderObservation
from .base import ProviderReceipt
from .base import ProviderRequest
from .base import accepted_observation
from .base import receipt_from_observation

_MAINTENANCE_CREATE_FIELDS = (
    "category_id",
    "title",
    "priority",
    "initiated_at",
    "text",
    "entry_allowed",
    "available_on",
)


class TenantCloudMutationsProtocol(Protocol):
    """Structural shape of the shared facade this adapter depends on."""

    def send_message(self, thread_id: object, body: object) -> Any: ...

    def mark_lead_working(self, lead_id: object) -> Any: ...

    def create_maintenance_request(self, **kwargs: object) -> Any: ...

    def update_maintenance_status(self, request_id: object, status: object) -> Any: ...

    def reconcile_message(self, thread_id: object, body: object, *, source_turn_at: object) -> Any: ...

    def reconcile_lead_status(self, lead_id: object) -> Any: ...

    def reconcile_maintenance_create(self, *, dispatched_after: object, **kwargs: object) -> Any: ...

    def reconcile_maintenance_status(self, request_id: object, status: object) -> Any: ...


class TenantCloudAdapter:
    """Adapter over the Comm-Data-Store TenantCloud facade.

    ``mutations_factory`` exists because the facade's AuthRefreshBudget is
    SCAN-LOCAL by design (see the 2026-07-21 inactive-runner-refresh spec):
    it permits one token refresh and then caches that token for the budget's
    lifetime. That is correct for a short batch and wrong for a daemon -- the
    gateway used to build one facade in build_runtime() and reuse it for the
    container's whole life, so the first refresh cached a token that was then
    served forever. One Firefox lapse latched TenantCloud writes dead until
    someone restarted the container (2026-08-10, three leads stranded).

    Building a facade per operation restores the lifetime the budget assumes.
    A single operation still shares one facade, so the pre-write readback and
    the write itself remain in the same scan and the anti-storm one-refresh
    reservation still holds within a dispatch.
    """

    def __init__(self, *, mutations_factory: Callable[[], TenantCloudMutationsProtocol]):
        # Factory only, deliberately. Accepting a ready-made facade would let a
        # caller hand in a long-lived one and silently re-create the 2026-08-10
        # latch; the API should not permit the bug it was written to fix.
        # Tests that want a fixed double pass ``mutations_factory=lambda: double``.
        self._mutations_factory = mutations_factory

    def _facade(self) -> TenantCloudMutationsProtocol:
        """One facade -- and so one auth budget -- per gateway operation."""
        return self._mutations_factory()

    def validate(self, context: ActionContext) -> None:
        if context.operation not in TENANTCLOUD_OPERATIONS:
            raise ValueError("TenantCloud adapter requires a tenantcloud.* operation")
        if not context.target.verified:
            raise ValueError("TenantCloud adapter requires a verified target")

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest:
        self.validate(context)
        del action_uid
        operation = context.operation
        # The stable, pre-write-knowable target reference (see
        # tenantcloud_shared.py) travels with the request so invoke() -- which
        # the ProviderAdapter protocol hands only (client, request), not the
        # full context -- can attach it to the six-key acceptance payload.
        target_reference = tenantcloud_target_reference(context)
        if operation is Operation.TENANTCLOUD_MESSAGE_SEND:
            arguments: dict[str, Any] = {
                "thread_id": context.target.target_id,
                "body": str(context.arguments["text"]),
                "source_sent_at": context.source_sent_at,
                "target_reference": target_reference,
            }
        elif operation is Operation.TENANTCLOUD_LEAD_STATUS_UPDATE:
            arguments = {"lead_id": context.target.target_id, "target_reference": target_reference}
        elif operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
            provider_ids = context.canonical_context["provider_ids"]
            arguments = {
                "property_id": provider_ids["property_id"],
                "unit_id": provider_ids["unit_id"],
                "source_sent_at": context.source_sent_at,
                "target_reference": target_reference,
                **{field: context.arguments[field] for field in _MAINTENANCE_CREATE_FIELDS},
            }
        else:
            arguments = {
                "request_id": context.target.target_id,
                "status": context.arguments["status"],
                "target_reference": target_reference,
            }
        return ProviderRequest(server_name="tenantcloud", tool=operation.value, arguments=arguments)

    async def invoke(self, client: Any, request: ProviderRequest) -> ProviderObservation:
        del client
        arguments = request.arguments
        operation_value = request.tool
        target_reference = arguments["target_reference"]
        mutations = self._facade()
        if operation_value == Operation.TENANTCLOUD_MESSAGE_SEND.value:
            return self._invoke_message(mutations, arguments, operation_value, target_reference)
        if operation_value == Operation.TENANTCLOUD_LEAD_STATUS_UPDATE.value:
            return self._invoke_lead_status(mutations, arguments, operation_value, target_reference)
        if operation_value == Operation.TENANTCLOUD_MAINTENANCE_CREATE.value:
            return self._invoke_maintenance_create(mutations, arguments, operation_value, target_reference)
        return self._invoke_maintenance_status(mutations, arguments, operation_value, target_reference)

    async def poll(self, client: Any, observation: ProviderObservation) -> ProviderObservation:
        # The facade's writes and readbacks are synchronous and always
        # terminal: there is no queued/pending provider state to poll.
        del client
        return observation

    def parse_receipt(self, context: ActionContext, observation: ProviderObservation) -> ProviderReceipt | None:
        self.validate(context)
        return receipt_from_observation(observation)

    async def reconcile(
        self,
        client: Any,
        context: ActionContext,
        action_uid: UUID,
        observation: ProviderObservation,
    ) -> ProviderObservation:
        del client, action_uid, observation
        operation = context.operation
        operation_value = operation.value
        target_reference = tenantcloud_target_reference(context)
        mutations = self._facade()
        if operation is Operation.TENANTCLOUD_MESSAGE_SEND:
            result = mutations.reconcile_message(
                context.target.target_id,
                str(context.arguments["text"]),
                source_turn_at=context.source_sent_at,
            )
            return self._from_reconciliation(
                result,
                kind="message",
                operation_value=operation_value,
                target_reference=target_reference,
                accepted_detail="tenantcloud_message_reconciled",
            )
        if operation is Operation.TENANTCLOUD_LEAD_STATUS_UPDATE:
            result = mutations.reconcile_lead_status(context.target.target_id)
            return self._from_reconciliation(
                result,
                kind="lead",
                operation_value=operation_value,
                target_reference=target_reference,
                accepted_detail="tenantcloud_lead_status_reconciled",
                definitive_absence_detail="tenantcloud_lead_status_not_yet_applied",
            )
        if operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
            provider_ids = context.canonical_context["provider_ids"]
            kwargs = {
                "property_id": provider_ids["property_id"],
                "unit_id": provider_ids["unit_id"],
                **{field: context.arguments[field] for field in _MAINTENANCE_CREATE_FIELDS},
            }
            result = mutations.reconcile_maintenance_create(dispatched_after=context.source_sent_at, **kwargs)
            return self._from_reconciliation(
                result,
                kind="maintenance",
                operation_value=operation_value,
                target_reference=target_reference,
                accepted_detail="tenantcloud_maintenance_create_reconciled",
            )
        result = mutations.reconcile_maintenance_status(context.target.target_id, context.arguments["status"])
        return self._from_reconciliation(
            result,
            kind="maintenance",
            operation_value=operation_value,
            target_reference=target_reference,
            accepted_detail="tenantcloud_maintenance_status_reconciled",
            definitive_absence_detail="tenantcloud_maintenance_status_not_yet_applied",
        )

    # -- per-operation dispatch -------------------------------------------------
    # Every path performs a pre-write idempotency check via the facade's own
    # reconcile_* method (the same bounded read used for crash recovery)
    # before ever issuing a write. On the happy path this costs one extra
    # provider round trip per dispatch attempt; that is the deliberate,
    # paranoid trade this codebase already makes for post-write readback.

    def _invoke_message(self, mutations: TenantCloudMutationsProtocol, arguments: Mapping[str, Any], operation_value: str, target_reference: str) -> ProviderObservation:
        thread_id = arguments["thread_id"]
        body = arguments["body"]
        pre = mutations.reconcile_message(thread_id, body, source_turn_at=arguments["source_sent_at"])
        if pre.disposition.value == "accepted":
            return self._accepted_from_observation(
                pre.observation,
                kind="message",
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code="tenantcloud_message_already_present",
            )
        execution = mutations.send_message(thread_id, body)
        return self._from_execution(
            execution,
            kind="message",
            operation_value=operation_value,
            target_reference=target_reference,
            accepted_detail="tenantcloud_message_accepted",
        )

    def _invoke_lead_status(self, mutations: TenantCloudMutationsProtocol, arguments: Mapping[str, Any], operation_value: str, target_reference: str) -> ProviderObservation:
        lead_id = arguments["lead_id"]
        pre = mutations.reconcile_lead_status(lead_id)
        if pre.disposition.value == "accepted":
            return self._accepted_from_observation(
                pre.observation,
                kind="lead",
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code="tenantcloud_lead_status_already_present",
            )
        execution = mutations.mark_lead_working(lead_id)
        return self._from_execution(
            execution,
            kind="lead",
            operation_value=operation_value,
            target_reference=target_reference,
            accepted_detail="tenantcloud_lead_status_accepted",
        )

    def _invoke_maintenance_create(self, mutations: TenantCloudMutationsProtocol, arguments: Mapping[str, Any], operation_value: str, target_reference: str) -> ProviderObservation:
        kwargs = {
            "property_id": arguments["property_id"],
            "unit_id": arguments["unit_id"],
            **{field: arguments[field] for field in _MAINTENANCE_CREATE_FIELDS},
        }
        pre = mutations.reconcile_maintenance_create(dispatched_after=arguments["source_sent_at"], **kwargs)
        if pre.disposition.value == "accepted":
            return self._accepted_from_observation(
                pre.observation,
                kind="maintenance",
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code="tenantcloud_maintenance_already_present",
            )
        execution = mutations.create_maintenance_request(**kwargs)
        return self._from_execution(
            execution,
            kind="maintenance",
            operation_value=operation_value,
            target_reference=target_reference,
            accepted_detail="tenantcloud_maintenance_create_accepted",
        )

    def _invoke_maintenance_status(self, mutations: TenantCloudMutationsProtocol, arguments: Mapping[str, Any], operation_value: str, target_reference: str) -> ProviderObservation:
        request_id = arguments["request_id"]
        status = arguments["status"]
        pre = mutations.reconcile_maintenance_status(request_id, status)
        if pre.disposition.value == "accepted":
            return self._accepted_from_observation(
                pre.observation,
                kind="maintenance",
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code="tenantcloud_maintenance_status_already_present",
            )
        execution = mutations.update_maintenance_status(request_id, status)
        return self._from_execution(
            execution,
            kind="maintenance",
            operation_value=operation_value,
            target_reference=target_reference,
            accepted_detail="tenantcloud_maintenance_status_accepted",
        )

    # -- result translation -------------------------------------------------

    @staticmethod
    def _provider_reference(kind: str, provider_object_id: str) -> str:
        if kind == "message":
            return f"tenantcloud-message:{provider_object_id}"
        if kind == "lead":
            return f"tenantcloud-lead:{provider_object_id}:working"
        return f"tenantcloud-maintenance:{provider_object_id}"

    @classmethod
    def _accepted_from_observation(
        cls,
        observation: Any,
        *,
        kind: str,
        operation_value: str,
        target_reference: str,
        detail_code: str,
    ) -> ProviderObservation:
        if not observation.target_reference:
            raise ValueError("verified TenantCloud readback requires a provider reference")
        # Exactly migration 118's six required keys, plus the facade's own
        # opaque evidence_hash (a seventh key the store strips before
        # sending p_observation -- 118_...sql:353-364 rejects extra keys).
        evidence = {
            "canonical_observed_state": dict(observation.canonical_observed_state),
            "operation": operation_value,
            "provider_object_id": observation.provider_object_id,
            "target_reference": target_reference,
            "readback_timestamp": observation.readback_timestamp,
            "readback_verified": bool(observation.readback_verified),
            "evidence_hash": observation.evidence_hash,
        }
        # provider_request_ref / evidence_reference must be equal to each
        # other (118_...sql:351) and are always the facade's own natural
        # per-write reference, even where it differs from the stable
        # target_reference above (maintenance create).
        return accepted_observation(
            request_ref_value=observation.target_reference,
            message_id=cls._provider_reference(kind, observation.provider_object_id),
            detail_code=detail_code,
            evidence=evidence,
        )

    @classmethod
    def _from_execution(
        cls,
        execution: Any,
        *,
        kind: str,
        operation_value: str,
        target_reference: str,
        accepted_detail: str,
    ) -> ProviderObservation:
        if execution.verified:
            return cls._accepted_from_observation(
                execution.observation,
                kind=kind,
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code=accepted_detail,
            )
        disposition_value = execution.mutation.disposition.value
        error_code = execution.mutation.audit.error_code
        if disposition_value == "definitive_non_acceptance":
            retryable = error_code == "authentication_unavailable"
            return ProviderObservation(
                ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
                "tenantcloud_auth_rejected_before_dispatch" if retryable else "tenantcloud_provider_rejected",
                category="provider_authentication" if retryable else "provider_rejected",
                retryable=retryable,
                evidence={"kind": "http_status", "error_code": error_code} if error_code else None,
            )
        # ACCEPTED-but-unverified or UNKNOWN: the write may or may not have
        # taken effect. Never trust it and never retry blindly -- route to
        # bounded reconciliation instead.
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            f"tenantcloud_write_ambiguous_{execution.error_code or 'unknown'}",
        )

    @classmethod
    def _from_reconciliation(
        cls,
        result: Any,
        *,
        kind: str,
        operation_value: str,
        target_reference: str,
        accepted_detail: str,
        definitive_absence_detail: str | None = None,
    ) -> ProviderObservation:
        disposition_value = result.disposition.value
        if disposition_value == "accepted":
            return cls._accepted_from_observation(
                result.observation,
                kind=kind,
                operation_value=operation_value,
                target_reference=target_reference,
                detail_code=accepted_detail,
            )
        if disposition_value == "definitive_non_acceptance":
            if result.error_code == "authentication_unavailable":
                # The reconciliation read itself could not authenticate --
                # exactly the signal _from_execution already trusts as
                # provably pre-dispatch (nothing could have been written by
                # a request that requires the same or greater authentication
                # than this read just failed to obtain). Retryable on every
                # operation kind, including the two "create" operations
                # (message send, maintenance create) whose
                # definitive_absence_detail is None below -- without this
                # branch, those two fell through to the generic ambiguous
                # case and escalated toward reconcile/manual_review instead
                # of retrying.
                return ProviderObservation(
                    ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
                    "tenantcloud_auth_rejected_before_dispatch",
                    category="provider_authentication",
                    retryable=True,
                )
            if definitive_absence_detail is not None:
                return ProviderObservation(
                    ProviderDisposition.DEFINITIVE_NON_ACCEPTANCE,
                    definitive_absence_detail,
                    category="provider_state_not_yet_applied",
                    retryable=True,
                )
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            f"tenantcloud_reconciliation_{result.error_code or 'inconclusive'}",
        )
