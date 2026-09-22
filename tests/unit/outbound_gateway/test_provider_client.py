# pyright: reportPrivateUsage=false

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from postgres_mcp.outbound_gateway.provider_client import McpCallResult
from postgres_mcp.outbound_gateway.provider_client import McpProviderClient
from postgres_mcp.outbound_gateway.provider_client import McpServerConfig
from postgres_mcp.outbound_gateway.provider_client import ProviderClientError
from postgres_mcp.outbound_gateway.provider_client import TransportErrorKind


@pytest.mark.asyncio
async def test_client_uses_only_configured_server_and_tool_allowlist():
    calls = []

    async def invoke(config, tool, arguments):
        calls.append((config, tool, arguments))
        return McpCallResult(structured_content={"status": "pending", "request_id": "req-1"})

    config = McpServerConfig(
        name="agent-email",
        url="http://127.0.0.1:9090/mcp",
        transport="streamable_http",
        allowed_tools=frozenset({"email_send", "request_status"}),
        timeout_seconds=2.0,
    )
    client = McpProviderClient({config.name: config}, invoker=invoke)

    result = await client.call("agent-email", "email_send", {"to": [{"address": "x@example.com"}]})

    assert result.structured_content == {"status": "pending", "request_id": "req-1"}
    assert calls == [(config, "email_send", {"to": [{"address": "x@example.com"}]})]
    with pytest.raises(ProviderClientError, match="not configured"):
        await client.call("attacker", "email_send", {})
    with pytest.raises(ProviderClientError, match="not allowed"):
        await client.call("agent-email", "upstream_call", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("token=secret"), TransportErrorKind.TIMEOUT),
        (ConnectionError("password=secret"), TransportErrorKind.CONNECTION_LOST),
        (RuntimeError("api_key=secret"), TransportErrorKind.TRANSPORT),
    ],
)
async def test_client_converts_transport_failures_to_sanitized_results(error, kind):
    async def invoke(_config, _tool, _arguments):
        raise error

    config = McpServerConfig(
        name="quo",
        url="http://127.0.0.1:8080/sse",
        transport="sse",
        allowed_tools=frozenset({"send_message"}),
        timeout_seconds=1.0,
    )
    client = McpProviderClient({config.name: config}, invoker=invoke)

    result = await client.call("quo", "send_message", {"content": "hello"})

    assert result.error_kind is kind
    assert result.is_error is True
    assert "secret" not in (result.safe_detail or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_client_classifies_http_auth_rejection_before_provider_call(status_code):
    request = httpx.Request("GET", "http://127.0.0.1:8080/sse")
    response = httpx.Response(status_code, request=request)
    rejection = httpx.HTTPStatusError(
        "sensitive upstream detail",
        request=request,
        response=response,
    )

    class NestedTransportError(RuntimeError):
        def __init__(self, child):
            super().__init__("transport setup failed")
            self.exceptions = (child,)

    async def invoke(_config, _tool, _arguments):
        raise NestedTransportError(rejection)

    config = McpServerConfig(
        name="quo",
        url="http://127.0.0.1:8080/sse",
        transport="sse",
        allowed_tools=frozenset({"send_message"}),
        timeout_seconds=1.0,
    )
    client = McpProviderClient({config.name: config}, invoker=invoke)

    result = await client.call("quo", "send_message", {"content": "hello"})

    assert result.error_kind is TransportErrorKind.AUTH_REJECTED
    assert result.is_error is True
    assert result.safe_detail == "provider_auth_rejected"
    assert "sensitive" not in repr(result)


def test_server_config_rejects_non_loopback_and_invalid_transport():
    base = McpServerConfig(
        name="quo",
        url="http://127.0.0.1:8080/sse",
        transport="sse",
        allowed_tools=frozenset({"send_message"}),
    )
    with pytest.raises(ValueError, match="loopback"):
        replace(base, url="https://example.com/sse")
    with pytest.raises(ValueError, match="transport"):
        replace(base, transport="websocket")


@pytest.mark.asyncio
async def test_streamable_http_transport_receives_secret_headers_without_repr_leak():
    captured = {}

    @asynccontextmanager
    async def transport(url, **kwargs):
        captured.update(url=url, **kwargs)
        yield object(), object(), lambda: None

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                structuredContent={"status": "ok"},
                content=[],
                isError=False,
            )

    config = McpServerConfig(
        name="agent-email",
        url="http://127.0.0.1:9090/mcp",
        transport="streamable_http",
        allowed_tools=frozenset({"email_send"}),
        headers={"Authorization": "Bearer top-secret"},
    )

    with (
        patch(
            "postgres_mcp.outbound_gateway.provider_client.streamablehttp_client",
            transport,
        ),
        patch("postgres_mcp.outbound_gateway.provider_client.ClientSession", Session),
    ):
        result = await McpProviderClient._invoke_mcp(config, "email_send", {})

    assert result.structured_content == {"status": "ok"}
    assert captured["headers"] == {"Authorization": "Bearer top-secret"}
    assert "top-secret" not in repr(config)
