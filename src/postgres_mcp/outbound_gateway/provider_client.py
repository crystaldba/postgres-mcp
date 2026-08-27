"""Allowlisted MCP transports for outbound provider adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta
from enum import StrEnum
from typing import Any
from typing import Literal
from typing import Mapping
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent


class ProviderClientError(ValueError):
    """Configuration or allowlist violation before provider I/O."""


class TransportErrorKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION_LOST = "connection_lost"
    AUTH_REJECTED = "auth_rejected"
    TRANSPORT = "transport"


@dataclass(frozen=True)
class McpCallResult:
    structured_content: dict[str, Any] | None = None
    text: str | None = None
    is_error: bool = False
    error_kind: TransportErrorKind | None = None
    safe_detail: str | None = None


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    url: str
    transport: Literal["streamable_http", "sse"]
    allowed_tools: frozenset[str]
    timeout_seconds: float = 10.0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("provider MCP endpoint must use loopback")
        if self.transport not in {"streamable_http", "sse"}:
            raise ValueError("unsupported provider MCP transport")
        if not self.name or not self.allowed_tools:
            raise ValueError("provider MCP name and tool allowlist are required")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("provider MCP timeout must be between 0 and 30 seconds")
        if any(not key.strip() or "\n" in key or "\r" in key or "\n" in value or "\r" in value for key, value in self.headers.items()):
            raise ValueError("provider MCP headers must be non-empty single-line strings")


Invoker = Callable[[McpServerConfig, str, dict[str, Any]], Awaitable[McpCallResult]]


class McpProviderClient:
    """Calls only configured loopback MCP servers and allowlisted tools."""

    def __init__(
        self,
        servers: dict[str, McpServerConfig],
        *,
        invoker: Invoker | None = None,
    ):
        self._servers = dict(servers)
        self._invoker = invoker or self._invoke_mcp

    async def call(self, server_name: str, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        config = self._servers.get(server_name)
        if config is None:
            raise ProviderClientError(f"provider MCP server {server_name!r} is not configured")
        if tool not in config.allowed_tools:
            raise ProviderClientError(f"provider MCP tool {tool!r} is not allowed for {server_name!r}")
        try:
            return await asyncio.wait_for(
                self._invoker(config, tool, dict(arguments)),
                timeout=config.timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return McpCallResult(
                is_error=True,
                error_kind=TransportErrorKind.TIMEOUT,
                safe_detail="provider_transport_timeout",
            )
        except (ConnectionError, BrokenPipeError, EOFError):
            return McpCallResult(
                is_error=True,
                error_kind=TransportErrorKind.CONNECTION_LOST,
                safe_detail="provider_connection_lost",
            )
        except Exception as exc:
            if _contains_http_auth_rejection(exc):
                return McpCallResult(
                    is_error=True,
                    error_kind=TransportErrorKind.AUTH_REJECTED,
                    safe_detail="provider_auth_rejected",
                )
            return McpCallResult(
                is_error=True,
                error_kind=TransportErrorKind.TRANSPORT,
                safe_detail="provider_transport_error",
            )

    @staticmethod
    async def _invoke_mcp(config: McpServerConfig, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        timeout = timedelta(seconds=config.timeout_seconds)
        transport = streamablehttp_client if config.transport == "streamable_http" else sse_client
        async with transport(
            config.url,
            headers=dict(config.headers) or None,
            timeout=config.timeout_seconds,
            sse_read_timeout=config.timeout_seconds,
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=timeout) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments, read_timeout_seconds=timeout)
        structured = result.structuredContent if isinstance(result.structuredContent, dict) else None
        text_parts = [item.text for item in result.content if isinstance(item, TextContent)]
        text = "\n".join(text_parts) if text_parts else None
        if structured is None and text:
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                structured = decoded
        return McpCallResult(
            structured_content=structured,
            text=text,
            is_error=bool(result.isError),
            safe_detail="provider_mcp_error" if result.isError else None,
        )


def _contains_http_auth_rejection(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) in {401, 403}:
        return True
    return any(
        _contains_http_auth_rejection(child)
        for child in getattr(error, "exceptions", ())
        if isinstance(child, BaseException)
    )
