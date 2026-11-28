"""The Host header allowlist installed by main(), checked with the MCP SDK's own middleware."""

import argparse
import logging
import re
import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from postgres_mcp.allowed_hosts import DEFAULT_ALLOWED_HOSTS
from postgres_mcp.allowed_hosts import DEFAULT_ALLOWED_ORIGINS
from postgres_mcp.allowed_hosts import parse_allowed_hosts

NETWORK_TRANSPORTS = ["sse", "streamable-http"]


@pytest.fixture(autouse=True)
def restore_server_state():
    """main() mutates the module-level server; put it back after each test."""
    import postgres_mcp.server as server

    saved = (server.mcp.settings.host, server.mcp.settings.port, server.mcp.settings.transport_security, server.current_access_mode)
    yield
    server.mcp.settings.host, server.mcp.settings.port, server.mcp.settings.transport_security, server.current_access_mode = saved


async def run_main(*args: str) -> dict[str, AsyncMock]:
    """Run main() with the given CLI arguments, with the database and transports mocked."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = ["postgres-mcp", "postgresql://user:password@localhost/db", *args]
        mocks = {name: AsyncMock() for name in ("stdio", "sse", "streamable-http")}
        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", mocks["stdio"]),
            patch("postgres_mcp.server.mcp.run_sse_async", mocks["sse"]),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", mocks["streamable-http"]),
        ):
            await main()
        return mocks
    finally:
        sys.argv = original_argv


async def installed_settings(transport: str, *args: str) -> TransportSecuritySettings:
    """Run main() for a network transport and return the settings it installed before starting it."""
    from postgres_mcp.server import mcp

    mcp.settings.transport_security = None
    mocks = await run_main(f"--transport={transport}", *args)
    mocks[transport].assert_called_once()
    settings = mcp.settings.transport_security
    assert settings is not None
    return settings


async def status(settings: TransportSecuritySettings, host: str | None, origin: str | None = None, post: bool = False) -> int:
    """Return the status the SDK middleware gives a request (200 if it lets it through)."""
    headers = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if post:
        headers.append((b"content-type", b"application/json"))
    request = Request({"type": "http", "method": "POST" if post else "GET", "path": "/", "headers": headers})
    response = await TransportSecurityMiddleware(settings).validate_request(request, is_post=post)
    return 200 if response is None else response.status_code


def test_defaults_match_what_the_sdk_installs_at_import():
    settings = FastMCP("check").settings.transport_security
    assert settings is not None
    assert settings.allowed_hosts == list(DEFAULT_ALLOWED_HOSTS)
    assert settings.allowed_origins == list(DEFAULT_ALLOWED_ORIGINS)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost:8000", 200),
        ("127.0.0.1:8000", 200),
        ("[::1]:8000", 200),
        ("localhost", 421),
        ("LOCALHOST:8000", 421),
        ("host.docker.internal:8000", 421),
        ("evil.example.com:8000", 421),
        ("0.0.0.0:8000", 421),
        (None, 421),
    ],
)
async def test_without_option_only_localhost_with_port_is_accepted(transport, host, expected):
    settings = await installed_settings(transport)
    assert await status(settings, host) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
@pytest.mark.parametrize(
    "host,expected",
    [
        ("host.docker.internal", 200),
        ("host.docker.internal:8000", 200),
        ("mcp.example.com", 200),
        ("mcp.example.com:443", 200),
        ("localhost:8000", 200),
        ("127.0.0.1:8000", 200),
        ("[::1]:8000", 200),
        ("evil.example.com", 421),
        ("evil.example.com:8000", 421),
        ("mcp.example.com.evil.com:8000", 421),
        ("sub.mcp.example.com", 421),
        (None, 421),
        ("", 421),
    ],
)
async def test_allowed_hosts_adds_names_with_and_without_port(transport, host, expected):
    settings = await installed_settings(transport, "--allowed-hosts=host.docker.internal,mcp.example.com")
    assert await status(settings, host) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option,host,expected",
    [
        ("name:8000", "name:8000", 200),
        ("name:8000", "name:8001", 421),
        ("name:8000", "name", 421),
        ("name:*", "name:8000", 200),
        ("name:*", "name:1", 200),
        ("name:*", "name", 421),
        ("[fe80::1]:8000", "[fe80::1]:8000", 200),
        ("[fe80::1]:8000", "[fe80::1]:9000", 421),
        ("[fe80::1]", "[fe80::1]", 200),
        ("[fe80::1]", "[fe80::1]:9000", 200),
        ("10.0.0.5", "10.0.0.5:8000", 200),
        ("MCP.Example.COM", "mcp.example.com", 200),
        ("compose_service", "compose_service:8000", 200),
    ],
)
async def test_entry_forms(option, host, expected):
    settings = await installed_settings("sse", f"--allowed-hosts={option}")
    assert await status(settings, host) == expected


@pytest.mark.asyncio
async def test_option_can_be_repeated():
    settings = await installed_settings("sse", "--allowed-hosts=a.example.com", "--allowed-hosts", "b.example.com:9000")
    assert settings.allowed_hosts == [*DEFAULT_ALLOWED_HOSTS, "a.example.com", "a.example.com:*", "b.example.com:9000"]
    assert await status(settings, "a.example.com:1234") == 200
    assert await status(settings, "b.example.com:9000") == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
async def test_origins_are_unchanged(transport):
    settings = await installed_settings(transport, "--allowed-hosts=mcp.example.com")
    assert settings.allowed_origins == list(DEFAULT_ALLOWED_ORIGINS)
    assert await status(settings, "mcp.example.com", origin="http://localhost:3000") == 200
    assert await status(settings, "mcp.example.com", origin="http://mcp.example.com") == 403
    assert await status(settings, "mcp.example.com", post=True) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["", ",", "*", "*.example.com", "http://x", "x/path", "a b", "a,,b", "a,", "::1", "[::1", "[nope]", "x:0", "x:65536", "x:80a", "x:"]
)
async def test_invalid_values_exit_with_usage_error(capsys, value):
    from postgres_mcp.server import mcp

    before = mcp.settings.transport_security
    with pytest.raises(SystemExit) as exc:
        await run_main("--transport=sse", f"--allowed-hosts={value}")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--allowed-hosts" in err
    assert "invalid entry" in err
    assert mcp.settings.transport_security is before


@pytest.mark.parametrize(
    "value,entry",
    [("", "''"), ("ok.example.com,*", "'*'"), ("a,*.example.com", "'*.example.com'"), ("http://x", "'http://x'"), ("a b", "'a b'")],
)
def test_error_names_the_bad_entry(value, entry):
    with pytest.raises(argparse.ArgumentTypeError, match=re.escape(f"invalid entry {entry}")):
        parse_allowed_hosts(value)


@pytest.mark.asyncio
async def test_stdio_ignores_option_with_one_warning(caplog):
    caplog.set_level(logging.WARNING)
    mocks = await run_main("--transport=stdio", "--allowed-hosts=mcp.example.com")
    mocks["stdio"].assert_called_once()
    warnings = [r.getMessage() for r in caplog.records if r.name == "postgres_mcp.server" and r.levelno == logging.WARNING]
    assert len([w for w in warnings if "--allowed-hosts" in w]) == 1


@pytest.mark.asyncio
async def test_stdio_without_option_does_not_warn(caplog):
    caplog.set_level(logging.WARNING)
    await run_main("--transport=stdio")
    assert not [r for r in caplog.records if "--allowed-hosts" in r.getMessage()]


@pytest.mark.asyncio
async def test_logs_accepted_host_headers(caplog):
    caplog.set_level(logging.INFO)
    await installed_settings("streamable-http", "--allowed-hosts=mcp.example.com")
    assert "Accepting Host headers: 127.0.0.1:*, localhost:*, [::1]:*, mcp.example.com, mcp.example.com:*" in caplog.text


@pytest.mark.asyncio
async def test_exposure_warning_still_fires_with_allowed_hosts(caplog):
    caplog.set_level(logging.WARNING)
    await installed_settings("sse", "--sse-host=0.0.0.0", "--allowed-hosts=mcp.example.com")
    assert len([r for r in caplog.records if "no authentication" in r.getMessage()]) == 1
