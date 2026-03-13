import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

_TRANSPORT_MOCK_MAP = {
    "sse": "postgres_mcp.server.mcp.run_sse_async",
    "streamable-http": "postgres_mcp.server.mcp.run_streamable_http_async",
}

_MCP_ENV_KEYS = [
    "MCP_ENABLE_DNS_REBINDING_PROTECTION",
    "MCP_ALLOWED_HOSTS",
    "MCP_ALLOWED_ORIGINS",
]


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
class TestTransportSecurityIntegration:
    @pytest.fixture(autouse=True)
    def _preserve_mcp_state(self, monkeypatch: pytest.MonkeyPatch):
        from postgres_mcp.server import mcp

        original_argv = sys.argv
        original_security = mcp.settings.transport_security
        for key in _MCP_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        yield
        sys.argv = original_argv
        mcp.settings.transport_security = original_security

    @pytest.mark.asyncio
    async def test_disable_dns_rebinding_via_cli_flag(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--disable-dns-rebinding-protection",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert mcp.settings.transport_security.enable_dns_rebinding_protection is False

    @pytest.mark.asyncio
    async def test_disable_dns_rebinding_via_env(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
            patch.dict("os.environ", {"MCP_ENABLE_DNS_REBINDING_PROTECTION": "false"}),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert mcp.settings.transport_security.enable_dns_rebinding_protection is False

    @pytest.mark.asyncio
    async def test_allowed_hosts_via_cli(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--allowed-hosts",
            "localhost:*,127.0.0.1:*",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert "localhost:*" in mcp.settings.transport_security.allowed_hosts
            assert "127.0.0.1:*" in mcp.settings.transport_security.allowed_hosts

    @pytest.mark.asyncio
    async def test_allowed_hosts_env_overrides_cli(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--allowed-hosts",
            "cli-host:*",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
            patch.dict("os.environ", {"MCP_ALLOWED_HOSTS": "env-host:*"}),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert "env-host:*" in mcp.settings.transport_security.allowed_hosts
            assert "cli-host:*" not in mcp.settings.transport_security.allowed_hosts

    @pytest.mark.asyncio
    async def test_allowed_origins_via_cli(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--allowed-origins",
            "http://localhost:*",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert "http://localhost:*" in mcp.settings.transport_security.allowed_origins

    @pytest.mark.asyncio
    async def test_allowed_origins_env_overrides_cli(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--allowed-origins",
            "http://cli-origin:*",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
            patch.dict("os.environ", {"MCP_ALLOWED_ORIGINS": "http://env-origin:*"}),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert "http://env-origin:*" in mcp.settings.transport_security.allowed_origins
            assert "http://cli-origin:*" not in mcp.settings.transport_security.allowed_origins

    @pytest.mark.asyncio
    async def test_env_protection_true_overrides_cli_disable(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
            "--disable-dns-rebinding-protection",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
            patch.dict("os.environ", {"MCP_ENABLE_DNS_REBINDING_PROTECTION": "true"}),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert mcp.settings.transport_security.enable_dns_rebinding_protection is True

    @pytest.mark.asyncio
    async def test_default_defers_to_fastmcp(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert mcp.settings.transport_security.enable_dns_rebinding_protection is True

    @pytest.mark.asyncio
    async def test_database_url_after_flags_not_consumed(self, transport: str):
        from postgres_mcp.server import main
        from postgres_mcp.server import mcp

        sys.argv = [
            "postgres_mcp",
            f"--transport={transport}",
            "--allowed-hosts",
            "localhost:*,my-gateway:8080",
            "--allowed-origins",
            "http://localhost:*,http://my-gateway:*",
            "postgresql://user:password@localhost/db",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch(_TRANSPORT_MOCK_MAP[transport], AsyncMock()),
        ):
            await main()
            assert mcp.settings.transport_security is not None
            assert "localhost:*" in mcp.settings.transport_security.allowed_hosts
            assert "my-gateway:8080" in mcp.settings.transport_security.allowed_hosts
