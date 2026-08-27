"""Regression tests for pooled connection session-state reset (issue #203)."""

import os
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from psycopg import sql

from postgres_mcp.sql.sql_driver import DbConnPool
from postgres_mcp.sql.sql_driver import SqlDriver
from postgres_mcp.sql.sql_driver import reset_pooled_connection

DATABASE_URI = os.environ.get("DATABASE_URI")


class _CursorCM:
    async def __aenter__(self):
        class _Cursor:
            async def execute(self, query: object) -> None:
                return None

        return _Cursor()

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _ConnectionCM:
    async def __aenter__(self):
        class _Connection:
            def cursor(self):
                return _CursorCM()

        return _Connection()

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _query_text(query: object) -> str:
    if isinstance(query, str):
        return query
    return repr(query)


def _mock_connection(*, hypopg_schema: str | None = "public") -> tuple[MagicMock, list[object]]:
    conn = MagicMock()
    conn.autocommit = False
    conn.set_autocommit = AsyncMock()
    calls: list[object] = []

    async def execute(query: object, *args: Any, **kwargs: Any) -> AsyncMock:
        calls.append(query)
        text = _query_text(query)
        cursor = AsyncMock()
        if "pg_extension" in text:
            cursor.fetchone = AsyncMock(return_value=(hypopg_schema,) if hypopg_schema else None)
        else:
            cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    conn.execute = AsyncMock(side_effect=execute)
    return conn, calls


@pytest.mark.asyncio
async def test_pool_connect_wires_reset_callback():
    captured: dict[str, Any] = {}
    mock_pool = MagicMock()
    mock_pool.open = AsyncMock()
    mock_pool.close = AsyncMock()

    mock_pool.connection = MagicMock(return_value=_ConnectionCM())

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return mock_pool

    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", side_effect=factory):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")
        await db_pool.pool_connect()

    assert captured["kwargs"]["reset"] is reset_pooled_connection
    await db_pool.close()


@pytest.mark.asyncio
async def test_reset_runs_discard_all_outside_transaction():
    conn, calls = _mock_connection(hypopg_schema=None)

    await reset_pooled_connection(conn)

    assert conn.set_autocommit.await_args_list[0].args == (True,)
    assert conn.set_autocommit.await_args_list[-1].args == (False,)
    assert any(isinstance(q, str) and q.strip().upper() == "DISCARD ALL" for q in calls)
    autocommit_true_index = next(i for i, c in enumerate(conn.set_autocommit.await_args_list) if c.args == (True,))
    restore_index = next(i for i, c in enumerate(conn.set_autocommit.await_args_list) if c.args == (False,))
    assert autocommit_true_index < restore_index


@pytest.mark.asyncio
async def test_reset_calls_schema_qualified_hypopg_reset_when_installed():
    conn, calls = _mock_connection(hypopg_schema="hypopg")

    await reset_pooled_connection(conn)

    reset_queries = [q for q in calls if "hypopg_reset" in _query_text(q)]
    assert reset_queries
    query = reset_queries[0]
    assert isinstance(query, sql.Composed)
    identifiers = [part for part in query if isinstance(part, sql.Identifier)]
    assert identifiers
    assert repr(identifiers[0]) == "Identifier('hypopg')"


@pytest.mark.asyncio
async def test_reset_skips_hypopg_when_not_installed():
    conn, calls = _mock_connection(hypopg_schema=None)

    await reset_pooled_connection(conn)

    assert not any("hypopg_reset" in _query_text(q) for q in calls)


