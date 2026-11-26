import logging
import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from postgres_mcp.transport_security import DEFAULT_ALLOWED_HOSTS
from postgres_mcp.transport_security import DEFAULT_ALLOWED_ORIGINS
from postgres_mcp.transport_security import build_transport_security
from postgres_mcp.transport_security import parse_allowed_hosts


def default_settings() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(DEFAULT_ALLOWED_HOSTS),
        allowed_origins=list(DEFAULT_ALLOWED_ORIGINS),
    )


# --- parse_allowed_hosts ---


@pytest.mark.parametrize(
    "value,expected",
    [
        ("myapp.example.com", ["myapp.example.com", "myapp.example.com:*"]),
        ("myapp.example.com:9000", ["myapp.example.com:9000"]),
        ("myapp.example.com:*", ["myapp.example.com:*"]),
        ("[::1]", ["[::1]", "[::1]:*"]),
        ("[fe80::1]:8000", ["[fe80::1]:8000"]),
        ("10.0.0.5", ["10.0.0.5", "10.0.0.5:*"]),
        ("MyApp.Example.COM", ["myapp.example.com", "myapp.example.com:*"]),
        ("  a ,, b:81 , a ", ["a", "a:*", "b:81"]),
        (
            "host.docker.internal,postgres-mcp-server",
            ["host.docker.internal", "host.docker.internal:*", "postgres-mcp-server", "postgres-mcp-server:*"],
        ),
    ],
)
def test_parse_allowed_hosts_valid(value, expected):
    assert parse_allowed_hosts(value) == expected


@pytest.mark.parametrize("value", ["", ",", "http://x", "x/path", "*", "*.example.com", "a b", "user@host", "x:*:*"])
def test_parse_allowed_hosts_invalid(value):
    with pytest.raises(ValueError):
        parse_allowed_hosts(value)


def test_parse_allowed_hosts_error_names_entry():
    with pytest.raises(ValueError, match=r"http://x"):
        parse_allowed_hosts("ok.example.com,http://x")


# --- build_transport_security ---


def test_build_unchanged_when_nothing_given():
    current = default_settings()
    assert build_transport_security(current, None, False) is current
    assert build_transport_security(None, None, False) is None


def test_build_adds_to_defaults_and_keeps_origins():
    result = build_transport_security(default_settings(), "myapp.example.com", False)
    assert result is not None
    assert result.enable_dns_rebinding_protection is True
    assert result.allowed_hosts == [*DEFAULT_ALLOWED_HOSTS, "myapp.example.com", "myapp.example.com:*"]
    assert result.allowed_origins == DEFAULT_ALLOWED_ORIGINS


def test_build_falls_back_to_defaults_when_current_missing_or_disabled():
    for current in (None, TransportSecuritySettings(enable_dns_rebinding_protection=False)):
        result = build_transport_security(current, "x.example.com", False)
        assert result is not None
        assert result.enable_dns_rebinding_protection is True
        assert result.allowed_hosts[:3] == DEFAULT_ALLOWED_HOSTS
        assert result.allowed_origins == DEFAULT_ALLOWED_ORIGINS


def test_build_disable_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="postgres_mcp.transport_security"):
        result = build_transport_security(default_settings(), None, True)
    assert result is not None
    assert result.enable_dns_rebinding_protection is False
    assert any("DISABLED" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_build_disable_and_hosts_is_error():
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_transport_security(default_settings(), "x.example.com", True)


def test_build_invalid_hosts_is_error():
    with pytest.raises(ValueError):
        build_transport_security(default_settings(), "*", False)


# --- enforcement through the SDK middleware ---


def make_request(host: str | None, origin: str | None = None, post: bool = False) -> Request:
    headers = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if post:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "method": "POST" if post else "GET",
        "path": "/mcp" if post else "/sse",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope)


async def status(settings, host, origin=None, post=False):
    """Return None if accepted, else the rejection status code."""
    response = await TransportSecurityMiddleware(settings).validate_request(make_request(host, origin, post), is_post=post)
    return None if response is None else response.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("post", [False, True])
