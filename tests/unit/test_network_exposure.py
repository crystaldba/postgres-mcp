import logging
import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from postgres_mcp.network_exposure import is_loopback_host


@pytest.fixture(autouse=True)
def restore_server_state():
    """main() mutates the module-level server; put it back after each test."""
    import postgres_mcp.server as server

    saved = (server.mcp.settings.host, server.mcp.settings.port, server.mcp.settings.transport_security, server.current_access_mode)
    yield
    server.mcp.settings.host, server.mcp.settings.port, server.mcp.settings.transport_security, server.current_access_mode = saved


async def run_main(*args: str) -> None:
    """Run main() with the given CLI arguments, with the database and transports mocked."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = ["postgres-mcp", "postgresql://user:password@localhost/db", *args]
        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
        ):
            await main()
    finally:
        sys.argv = original_argv


def exposure_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "postgres_mcp.server" and r.levelno == logging.WARNING and "no authentication" in r.getMessage()
    ]


HOST_OPTION = {"sse": "--sse-host", "streamable-http": "--streamable-http-host"}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "203.0.113.7", "mcp.example.com", "localhost.example.com"])
async def test_warns_once_for_non_loopback_bind(caplog, transport, host):
    caplog.set_level(logging.WARNING)
    await run_main(f"--transport={transport}", f"{HOST_OPTION[transport]}={host}")

    warnings = exposure_warnings(caplog)
    assert len(warnings) == 1
    assert transport in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1", "[::1]"])
async def test_no_warning_for_loopback_bind(caplog, transport, host):
    caplog.set_level(logging.WARNING)
    await run_main(f"--transport={transport}", f"{HOST_OPTION[transport]}={host}")

    assert exposure_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [(), ("--transport=sse",), ("--transport=streamable-http",), ("--transport=stdio",)])
async def test_no_warning_by_default(caplog, args):
    caplog.set_level(logging.WARNING)
    await run_main(*args)

    assert exposure_warnings(caplog) == []


@pytest.mark.asyncio
async def test_no_warning_for_stdio_even_with_non_loopback_sse_host(caplog):
    caplog.set_level(logging.WARNING)
    await run_main("--transport=stdio", "--sse-host=0.0.0.0")

    assert exposure_warnings(caplog) == []


@pytest.mark.asyncio
async def test_warning_content_unrestricted(caplog):
    caplog.set_level(logging.WARNING)
    await run_main("--transport=sse", "--sse-host=0.0.0.0", "--sse-port=8123", "--access-mode=unrestricted")

    (warning,) = exposure_warnings(caplog)
    assert "0.0.0.0:8123" in warning
    assert "arbitrary SQL" in warning
    assert "read-only" not in warning
    assert "-p 127.0.0.1:<port>:<port>" in warning
    assert "reverse proxy" in warning
    assert "VPN" in warning


@pytest.mark.asyncio
async def test_warning_content_restricted(caplog):
    caplog.set_level(logging.WARNING)
    await run_main("--transport=streamable-http", "--streamable-http-host=::", "--access-mode=restricted")

    (warning,) = exposure_warnings(caplog)
    assert "[::]:8000" in warning
    assert "read-only SQL" in warning
    assert "arbitrary SQL" not in warning


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("Localhost", True),
        ("127.0.0.1", True),
        ("127.255.255.254", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("[::]", False),
        ("", False),
        ("10.0.0.1", False),
        ("localhost.example.com", False),
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected
