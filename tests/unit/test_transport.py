import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
async def test_transport_argument_parsing(transport):
    """Test that all transport options are parsed correctly."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()) as mock_stdio,
            patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()) as mock_sse,
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()) as mock_http,
        ):
            await main()

            # Verify the correct transport method was called
            if transport == "stdio":
                mock_stdio.assert_called_once()
                mock_sse.assert_not_called()
                mock_http.assert_not_called()
            elif transport == "sse":
                mock_stdio.assert_not_called()
                mock_sse.assert_called_once()
                mock_http.assert_not_called()
            elif transport == "streamable-http":
                mock_stdio.assert_not_called()
                mock_sse.assert_not_called()
                mock_http.assert_called_once()
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_streamable_http_host_port_arguments():
    """Test that streamable-http host and port arguments are applied correctly."""
    from postgres_mcp.server import main
    from postgres_mcp.server import mcp

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=streamable-http",
            "--streamable-http-host=0.0.0.0",
            "--streamable-http-port=9000",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
        ):
            await main()

            # Verify the host and port were set correctly
            assert mcp.settings.host == "0.0.0.0"
            assert mcp.settings.port == 9000
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_sse_host_port_arguments():
    """Test that SSE host and port arguments are applied correctly."""
    from postgres_mcp.server import main
    from postgres_mcp.server import mcp

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=sse",
            "--sse-host=0.0.0.0",
            "--sse-port=8080",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()),
        ):
            await main()

            # Verify the host and port were set correctly
            assert mcp.settings.host == "0.0.0.0"
            assert mcp.settings.port == 8080
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_default_transport_is_stdio():
    """Test that the default transport is stdio when not specified."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()) as mock_stdio,
            patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()) as mock_sse,
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()) as mock_http,
        ):
            await main()

            mock_stdio.assert_called_once()
            mock_sse.assert_not_called()
            mock_http.assert_not_called()
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_stateless_http_argument_sets_setting():
    """Test that --stateless-http enables FastMCP's stateless_http setting."""
    from postgres_mcp.server import main
    from postgres_mcp.server import mcp

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=streamable-http",
            "--stateless-http",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
        ):
            await main()

            assert mcp.settings.stateless_http is True
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_stateless_http_defaults_to_stateful():
    """Test that streamable-http stays stateful when --stateless-http is not passed."""
    from postgres_mcp.server import main
    from postgres_mcp.server import mcp

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=streamable-http",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
        ):
            await main()

            assert mcp.settings.stateless_http is False
    finally:
        sys.argv = original_argv


# The two tests below pin the SDK behavior --stateless-http exists for, so an mcp
# SDK bump that changes stateless streamable-HTTP semantics fails here instead of
# resurfacing in production as a memory leak: in stateful mode the SDK retains
# per-session state until the client sends DELETE /mcp, so clients that never do
# (most MCP connectors) grow the server's memory without bound. They use fresh
# FastMCP instances rather than postgres_mcp.server's, because a FastMCP instance
# builds its session manager once and the manager can only run once.


def _streamable_http_client(server):
    """TestClient for the server's streamable-HTTP app.

    The base_url matters: the SDK's DNS-rebinding protection only accepts
    localhost Host headers by default, and TestClient's default Host
    (testserver) is rejected with 421.
    """
    from starlette.testclient import TestClient

    return TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:8000")


def _sessionless_tools_list(client):
    """POST tools/list with no Mcp-Session-Id header and no prior initialize."""
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )


def test_stateless_streamable_http_serves_sessionless_requests():
    """Test that stateless mode serves requests that carry no session."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("stateless-contract-test", stateless_http=True)
    with _streamable_http_client(server) as client:
        response = _sessionless_tools_list(client)

        assert response.status_code == 200


def test_stateful_streamable_http_rejects_sessionless_requests():
    """Test that stateful mode rejects the same sessionless request.

    Guards the contrast with the stateless test above: if this ever starts
    passing requests through, the stateless test no longer proves the flag
    changes behavior.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("stateful-contract-test", stateless_http=False)
    with _streamable_http_client(server) as client:
        response = _sessionless_tools_list(client)

        assert response.status_code == 400
        assert "session" in response.text.lower()
