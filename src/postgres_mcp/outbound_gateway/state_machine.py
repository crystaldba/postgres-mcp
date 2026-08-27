"""Pure durable-state transition and public-result mapping."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from .models import ActionState
from .models import CompletionKind
from .models import PublicResult
from .models import PublicStatus


class InvalidTransitionError(ValueError):
    pass


class LockDisposition(StrEnum):
    NONE = "none"
    HELD = "held"
    RETAINED = "retained"
    COMPLETED = "completed"
    RELEASED = "released"
    NONE_OR_RELEASED = "none_or_released"
    RETAIN_IF_AMBIGUOUS = "retain_if_ambiguous"
    UNCHANGED = "unchanged"


ALLOWED_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.RECEIVED: {
        ActionState.REJECTED,
        ActionState.STALE,
        ActionState.DEPENDENCY_WAIT,
        ActionState.PREPARED,
        ActionState.COMPLETED,
    },
    ActionState.DEPENDENCY_WAIT: {
        ActionState.REJECTED,
        ActionState.STALE,
        ActionState.PREPARED,
        ActionState.DEAD_LETTER,
    },
    ActionState.PREPARED: {
        ActionState.REJECTED,
        ActionState.STALE,
        ActionState.DISPATCHING,
        ActionState.DEFINITIVE_FAILED,
        ActionState.DEAD_LETTER,
    },
    ActionState.DISPATCHING: {
        ActionState.PROVIDER_ACCEPTED,
        ActionState.UNKNOWN,
        ActionState.RETRY_READY,
        ActionState.DEFINITIVE_FAILED,
    },
    ActionState.PROVIDER_ACCEPTED: {ActionState.COMPLETED, ActionState.UNKNOWN},
    ActionState.UNKNOWN: {ActionState.RECONCILING, ActionState.DEAD_LETTER},
    ActionState.RECONCILING: {
        ActionState.COMPLETED,
        ActionState.UNKNOWN,
        ActionState.RETRY_READY,
        ActionState.DEAD_LETTER,
    },
    ActionState.RETRY_READY: {
        ActionState.DISPATCHING,
        ActionState.DEFINITIVE_FAILED,
        ActionState.DEAD_LETTER,
    },
    ActionState.DEAD_LETTER: {ActionState.MANUAL_REVIEW},
    ActionState.MANUAL_REVIEW: {ActionState.COMPLETED, ActionState.DEFINITIVE_FAILED},
    ActionState.COMPLETED: set(),
    ActionState.STALE: set(),
    ActionState.REJECTED: set(),
    ActionState.DEFINITIVE_FAILED: set(),
}


LOCK_DISPOSITIONS: dict[ActionState, LockDisposition] = {
    ActionState.RECEIVED: LockDisposition.NONE,
    ActionState.DEPENDENCY_WAIT: LockDisposition.NONE,
    ActionState.PREPARED: LockDisposition.HELD,
    ActionState.DISPATCHING: LockDisposition.HELD,
    ActionState.PROVIDER_ACCEPTED: LockDisposition.HELD,
    ActionState.UNKNOWN: LockDisposition.RETAINED,
    ActionState.RECONCILING: LockDisposition.RETAINED,
    ActionState.RETRY_READY: LockDisposition.HELD,
    ActionState.COMPLETED: LockDisposition.COMPLETED,
    ActionState.STALE: LockDisposition.NONE_OR_RELEASED,
    ActionState.REJECTED: LockDisposition.NONE_OR_RELEASED,
    ActionState.DEFINITIVE_FAILED: LockDisposition.RELEASED,
    ActionState.DEAD_LETTER: LockDisposition.RETAIN_IF_AMBIGUOUS,
    ActionState.MANUAL_REVIEW: LockDisposition.UNCHANGED,
}


def validate_transition(current: ActionState, target: ActionState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"invalid outbound action transition: {current} -> {target}")


def lock_disposition(state: ActionState) -> LockDisposition:
    return LOCK_DISPOSITIONS[state]


def public_status(
    state: ActionState,
    completion_kind: CompletionKind | None = None,
    *,
    repeated_execute: bool = False,
) -> PublicStatus:
    if state in {
        ActionState.RECEIVED,
        ActionState.DEPENDENCY_WAIT,
        ActionState.PREPARED,
        ActionState.DISPATCHING,
        ActionState.PROVIDER_ACCEPTED,
        ActionState.RETRY_READY,
    }:
        return PublicStatus.PENDING
    if state in {ActionState.UNKNOWN, ActionState.RECONCILING}:
        return PublicStatus.UNKNOWN
    if state is ActionState.COMPLETED:
        if completion_kind is None:
            raise ValueError("completed state requires completion kind")
        if repeated_execute or completion_kind is CompletionKind.DUPLICATE:
            return PublicStatus.DUPLICATE
        return PublicStatus.SENT
    if state is ActionState.STALE:
        return PublicStatus.STALE
    if state is ActionState.REJECTED:
        return PublicStatus.REJECTED
    if state is ActionState.DEFINITIVE_FAILED:
        return PublicStatus.FAILED
    return PublicStatus.MANUAL_REVIEW


def public_result(
    *,
    state: ActionState,
    action_id: UUID,
    action_uid: UUID | None,
    provider_request_ref: str | None,
    detail_code: str,
    completion_kind: CompletionKind | None = None,
    repeated_execute: bool = False,
) -> PublicResult:
    return PublicResult(
        status=public_status(
            state,
            completion_kind,
            repeated_execute=repeated_execute,
        ),
        action_id=action_id,
        action_uid=action_uid,
        provider_request_ref=provider_request_ref,
        retryable=False,
        detail_code=detail_code,
    )
