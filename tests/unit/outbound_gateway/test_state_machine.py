# pyright: reportCallIssue=false

from uuid import UUID

import pytest
from pydantic import ValidationError

from postgres_mcp.outbound_gateway.models import ActionState
from postgres_mcp.outbound_gateway.models import CompletionKind
from postgres_mcp.outbound_gateway.models import PublicStatus
from postgres_mcp.outbound_gateway.state_machine import ALLOWED_TRANSITIONS
from postgres_mcp.outbound_gateway.state_machine import InvalidTransitionError
from postgres_mcp.outbound_gateway.state_machine import LockDisposition
from postgres_mcp.outbound_gateway.state_machine import lock_disposition
from postgres_mcp.outbound_gateway.state_machine import public_result
from postgres_mcp.outbound_gateway.state_machine import validate_transition

EXPECTED_TRANSITIONS = {
    ActionState.RECEIVED: {ActionState.REJECTED, ActionState.STALE, ActionState.DEPENDENCY_WAIT, ActionState.PREPARED, ActionState.COMPLETED},
    ActionState.DEPENDENCY_WAIT: {ActionState.REJECTED, ActionState.STALE, ActionState.PREPARED, ActionState.DEAD_LETTER},
    ActionState.PREPARED: {ActionState.REJECTED, ActionState.STALE, ActionState.DISPATCHING, ActionState.DEFINITIVE_FAILED, ActionState.DEAD_LETTER},
    ActionState.DISPATCHING: {ActionState.PROVIDER_ACCEPTED, ActionState.UNKNOWN, ActionState.RETRY_READY, ActionState.DEFINITIVE_FAILED},
    ActionState.PROVIDER_ACCEPTED: {ActionState.COMPLETED, ActionState.UNKNOWN},
    ActionState.UNKNOWN: {ActionState.RECONCILING, ActionState.DEAD_LETTER},
    ActionState.RECONCILING: {ActionState.COMPLETED, ActionState.UNKNOWN, ActionState.RETRY_READY, ActionState.DEAD_LETTER},
    ActionState.RETRY_READY: {ActionState.DISPATCHING, ActionState.DEFINITIVE_FAILED, ActionState.DEAD_LETTER},
    ActionState.DEAD_LETTER: {ActionState.MANUAL_REVIEW},
    ActionState.MANUAL_REVIEW: {ActionState.COMPLETED, ActionState.DEFINITIVE_FAILED},
    ActionState.COMPLETED: set(),
    ActionState.STALE: set(),
    ActionState.REJECTED: set(),
    ActionState.DEFINITIVE_FAILED: set(),
}


def test_allowed_transition_graph_is_exact_and_every_edge_validates():
    assert ALLOWED_TRANSITIONS == EXPECTED_TRANSITIONS
    for current, targets in EXPECTED_TRANSITIONS.items():
        for target in targets:
            validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ActionState.RECEIVED, ActionState.DISPATCHING),
        (ActionState.UNKNOWN, ActionState.RETRY_READY),
        (ActionState.PROVIDER_ACCEPTED, ActionState.DEFINITIVE_FAILED),
        (ActionState.COMPLETED, ActionState.PREPARED),
        (ActionState.MANUAL_REVIEW, ActionState.DISPATCHING),
    ],
)
def test_invalid_transitions_raise(current, target):
    with pytest.raises(InvalidTransitionError):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ActionState.RECEIVED, LockDisposition.NONE),
        (ActionState.DEPENDENCY_WAIT, LockDisposition.NONE),
        (ActionState.PREPARED, LockDisposition.HELD),
        (ActionState.DISPATCHING, LockDisposition.HELD),
        (ActionState.PROVIDER_ACCEPTED, LockDisposition.HELD),
        (ActionState.UNKNOWN, LockDisposition.RETAINED),
        (ActionState.RECONCILING, LockDisposition.RETAINED),
        (ActionState.RETRY_READY, LockDisposition.HELD),
        (ActionState.COMPLETED, LockDisposition.COMPLETED),
        (ActionState.STALE, LockDisposition.NONE_OR_RELEASED),
        (ActionState.REJECTED, LockDisposition.NONE_OR_RELEASED),
        (ActionState.DEFINITIVE_FAILED, LockDisposition.RELEASED),
        (ActionState.DEAD_LETTER, LockDisposition.RETAIN_IF_AMBIGUOUS),
        (ActionState.MANUAL_REVIEW, LockDisposition.UNCHANGED),
    ],
)
def test_lock_disposition_is_derived_from_durable_state(state, expected):
    assert lock_disposition(state) == expected


@pytest.mark.parametrize(
    ("state", "completion_kind", "repeated", "expected"),
    [
        (ActionState.RECEIVED, None, False, PublicStatus.PENDING),
        (ActionState.DEPENDENCY_WAIT, None, False, PublicStatus.PENDING),
        (ActionState.PREPARED, None, False, PublicStatus.PENDING),
        (ActionState.DISPATCHING, None, False, PublicStatus.PENDING),
        (ActionState.PROVIDER_ACCEPTED, None, False, PublicStatus.PENDING),
        (ActionState.RETRY_READY, None, False, PublicStatus.PENDING),
        (ActionState.UNKNOWN, None, False, PublicStatus.UNKNOWN),
        (ActionState.RECONCILING, None, False, PublicStatus.UNKNOWN),
        (ActionState.COMPLETED, CompletionKind.SENT, False, PublicStatus.SENT),
        (ActionState.COMPLETED, CompletionKind.DUPLICATE, False, PublicStatus.DUPLICATE),
        (ActionState.COMPLETED, CompletionKind.SENT, True, PublicStatus.DUPLICATE),
        (ActionState.STALE, None, False, PublicStatus.STALE),
        (ActionState.REJECTED, None, False, PublicStatus.REJECTED),
        (ActionState.DEFINITIVE_FAILED, None, False, PublicStatus.FAILED),
        (ActionState.DEAD_LETTER, None, False, PublicStatus.MANUAL_REVIEW),
        (ActionState.MANUAL_REVIEW, None, False, PublicStatus.MANUAL_REVIEW),
    ],
)
def test_public_result_is_normalized_and_never_exposes_raw_provider_payload(state, completion_kind, repeated, expected):
    result = public_result(
        state=state,
        action_id=UUID("8f8f1a45-13a7-4bd3-a15a-f8d265bbc567"),
        action_uid=None,
        provider_request_ref="request-1",
        detail_code="stable_code",
        completion_kind=completion_kind,
        repeated_execute=repeated,
    )
    assert result.status == expected
    assert result.retryable is False
    assert set(result.model_dump()) == {
        "status",
        "action_id",
        "action_uid",
        "provider_request_ref",
        "retryable",
        "detail_code",
    }
    with pytest.raises(ValidationError):
        type(result)(**result.model_dump(), raw_provider_payload={"secret": "x"})


def test_completed_state_requires_completion_kind():
    with pytest.raises(ValueError, match="completion kind"):
        public_result(
            state=ActionState.COMPLETED,
            action_id=UUID("8f8f1a45-13a7-4bd3-a15a-f8d265bbc567"),
            action_uid=None,
            provider_request_ref="request-1",
            detail_code="stable_code",
        )