@pytest.mark.asyncio
async def test_reset_errors_propagate_so_pool_can_discard():
    conn, _calls = _mock_connection(hypopg_schema=None)
    conn.execute = AsyncMock(side_effect=RuntimeError("reset failed"))

    with pytest.raises(RuntimeError, match="reset failed"):
        await reset_pooled_connection(conn)

    conn.set_autocommit.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_failed_reset_discards_pooled_connection():
    from psycopg.pq import TransactionStatus
    from psycopg_pool import AsyncConnectionPool

    closed: list[object] = []

    class FakeConn:
        def __init__(self) -> None:
            self.pgconn = MagicMock()
            self.pgconn.transaction_status = TransactionStatus.IDLE
            self._pool = object()

        async def close(self) -> None:
            closed.append(self)

    async def boom(_conn: object) -> None:
        raise RuntimeError("intentional reset failure")

    pool = AsyncConnectionPool(conninfo="postgresql://unused", open=False, reset=boom)
    fake = FakeConn()
    try:
        await pool._reset_connection(fake)  # type: ignore[attr-defined]
        assert closed == [fake]
        assert fake._pool is None
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URI, reason="DATABASE_URI is not set")
async def test_session_guc_does_not_leak_across_checkouts():
    from psycopg_pool import AsyncConnectionPool

    async def first_row(driver: SqlDriver, query: str) -> dict[str, Any]:
        rows = await driver.execute_query(query)  # type: ignore[arg-type]
        assert rows
        return rows[0].cells

    pool = DbConnPool(DATABASE_URI)
    raw_pool = AsyncConnectionPool(
        conninfo=DATABASE_URI,
        min_size=1,
        max_size=1,
        open=False,
        reset=reset_pooled_connection,
    )
    await raw_pool.open()
    await raw_pool.wait()
    pool.pool = raw_pool
    pool._is_valid = True  # type: ignore[attr-defined]
    driver = SqlDriver(conn=pool)
    try:
        before = await first_row(
            driver,
            """
            SELECT pg_backend_pid() AS pid,
                   current_setting('enable_hashjoin') AS enable_hashjoin
            """,
        )
        changed = await first_row(
            driver,
            """
            SET enable_hashjoin = off;
            SELECT pg_backend_pid() AS pid,
                   current_setting('enable_hashjoin') AS enable_hashjoin
            """,
        )
        after = await first_row(
            driver,
            """
            SELECT pg_backend_pid() AS pid,
                   current_setting('enable_hashjoin') AS enable_hashjoin
            """,
        )
        assert before["pid"] == changed["pid"] == after["pid"]
        assert changed["enable_hashjoin"] == "off"
        assert after["enable_hashjoin"] == before["enable_hashjoin"]
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URI, reason="DATABASE_URI is not set")
async def test_hypopg_state_does_not_leak_across_checkouts():
    from psycopg_pool import AsyncConnectionPool

    async def first_row(driver: SqlDriver, query: str) -> dict[str, Any]:
        rows = await driver.execute_query(query)  # type: ignore[arg-type]
        assert rows
        return rows[0].cells

    pool = DbConnPool(DATABASE_URI)
    raw_pool = AsyncConnectionPool(
        conninfo=DATABASE_URI,
        min_size=1,
        max_size=1,
        open=False,
        reset=reset_pooled_connection,
    )
    await raw_pool.open()
    await raw_pool.wait()
    pool.pool = raw_pool
    pool._is_valid = True  # type: ignore[attr-defined]
    driver = SqlDriver(conn=pool)
    try:
        installed = await first_row(
            driver,
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'hypopg') AS installed",
        )
        if not installed["installed"]:
            pytest.skip("HypoPG is not installed")

        await driver.execute_query("SELECT hypopg_reset(); SELECT hypopg_create_index('CREATE INDEX ON pg_class (relname)')")
        after = await first_row(driver, "SELECT count(*) AS count FROM hypopg_list_indexes()")
        assert int(after["count"]) == 0
    finally:
        try:
            await driver.execute_query("SELECT hypopg_reset()")
        finally:
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URI, reason="DATABASE_URI is not set")
async def test_failed_reset_replaces_backend_when_database_available():
    from psycopg_pool import AsyncConnectionPool

    async def boom(conn: Any) -> None:
        raise RuntimeError("intentional reset failure")

    raw_pool = AsyncConnectionPool(
        conninfo=DATABASE_URI,
        min_size=1,
        max_size=1,
        open=False,
        reset=boom,
    )
    await raw_pool.open()
    await raw_pool.wait()
    try:
        async with raw_pool.connection() as conn:
            pid_row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
        async with raw_pool.connection() as conn:
            new_pid_row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
        assert pid_row is not None and new_pid_row is not None
        assert pid_row[0] != new_pid_row[0]
    finally:
        await raw_pool.close()