async def test_default_settings_enforcement(post):
    s = default_settings()
    assert await status(s, "localhost:8000", post=post) is None
    assert await status(s, "127.0.0.1:8000", post=post) is None
    assert await status(s, "[::1]:8000", post=post) is None
    for bad in ("evil.example.com:8000", "0.0.0.0:8000", "host.docker.internal:8000"):
        assert await status(s, bad, post=post) == 421


@pytest.mark.asyncio
@pytest.mark.parametrize("post", [False, True])
async def test_allowed_hosts_enforcement(post):
    s = build_transport_security(default_settings(), "host.docker.internal,myapp.example.com", False)
    assert s is not None
    for ok in (
        "host.docker.internal",
        "host.docker.internal:8000",
        "myapp.example.com",
        "myapp.example.com:8000",
        "localhost:8000",
        "127.0.0.1:8000",
    ):
        assert await status(s, ok, post=post) is None, ok
    assert await status(s, "evil.example.com:8000", post=post) == 421
    assert await status(s, "evil.example.com", post=post) == 421
    assert await status(s, "0.0.0.0:8000", post=post) == 421
    assert await status(s, "myapp.example.com.evil.com:8000", post=post) == 421
    assert await status(s, None, post=post) == 421
    # Origin checks stay at the localhost defaults
    assert await status(s, "myapp.example.com:8000", origin="http://evil.example.com", post=post) == 403
    assert await status(s, "myapp.example.com:8000", origin="http://myapp.example.com", post=post) == 403
    assert await status(s, "myapp.example.com:8000", origin="http://localhost:3000", post=post) is None
    assert await status(s, "myapp.example.com:8000", origin=None, post=post) is None


@pytest.mark.asyncio
async def test_disabled_enforcement_accepts_everything():
    s = build_transport_security(default_settings(), None, True)
    assert s is not None
    assert await status(s, "evil.example.com:8000", origin="http://evil.example.com") is None
    assert await status(s, "0.0.0.0:8000", post=True) is None
    assert await status(s, None) is None


# --- main() wiring ---


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


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_main_allowed_hosts(restore_security, transport):
    mcp = restore_security
    await run_main(f"--transport={transport}", "--allowed-hosts=myapp.example.com")
    ts = mcp.settings.transport_security
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    assert "myapp.example.com" in ts.allowed_hosts
    assert "myapp.example.com:*" in ts.allowed_hosts
    for h in DEFAULT_ALLOWED_HOSTS:
        assert h in ts.allowed_hosts
    assert ts.allowed_origins == DEFAULT_ALLOWED_ORIGINS


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_main_disable_protection(restore_security, transport):
    mcp = restore_security
    await run_main(f"--transport={transport}", "--disable-dns-rebinding-protection")
    assert mcp.settings.transport_security is not None
    assert mcp.settings.transport_security.enable_dns_rebinding_protection is False


@pytest.mark.asyncio
async def test_main_no_flags_leaves_defaults(restore_security):
    mcp = restore_security
    before = mcp.settings.transport_security
    await run_main("--transport=sse")
    assert mcp.settings.transport_security == before


@pytest.mark.asyncio
async def test_main_stdio_ignores_flags(restore_security, caplog):
    mcp = restore_security
    before = mcp.settings.transport_security
    with caplog.at_level(logging.WARNING):
        await run_main("--transport=stdio", "--allowed-hosts=myapp.example.com")
        await run_main("--transport=stdio", "--disable-dns-rebinding-protection")
    assert mcp.settings.transport_security == before
    assert any("only apply" in r.message for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["--allowed-hosts=http://x", "--allowed-hosts=*", "--allowed-hosts=,"])
async def test_main_invalid_entry_exits_2(restore_security, bad):
    mcp = restore_security
    before = mcp.settings.transport_security
    with pytest.raises(SystemExit) as exc:
        await run_main("--transport=sse", bad)
    assert exc.value.code == 2
    assert mcp.settings.transport_security == before


@pytest.mark.asyncio
async def test_main_mutually_exclusive_exits_2(restore_security):
    with pytest.raises(SystemExit) as exc:
        await run_main("--transport=sse", "--allowed-hosts=x.example.com", "--disable-dns-rebinding-protection")
    assert exc.value.code == 2


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
    from postgres_mcp.transport_security import is_loopback_host

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
