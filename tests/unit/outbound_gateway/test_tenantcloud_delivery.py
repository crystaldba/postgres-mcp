from __future__ import annotations

import sys
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from postgres_mcp.outbound_gateway.models import ActionState
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.models import PublicStatus
from postgres_mcp.outbound_gateway.tenantcloud_delivery import AuthResult
from postgres_mcp.outbound_gateway.tenantcloud_delivery import AuthState
from postgres_mcp.outbound_gateway.tenantcloud_delivery import DeliveryPhase
from postgres_mcp.outbound_gateway.tenantcloud_delivery import DeliveryResult
from postgres_mcp.outbound_gateway.tenantcloud_delivery import RestateWorkflowSubmitter
from postgres_mcp.outbound_gateway.tenantcloud_delivery import TenantCloudDeliveryCoordinator
from postgres_mcp.outbound_gateway.tenantcloud_delivery import build_restate_app

ACTION_ID = UUID("4cbac369-48c6-5b62-95e9-41f50259e732")


def action(state: ActionState, *, attempts: int = 0):
    return SimpleNamespace(
        action_id=ACTION_ID,
        operation=Operation.TENANTCLOUD_MESSAGE_SEND,
        state=state,
        attempt_count=attempts,
        next_attempt_at=datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_auth_outage_waits_without_consuming_provider_attempt() -> None:
    row = action(ActionState.PREPARED, attempts=2)
    store = AsyncMock()
    store.get.return_value = row
    service = AsyncMock()
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.TRANSPORT_FAILURE, retry_after_seconds=30)
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth, max_attempts=5)

    result = await coordinator.advance(ACTION_ID)

    assert result.phase is DeliveryPhase.WAIT
    assert result.detail_code == "tenantcloud_auth_transport_failure"
    assert result.retry_after_seconds == 30
    assert result.to_dict() == {
        "phase": "wait",
        "detail_code": "tenantcloud_auth_transport_failure",
        "retry_after_seconds": 30,
    }
    assert row.attempt_count == 2
    service.resume.assert_not_called()
    service.reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_ready_action_resumes_once_after_auth_self_heals() -> None:
    row = action(ActionState.RETRY_READY, attempts=1)
    store = AsyncMock()
    store.get.return_value = row
    service = AsyncMock()
    service.resume.return_value = SimpleNamespace(status=PublicStatus.SENT, detail_code="provider_receipt_verified")
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.READY)
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth, max_attempts=5)

    result = await coordinator.advance(ACTION_ID)

    assert result.phase is DeliveryPhase.COMPLETE
    service.resume.assert_awaited_once_with(ACTION_ID)


@pytest.mark.asyncio
async def test_received_remediation_is_prepared_before_any_provider_io() -> None:
    received = action(ActionState.RECEIVED)
    prepared = action(ActionState.PREPARED)
    store = AsyncMock()
    store.get.side_effect = [received, prepared]
    service = AsyncMock()
    service.prepare.return_value = SimpleNamespace(
        status=PublicStatus.PENDING,
        detail_code="prepared",
    )
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.READY)
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth)

    result = await coordinator.advance(ACTION_ID)

    assert result.phase is DeliveryPhase.WAIT
    service.prepare.assert_awaited_once_with(ACTION_ID)
    service.resume.assert_not_called()


@pytest.mark.asyncio
async def test_unavailable_persisted_context_waits_without_hot_loop() -> None:
    received = action(ActionState.RECEIVED)
    store = AsyncMock()
    store.get.return_value = received
    service = AsyncMock()
    service.prepare.return_value = SimpleNamespace(
        status=PublicStatus.PENDING,
        detail_code="operator_remediation_created",
        detail="persisted_context_unavailable",
    )
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.READY)
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth)

    result = await coordinator.advance(ACTION_ID)

    assert result == DeliveryResult(
        DeliveryPhase.WAIT,
        "persisted_context_unavailable",
        300,
    )
    store.get.assert_awaited_once_with(ACTION_ID)


@pytest.mark.asyncio
async def test_restarted_step_reconciles_dispatching_action_never_blind_resends() -> None:
    row = action(ActionState.PREPARED)
    store = AsyncMock()
    store.get.side_effect = lambda _action_id: row
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.READY)
    service = AsyncMock()

    async def crash_after_dispatch(_action_id):
        row.state = ActionState.DISPATCHING
        raise RuntimeError("worker died after provider accepted request")

    service.resume.side_effect = crash_after_dispatch
    service.reconcile.return_value = SimpleNamespace(status=PublicStatus.SENT, detail_code="provider_receipt_verified")
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth, max_attempts=5)

    with pytest.raises(RuntimeError, match="worker died"):
        await coordinator.advance(ACTION_ID)
    result = await coordinator.advance(ACTION_ID)

    assert result.phase is DeliveryPhase.COMPLETE
    service.resume.assert_awaited_once_with(ACTION_ID)
    service.reconcile.assert_awaited_once_with(ACTION_ID)


