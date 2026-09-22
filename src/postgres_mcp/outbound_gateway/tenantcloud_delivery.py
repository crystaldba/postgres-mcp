"""TenantCloud-only durable delivery coordinator and Restate ingress adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import StrEnum
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Protocol
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener
from uuid import UUID

from .models import ActionState
from .models import PublicStatus
from .tenantcloud_shared import TENANTCLOUD_OPERATIONS


class AuthState(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    TRANSPORT_FAILURE = "transport_failure"
    PROVIDER_FAILURE = "provider_failure"


@dataclass(frozen=True)
class AuthResult:
    state: AuthState
    retry_after_seconds: int = 0


class DeliveryPhase(StrEnum):
    COMPLETE = "complete"
    WAIT = "wait"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class DeliveryResult:
    phase: DeliveryPhase
    detail_code: str
    retry_after_seconds: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "detail_code": self.detail_code,
            "retry_after_seconds": self.retry_after_seconds,
        }


class DeliveryStore(Protocol):
    async def get(self, action_id: UUID) -> Any: ...


class DeliveryService(Protocol):
    async def prepare(self, action_id: UUID) -> Any: ...

    async def resume(self, action_id: UUID) -> Any: ...

    async def reconcile(self, action_id: UUID) -> Any: ...

    async def exhaust(self, action_id: UUID) -> Any: ...


class AuthGate(Protocol):
    async def ensure_ready(self) -> AuthResult: ...


_AMBIGUOUS_STATES = {
    ActionState.DISPATCHING,
    ActionState.PROVIDER_ACCEPTED,
    ActionState.UNKNOWN,
    ActionState.RECONCILING,
}
_RESUMABLE_STATES = {
    ActionState.RECEIVED,
    ActionState.PREPARED,
    ActionState.RETRY_READY,
    ActionState.DEPENDENCY_WAIT,
}
_TERMINAL_STATES = {
    ActionState.COMPLETED,
    ActionState.STALE,
    ActionState.REJECTED,
    ActionState.DEFINITIVE_FAILED,
    ActionState.DEAD_LETTER,
    ActionState.MANUAL_REVIEW,
}
_CONTEXT_WAIT_DETAILS = {
    "persisted_context_unavailable",
    "persisted_context_mismatch",
}


class TenantCloudDeliveryCoordinator:
    """One idempotent advance over CDS state. No workflow/runtime details."""

    def __init__(
        self,
        *,
        store: DeliveryStore,
        service: DeliveryService,
        auth: AuthGate,
        max_attempts: int = 5,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._service = service
        self._auth = auth
        self._max_attempts = max(1, max_attempts)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def status(self, action_id: UUID) -> DeliveryResult:
        action = await self._store.get(action_id)
        if action is None:
            return DeliveryResult(DeliveryPhase.TERMINAL, "action_not_found")
        if action.operation not in TENANTCLOUD_OPERATIONS:
            return DeliveryResult(DeliveryPhase.TERMINAL, "operation_not_tenantcloud")
        if action.state is ActionState.COMPLETED:
            return DeliveryResult(DeliveryPhase.COMPLETE, "completed")
        if action.state in _TERMINAL_STATES:
            return DeliveryResult(DeliveryPhase.TERMINAL, action.state.value)
        delay = max(
            1,
            int((action.next_attempt_at - self._clock()).total_seconds()),
        )
        return DeliveryResult(DeliveryPhase.WAIT, action.state.value, delay)

    async def advance(self, action_id: UUID) -> DeliveryResult:
        action = await self._store.get(action_id)
        if action is None:
            return DeliveryResult(DeliveryPhase.TERMINAL, "action_not_found")
        if action.operation not in TENANTCLOUD_OPERATIONS:
            return DeliveryResult(DeliveryPhase.TERMINAL, "operation_not_tenantcloud")
        if action.state is ActionState.COMPLETED:
            return DeliveryResult(DeliveryPhase.COMPLETE, "completed")
        if action.state in _TERMINAL_STATES:
            return DeliveryResult(DeliveryPhase.TERMINAL, action.state.value)
        if action.state not in (_AMBIGUOUS_STATES | _RESUMABLE_STATES):
            return DeliveryResult(DeliveryPhase.WAIT, "action_not_prepared", 5)

        auth = await self._auth.ensure_ready()
        if auth.state is not AuthState.READY:
            default_delay = {
                AuthState.LOGIN_REQUIRED: 300,
                AuthState.RUNNER_UNAVAILABLE: 30,
                AuthState.TRANSPORT_FAILURE: 30,
                AuthState.PROVIDER_FAILURE: 120,
            }[auth.state]
            return DeliveryResult(
                DeliveryPhase.WAIT,
                f"tenantcloud_auth_{auth.state.value}",
                max(1, auth.retry_after_seconds or default_delay),
            )

        if action.state is ActionState.RECEIVED:
            result = await self._service.prepare(action_id)
            if (
                result.status is PublicStatus.PENDING
                and getattr(result, "detail", None) in _CONTEXT_WAIT_DETAILS
            ):
                return DeliveryResult(
                    DeliveryPhase.WAIT,
                    result.detail,
                    300,
                )
        elif action.state in _AMBIGUOUS_STATES:
            result = await self._service.reconcile(action_id)
        elif action.attempt_count >= self._max_attempts:
            result = await self._service.exhaust(action_id)
        else:
            result = await self._service.resume(action_id)
        if result.status in {PublicStatus.SENT, PublicStatus.DUPLICATE}:
            return DeliveryResult(DeliveryPhase.COMPLETE, result.detail_code)
        if result.status in {
            PublicStatus.FAILED,
            PublicStatus.REJECTED,
            PublicStatus.STALE,
            PublicStatus.MANUAL_REVIEW,
        }:
            return DeliveryResult(DeliveryPhase.TERMINAL, result.detail_code)
        return await self.status(action_id)


class TenantCloudAuthGate:
    """Classify secret-bearing auth implementation into secret-free outcomes."""

    def __init__(
        self,
        auth_factory: Callable[[], Any],
        *,
        login_required_errors: tuple[type[BaseException], ...] = (),
        transport_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._auth_factory = auth_factory
        self._login_required_errors = login_required_errors
        self._transport_errors = transport_errors

    async def ensure_ready(self) -> AuthResult:
        try:
            await asyncio.to_thread(self._auth_factory().get_token)
        except self._login_required_errors:
            return AuthResult(AuthState.LOGIN_REQUIRED, 300)
        except self._transport_errors:
            return AuthResult(AuthState.TRANSPORT_FAILURE, 30)
        except Exception:
            return AuthResult(AuthState.RUNNER_UNAVAILABLE, 30)
        return AuthResult(AuthState.READY)


RequestFn = Callable[[str, bytes, dict[str, str], float], Awaitable[tuple[int, bytes]]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    def send() -> tuple[int, bytes]:
        request = Request(url, data=body, headers=headers, method="POST")
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(16_385)
        except HTTPError as error:
            try:
                return error.code, error.read(16_385)
            finally:
                error.close()
        except (URLError, OSError, TimeoutError) as error:
            raise RuntimeError("Restate ingress request failed") from error

    return await asyncio.to_thread(send)


class RestateWorkflowSubmitter:
    """Submit exactly one Workflow invocation keyed by CDS action UUID."""

    def __init__(self, ingress_url: str, *, request: RequestFn = _post) -> None:
        parsed = urlsplit(ingress_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Restate ingress URL must be literal HTTP loopback")
        self._ingress_url = ingress_url.rstrip("/")
        self._request = request

    async def submit(self, action_id: UUID) -> None:
        encoded_id = quote(str(action_id), safe="")
        url = f"{self._ingress_url}/TenantCloudDelivery/{encoded_id}/deliver/send"
        body = json.dumps({"action_id": str(action_id)}, separators=(",", ":")).encode("utf-8")
        status, _body = await self._request(
            url,
            body,
            {"Content-Type": "application/json"},
            15.0,
        )
        if len(_body) > 16_384:
            raise RuntimeError("Restate ingress response too large")
        if status not in {200, 201, 202, 409}:
            raise RuntimeError(f"Restate ingress returned HTTP {status}")


def build_restate_app(coordinator: TenantCloudDeliveryCoordinator):
    """Build lazily so normal gateway processes do not require Restate imports."""
    import restate

    workflow = restate.Workflow(
        "TenantCloudDelivery",
        inactivity_timeout=timedelta(minutes=10),
        abort_timeout=timedelta(minutes=15),
        ingress_private=False,
    )

    @workflow.main(workflow_retention=timedelta(days=30))
    async def deliver(ctx: Any, payload: object) -> dict[str, object]:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action_id"}
            or payload.get("action_id") != ctx.key()
        ):
            raise restate.TerminalError("action_id must equal workflow key", status_code=400)
        try:
            action_id = UUID(ctx.key())
        except ValueError as error:
            raise restate.TerminalError("invalid action_id", status_code=400) from error
        retry = restate.RunOptions(
            initial_retry_interval=timedelta(seconds=2),
            max_retry_interval=timedelta(minutes=5),
            retry_interval_factor=2.0,
        )
        step = 0
        while True:

            async def advance_step() -> dict[str, object]:
                return (await coordinator.advance(action_id)).to_dict()

            outcome = await ctx.run_typed(
                f"advance {step}",
                advance_step,
                retry,
            )
            if outcome["phase"] in {DeliveryPhase.COMPLETE.value, DeliveryPhase.TERMINAL.value}:
                return outcome
            delay = outcome.get("retry_after_seconds")
            if type(delay) is not int or delay < 1:
                raise restate.TerminalError("invalid delivery retry delay", status_code=500)
            await ctx.sleep(timedelta(seconds=min(delay, 3600)), name=f"wait {step}")
            step += 1

    return restate.app([workflow])
