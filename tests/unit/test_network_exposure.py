import logging
import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest


@pytest.fixture
def restore_security():
    from postgres_mcp.server import mcp

    saved = mcp.settings.transport_security
    saved_host, saved_port = mcp.settings.host, mcp.settings.port
    original_argv = sys.argv
    yield mcp
    mcp.settings.transport_security = saved
    mcp.settings.host, mcp.settings.port = saved_host, saved_port
    sys.argv = original_argv


async def run_main(*args: str):
    from postgres_mcp.server import main

    sys.argv = ["postgres_mcp", "postgresql://user:password@localhost/db", *args]
    with (
        patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
        patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()),
        patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()),
        patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
    ):
        await main()


# --- warning when an unauthenticated transport is network-exposed ---


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.5", False),
        ("10.0.0.5", False),
        ("myhost.example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host, expected):
    from postgres_mcp.network_exposure import is_loopback_host

    assert is_loopback_host(host) is expected


@pytest.mark.parametrize("flag", ["--sse-host", "--streamable-http-host"])
@pytest.mark.asyncio
async def test_main_warns_when_bound_beyond_loopback(restore_security, caplog, flag):
    transport = "sse" if flag == "--sse-host" else "streamable-http"
    with caplog.at_level(logging.WARNING):
        await run_main(f"--transport={transport}", f"{flag}=0.0.0.0", "--access-mode=unrestricted")
    messages = [r.message for r in caplog.records if "has no authentication" in r.message]
    assert len(messages) == 1
    assert "0.0.0.0" in messages[0]
    assert "arbitrary SQL" in messages[0]
    assert "127.0.0.1:8000:8000" in messages[0]


@pytest.mark.asyncio
async def test_main_warning_describes_restricted_mode(restore_security, caplog):
    with caplog.at_level(logging.WARNING):
        await run_main("--transport=sse", "--sse-host=0.0.0.0", "--access-mode=restricted")
    messages = [r.message for r in caplog.records if "has no authentication" in r.message]
    assert len(messages) == 1
    assert "read-only SQL" in messages[0]
    assert "arbitrary SQL" not in messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_main_no_warning_on_loopback_default(restore_security, caplog, transport):
    with caplog.at_level(logging.WARNING):
        await run_main(f"--transport={transport}")
    assert not any("has no authentication" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_main_no_warning_for_stdio(restore_security, caplog):
    with caplog.at_level(logging.WARNING):
        await run_main("--transport=stdio")
    assert not any("has no authentication" in r.message for r in caplog.records)