@pytest.mark.asyncio
async def test_ambiguous_action_reconciles_even_at_send_retry_limit() -> None:
    row = action(ActionState.UNKNOWN, attempts=5)
    store = AsyncMock()
    store.get.return_value = row
    service = AsyncMock()
    service.reconcile.return_value = SimpleNamespace(status=PublicStatus.UNKNOWN, detail_code="reconciliation_no_match")
    auth = AsyncMock()
    auth.ensure_ready.return_value = AuthResult(AuthState.READY)
    coordinator = TenantCloudDeliveryCoordinator(store=store, service=service, auth=auth, max_attempts=5)

    result = await coordinator.advance(ACTION_ID)

    assert result.phase is DeliveryPhase.WAIT
    service.reconcile.assert_awaited_once_with(ACTION_ID)
    service.exhaust.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_restate_submission_is_success() -> None:
    calls = []

    async def request(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 409, b"already invoked"

    submitter = RestateWorkflowSubmitter("http://127.0.0.1:18080", request=request)

    await submitter.submit(ACTION_ID)

    assert calls[0][0].endswith(f"/TenantCloudDelivery/{ACTION_ID}/deliver/send")
    assert calls[0][1] == b'{"action_id":"4cbac369-48c6-5b62-95e9-41f50259e732"}'


@pytest.mark.asyncio
async def test_restate_submission_rejects_non_success_status() -> None:
    async def request(_url, _body, _headers, _timeout):
        return 503, b"unavailable"

    submitter = RestateWorkflowSubmitter("http://127.0.0.1:18080", request=request)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await submitter.submit(ACTION_ID)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18080",
        "http://localhost:18080",
        "http://10.0.0.1:18080",
        "http://user:password@127.0.0.1:18080",
        "http://127.0.0.1:18080/path",
    ],
)
def test_restate_submitter_rejects_non_loopback_or_malformed_ingress(url) -> None:
    with pytest.raises(ValueError, match="literal HTTP loopback"):
        RestateWorkflowSubmitter(url)


@pytest.mark.asyncio
async def test_restate_submission_rejects_oversized_response() -> None:
    async def request(_url, _body, _headers, _timeout):
        return 202, b"x" * 16_385

    submitter = RestateWorkflowSubmitter("http://127.0.0.1:18080", request=request)

    with pytest.raises(RuntimeError, match="response too large"):
        await submitter.submit(ACTION_ID)


@pytest.mark.asyncio
async def test_restate_workflow_returns_serializable_terminal_outcome(monkeypatch) -> None:
    class Workflow:
        def __init__(self, name, **_kwargs):
            self.name = name
            self.handler = None

        def main(self, **_kwargs):
            def decorate(handler):
                self.handler = handler
                return handler

            return decorate

    class TerminalError(RuntimeError):
        def __init__(self, message, *, status_code):
            super().__init__(message)
            self.status_code = status_code

    fake_restate = SimpleNamespace(
        Workflow=Workflow,
        RunOptions=lambda **kwargs: kwargs,
        TerminalError=TerminalError,
        app=lambda services: services[0],
    )
    monkeypatch.setitem(sys.modules, "restate", fake_restate)
    coordinator = AsyncMock()
    coordinator.advance.return_value = DeliveryResult(
        DeliveryPhase.COMPLETE,
        "provider_receipt_verified",
    )
    workflow = build_restate_app(coordinator)

    class Context:
        def key(self):
            return str(ACTION_ID)

        async def run_typed(self, _name, call, _options):
            return await call()

        async def sleep(self, *_args, **_kwargs):
            raise AssertionError("completed workflow must not sleep")

    with pytest.raises(TerminalError, match="action_id must equal workflow key") as error:
        await workflow.handler(Context(), None)
    assert error.value.status_code == 400
    coordinator.advance.assert_not_awaited()

    result = await workflow.handler(
        Context(),
        {"action_id": str(ACTION_ID)},
    )

    assert result == {
        "phase": "complete",
        "detail_code": "provider_receipt_verified",
        "retry_after_seconds": 0,
    }
