"""Single source of truth for TenantCloud gateway constants and derivations.

Schema source of truth is
``migrations/118_tenantcloud_outbound_gateway_operations.sql`` in
Comm-Data-Store (a different repo -- read, never imported from here). This
module exists so `service.py`, `store.py`, and `adapters/tenantcloud.py`
agree on the same literals and the same derivation formulas instead of each
inventing their own (the divergence that caused the Task 7 review findings).

Two distinct "target reference" concepts are involved and must not be
confused:

* ``tenantcloud_target_reference`` -- a *pre-write-knowable* stable
  identifier persisted into ``outbound_actions.arguments->>'target_reference'``
  at enqueue time (before any provider call) and asserted again in
  ``p_observation->>'target_reference'`` at acceptance time
  (118_...sql:370). For message/lead/maintenance-status this coincides with
  the shared facade's own per-resource reference ("thread:<id>",
  "lead:<id>", "maintenance_request:<id>") because those targets already
  exist. For maintenance *create* the eventual provider object id does not
  exist yet at enqueue time, so the stable property/unit identifier is used
  instead -- this is deliberately *not* the facade's own
  ``MutationObservation.target_reference`` for that one operation (see
  adapters/tenantcloud.py).
* the facade's own per-write reference, used as ``provider_request_ref`` /
  ``evidence_reference`` (which migration 118 requires to be equal to each
  other, 118_...sql:351) -- always the facade's natural
  "kind:<resource-id>" string, even for maintenance create.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from typing import Mapping

from .models import Operation

if TYPE_CHECKING:
    from .context import ActionContext

TENANTCLOUD_OPERATIONS = frozenset(
    {
        Operation.TENANTCLOUD_MESSAGE_SEND,
        Operation.TENANTCLOUD_LEAD_STATUS_UPDATE,
        Operation.TENANTCLOUD_MAINTENANCE_CREATE,
        Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE,
    }
)

# Exact literal migration 118 requires for a TenantCloud provider_accepted
# transition (118_...sql:348). Do not rename without updating the migration.
EVIDENCE_KIND_VERIFIED_READBACK = "verified_provider_readback"

# Exactly the keys transition_outbound_action requires in p_observation for
# that transition -- presence AND no-extra-keys are both enforced
# (118_...sql:353-364).
READBACK_OBSERVATION_KEYS = frozenset(
    {
        "canonical_observed_state",
        "operation",
        "provider_object_id",
        "target_reference",
        "readback_timestamp",
        "readback_verified",
    }
)


def tenantcloud_target_reference(context: ActionContext) -> str:
    operation = context.operation
    if operation is Operation.TENANTCLOUD_MESSAGE_SEND:
        return f"thread:{context.target.target_id}"
    if operation is Operation.TENANTCLOUD_LEAD_STATUS_UPDATE:
        return f"lead:{context.target.target_id}"
    if operation is Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE:
        return f"maintenance_request:{context.target.target_id}"
    if operation is Operation.TENANTCLOUD_MAINTENANCE_CREATE:
        # Already "property:<id>:unit:<id>" -- see context.py's _target().
        return context.target.target_id
    raise ValueError("not a TenantCloud operation")


def normalize_tenantcloud_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("TenantCloud identifier must be an int or digit string")
    text = str(value)
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("TenantCloud identifier must be positive decimal digits")
    return str(int(text))


def normalize_tenantcloud_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("TenantCloud text must be a string")
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def normalize_tenantcloud_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("TenantCloud date must be a string")
    return datetime.strptime(value, "%Y-%m-%d").date().strftime("%m/%d/%Y")


def tenantcloud_desired_state(context: ActionContext) -> dict[str, Any]:
    """Best-effort mirror of the shared facade's post-write canonical state.

    This must match what the real ``TenantCloudMutations`` facade
    (Comm-Data-Store, ``scripts/tenantcloud_mutations.py``) normalizes a
    successful readback into, because migration 118 compares this value
    against the facade's actual ``canonical_observed_state`` byte-for-byte
    (118_...sql:366). It is reproduced here rather than imported because
    this repo cannot depend on that one. Production correctness of this
    replication can only be confirmed by an integration test against the
    real facade + migration 118 (out of scope for this gateway-side task).
    """
    operation = context.operation
    arguments = context.arguments
    if operation is Operation.TENANTCLOUD_MESSAGE_SEND:
        return {
            "thread_id": normalize_tenantcloud_id(context.target.target_id),
            "body": normalize_tenantcloud_text(str(arguments["text"])),
        }
    if operation is Operation.TENANTCLOUD_LEAD_STATUS_UPDATE:
        return {"status": "working"}
    if operation is Operation.TENANTCLOUD_MAINTENANCE_STATUS_UPDATE:
        return {"status": arguments["status"]}
    provider_ids = context.canonical_context["provider_ids"]
    desired: dict[str, Any] = {
        "property_id": normalize_tenantcloud_id(provider_ids["property_id"]),
        "unit_id": normalize_tenantcloud_id(provider_ids["unit_id"]),
        "category_id": normalize_tenantcloud_id(arguments["category_id"]),
        "title": normalize_tenantcloud_text(str(arguments["title"])),
        "priority": "normal",
        "initiated_at": normalize_tenantcloud_date(str(arguments["initiated_at"])),
        "text": normalize_tenantcloud_text(str(arguments["text"])),
        "entry_allowed": bool(arguments["entry_allowed"]),
        "status": 1,
    }
    if arguments.get("available_on") is not None:
        desired["available_on"] = normalize_tenantcloud_date(str(arguments["available_on"]))
    return desired


def tenantcloud_idempotency_key(context: ActionContext) -> str:
    return (
        f"v1:claim:{context.canonical_context['tenantcloud_claim_id']}:"
        f"source:{context.canonical_context['source_event_id']}:"
        f"op:{context.operation.value}:target:{tenantcloud_target_reference(context)}:"
        f"state:{context.canonical_scope['desired_state_hash']}"
    )


# The three gateway-owned keys tenantcloud_persisted_arguments() adds on
# top of the strict per-operation ArgumentModel fields. Every ArgumentModel
# is a StrictModel with extra="forbid" (models.py), so anything that rebuilds
# an ExecuteRequest from a persisted, enriched arguments dict (e.g.
# OutboundActionRecord.execute_request(), used by every reconcile()/resume()
# context reload) must strip these back out first.
TENANTCLOUD_PERSISTED_ARGUMENT_KEYS = frozenset({"desired_state", "target_reference", "idempotency_key"})


def tenantcloud_persisted_arguments(context: ActionContext) -> Mapping[str, Any]:
    """Arguments enriched with the fields migration 118's acceptance guard
    and bindings insert read directly off ``outbound_actions.arguments``
    (118_...sql:220-222,366,370,456-457): ``desired_state``,
    ``target_reference``, ``idempotency_key``. Non-TenantCloud callers must
    get ``context.arguments`` back unchanged."""
    if context.operation not in TENANTCLOUD_OPERATIONS:
        return context.arguments
    return {
        **dict(context.arguments),
        "desired_state": tenantcloud_desired_state(context),
        "target_reference": tenantcloud_target_reference(context),
        "idempotency_key": tenantcloud_idempotency_key(context),
    }


def strip_tenantcloud_persisted_argument_keys(operation: Operation, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """Inverse of the enrichment above -- reconstructs the strict, unenriched
    arguments an ExecuteRequest's ArgumentModel actually validates against,
    from a persisted (possibly enriched) arguments mapping. Non-TenantCloud
    arguments pass through untouched."""
    if operation not in TENANTCLOUD_OPERATIONS:
        return arguments
    return {key: value for key, value in arguments.items() if key not in TENANTCLOUD_PERSISTED_ARGUMENT_KEYS}
