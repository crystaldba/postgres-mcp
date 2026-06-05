"""Tests for the stdio connection-pool lifecycle: the orphan watchdog and the
guarantee that the pool is released when the server exits.

These cover the behavior added to stop the server leaking connections against
the database role when its MCP client is force-killed without closing stdin —
see exit_if_orphaned() and main() in postgres_mcp.server.
"""

import asyncio
import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_exit_if_orphaned_closes_pool_before_exiting():
    """Once orphaned (getppid()==1) the watchdog must close the pool and THEN
    hard-exit — closing first is what returns the connections cleanly."""
    from postgres_mcp import server

    order = []
    close_mock = AsyncMock(side_effect=lambda: order.append("close"))
    exit_mock = MagicMock(side_effect=lambda code: order.append(("exit", code)))

    with (
        patch("postgres_mcp.server.os.getppid", return_value=1),
        patch("postgres_mcp.server.db_connection.close", close_mock),
        patch("postgres_mcp.server.os._exit", exit_mock),
    ):
        await server.exit_if_orphaned(poll_interval=0.001)

    assert order == ["close", ("exit", 0)]


@pytest.mark.asyncio
async def test_exit_if_orphaned_waits_until_orphaned():
    """While the parent is alive (getppid()!=1) the watchdog keeps polling and
    must NOT close the pool or exit."""
    from postgres_mcp import server

    ppids = iter([1234, 1234, 1])  # alive twice, then reparented to init

    def fake_getppid():
        return next(ppids)

    close_mock = AsyncMock()
    exit_mock = MagicMock()

    with (
        patch("postgres_mcp.server.os.getppid", side_effect=fake_getppid),
        patch("postgres_mcp.server.db_connection.close", close_mock),
        patch("postgres_mcp.server.os._exit", exit_mock),
    ):
        await server.exit_if_orphaned(poll_interval=0.001)

    close_mock.assert_awaited_once()
    exit_mock.assert_called_once_with(0)


async def _run_main_capturing_tasks(transport, created):
    """Run main() for the given transport, recording every task spawned via
    asyncio.ensure_future so tests can assert whether the watchdog was started."""
    from postgres_mcp.server import main

    real_ensure_future = asyncio.ensure_future

    def spy(coro, *args, **kwargs):
        task = real_ensure_future(coro, *args, **kwargs)
        created.append(task)
        return task

    original_argv = sys.argv
    sys.argv = ["postgres_mcp", "postgresql://user:password@localhost/db", f"--transport={transport}"]
    try:
        with (
            # never actually orphaned during the test
            patch("postgres_mcp.server.os.getppid", return_value=99999),
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.db_connection.close", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_sse_async", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_streamable_http_async", AsyncMock()),
            patch("postgres_mcp.server.asyncio.ensure_future", side_effect=spy),
        ):
            await main()
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_stdio_starts_watchdog_and_cancels_it_on_clean_exit():
    """stdio must start exactly one watchdog task and cancel it when the
    transport returns cleanly (stdin EOF), leaving no task running."""
    created = []
    await _run_main_capturing_tasks("stdio", created)

    assert len(created) == 1, "stdio should start exactly one watchdog task"
    with pytest.raises(asyncio.CancelledError):
        await created[0]  # deterministically confirms it was cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_non_stdio_transports_do_not_start_watchdog(transport):
    """The orphan watchdog is stdio-only; long-running HTTP transports manage
    their own lifecycle and must not spawn it."""
    created = []
    await _run_main_capturing_tasks(transport, created)

    assert created == [], f"{transport} must not start the orphan watchdog"
