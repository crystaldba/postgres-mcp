"""Bounded durable outbound reconciliation worker."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from .models import ActionState
from .service import OutboundActionService


class WorkerStore(Protocol):
    async def list_work(self, limit: int, max_attempts: int) -> list[tuple[UUID, ActionState]]: ...

    async def list_exhausted(self, limit: int, max_attempts: int) -> list[tuple[UUID, ActionState]]: ...


class OutboundWorker:
    def __init__(
        self,
        *,
        store: WorkerStore,
        service: OutboundActionService,
        batch_size: int = 20,
        max_attempts: int = 5,
        observability=None,
        on_error: Callable[[UUID, str, Exception], None] | None = None,
    ):
        self._store = store
        self._service = service
        self._batch_size = max(1, min(batch_size, 100))
        self._max_attempts = max(1, min(max_attempts, 100))
        self._observability = observability
        self._on_error = on_error or self._default_error

    @staticmethod
    def _default_error(action_id: UUID, operation: str, error: Exception) -> None:
        print(
            json.dumps(
                {
                    "action_id": str(action_id),
                    "error_type": type(error).__name__,
                    "event": "outbound_worker_action_failed",
                    "operation": operation,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    async def _run_isolated(self, action_id: UUID, operation: str) -> None:
        try:
            await getattr(self._service, operation)(action_id)
        except Exception as exc:
            self._on_error(action_id, operation, exc)

    async def run_once(self) -> int:
        exhausted = await self._store.list_exhausted(self._batch_size, self._max_attempts)
        for action_id, _state in exhausted:
            await self._run_isolated(action_id, "exhaust")
        work = await self._store.list_work(self._batch_size, self._max_attempts)
        for action_id, state in work:
            if state in {
                ActionState.UNKNOWN,
                ActionState.RECONCILING,
                ActionState.DISPATCHING,
                ActionState.PROVIDER_ACCEPTED,
            }:
                await self._run_isolated(action_id, "reconcile")
            elif state in {ActionState.PREPARED, ActionState.RETRY_READY, ActionState.DEPENDENCY_WAIT}:
                await self._run_isolated(action_id, "resume")
        if self._observability is not None:
            alerts = await self._observability.scan_alerts()
            for alert in alerts:
                print(alert.as_json(), flush=True)
        return len(exhausted) + len(work)
