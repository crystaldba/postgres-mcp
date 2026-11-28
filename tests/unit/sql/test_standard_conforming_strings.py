"""Pooled connections must run with standard_conforming_strings = on.

SafeSqlDriver validates SQL with pglast, which lexes string literals as if
standard_conforming_strings were on, and then sends the original text to the server. The
server must tokenize that text the same way, so the product forces the setting on for every
pooled connection (when it is opened and after every DISCARD ALL reset) and restricted-mode
execution refuses a connection that does not report it on.
"""

import os
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.conninfo import make_conninfo

from postgres_mcp.sql.safe_sql import SafeSqlDriver
from postgres_mcp.sql.sql_driver import DbConnPool
from postgres_mcp.sql.sql_driver import SqlDriver
from postgres_mcp.sql.sql_driver import configure_pooled_connection
from postgres_mcp.sql.sql_driver import reports_standard_conforming_strings
from postgres_mcp.sql.sql_driver import reset_pooled_connection

DATABASE_URI = os.environ.get("DATABASE_URI")
SET_ON = "SET standard_conforming_strings = on"


def _text(value: object) -> str:
    """Normalize driver values: SQL_ASCII databases return bytes for text results."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _mock_connection(reported: str | None = "on") -> tuple[MagicMock, list[object]]:
    conn = MagicMock()
    conn.autocommit = False
    conn.set_autocommit = AsyncMock()
    conn.close = AsyncMock()
    conn.info.parameter_status = MagicMock(return_value=reported)
    calls: list[object] = []

    async def execute(query: object, *args: Any, **kwargs: Any) -> AsyncMock:
        calls.append(query)
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    conn.execute = AsyncMock(side_effect=execute)
    return conn, calls


class _ConnectionCM:
    async def __aenter__(self):
        class _Cursor:
            async def execute(self, query: object) -> None:
                return None

        class _CursorCM:
            async def __aenter__(self):
                return _Cursor()

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class _Connection:
            def cursor(self):
                return _CursorCM()

        return _Connection()

    async def __aexit__(self, exc_type, exc, tb):
        return None


async def _captured_pool_args(url: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    mock_pool = MagicMock()
    mock_pool.open = AsyncMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection = MagicMock(return_value=_ConnectionCM())

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return mock_pool

    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", side_effect=factory):
        db_pool = DbConnPool(url)
        await db_pool.pool_connect()
        await db_pool.close()
    return captured


# --- reporting -------------------------------------------------------------------------------


@pytest.mark.parametrize(("reported", "expected"), [("on", True), ("off", False), (None, False)])
def test_reports_standard_conforming_strings(reported: str | None, expected: bool):
    conn, _ = _mock_connection(reported)
    assert reports_standard_conforming_strings(conn) is expected
    conn.info.parameter_status.assert_called_with("standard_conforming_strings")


# --- pool wiring and operator connection options ---------------------------------------------


@pytest.mark.asyncio
async def test_pool_connect_wires_configure_callback():
    captured = await _captured_pool_args("postgresql://user:pass@localhost/db")
    assert captured["configure"] is configure_pooled_connection
    assert captured["reset"] is reset_pooled_connection


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pass@localhost/db",
        "postgresql://user:pass@localhost/db?options=-c%20search_path%3Dapp",
        "postgresql://user:pass@localhost/db?options=-c%20standard_conforming_strings%3Doff",
        "host=localhost dbname=db user=user options='-c search_path=app'",
        "service=analytics",
    ],
)
@pytest.mark.asyncio
async def test_pool_passes_operator_conninfo_through_unchanged(url: str):
    """The setting is applied with SET, not through libpq ``options``.

    libpq uses the ``options`` from a service file or from PGOPTIONS only when the connection
    parameters do not include ``options`` at all, and psycopg lets a keyword argument replace an
    ``options`` value from the conninfo string. Passing no ``options`` keyword keeps whatever the
    operator configured in effect.
    """
    captured = await _captured_pool_args(url)
    assert captured["conninfo"] == url
    assert "options" not in captured["kwargs"]


def test_psycopg_keyword_replaces_conninfo_options():
    """Documents the psycopg behavior that rules out passing ``options`` as a pool keyword."""
    for url in (
        "postgresql://u@h/db?options=-c%20search_path%3Dapp",
        "host=h dbname=db options='-c search_path=app'",
    ):
        assert conninfo_to_dict(url)["options"] == "-c search_path=app"
        merged = conninfo_to_dict(make_conninfo(url, options="-c standard_conforming_strings=on"))
        assert merged["options"] == "-c standard_conforming_strings=on"


# --- configure and reset callbacks ------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_sets_standard_conforming_strings_in_autocommit():
    conn, calls = _mock_connection("on")

    await configure_pooled_connection(conn)

    assert calls == [SET_ON]
    assert [c.args for c in conn.set_autocommit.await_args_list] == [(True,), (False,)]
    conn.close.assert_not_awaited()


@pytest.mark.parametrize("reported", ["off", None])
@pytest.mark.asyncio
async def test_configure_closes_connection_when_setting_not_reported_on(reported: str | None):
    conn, _ = _mock_connection(reported)

    with pytest.raises(RuntimeError, match="standard_conforming_strings"):
        await configure_pooled_connection(conn)

    conn.close.assert_awaited_once()
    conn.set_autocommit.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_reset_sets_standard_conforming_strings_after_discard_all():
    conn, calls = _mock_connection("on")

    await reset_pooled_connection(conn)

    assert calls.index("DISCARD ALL") < calls.index(SET_ON)


@pytest.mark.asyncio
async def test_reset_fails_when_setting_not_reported_on():
    conn, _ = _mock_connection("off")

    with pytest.raises(RuntimeError, match="standard_conforming_strings"):
        await reset_pooled_connection(conn)

    conn.set_autocommit.assert_awaited_with(False)


# --- fail-closed check for read-only (restricted mode) execution ------------------------------


@pytest.mark.parametrize("reported", ["off", None])
@pytest.mark.asyncio
async def test_readonly_execution_refuses_connection_without_standard_conforming_strings(reported: str | None):
    connection = MagicMock()
    connection.info.parameter_status = MagicMock(return_value=reported)
    driver = SqlDriver(conn=MagicMock())

    with pytest.raises(ValueError, match="standard_conforming_strings"):
        await driver._execute_with_connection(connection, "SELECT 1", None, force_readonly=True)  # type: ignore[reportPrivateUsage]

    connection.cursor.assert_not_called()


@pytest.mark.asyncio
async def test_unrestricted_execution_does_not_check_setting():
    connection = MagicMock()
    connection.info.parameter_status = MagicMock(return_value="off")
    cursor = AsyncMock()
    cursor.nextset = MagicMock(return_value=None)
    cursor.description = None
    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=None)
    connection.cursor = MagicMock(return_value=cursor_cm)
    driver = SqlDriver(conn=MagicMock())

    result = await driver._execute_with_connection(connection, "SELECT 1", None, force_readonly=False)  # type: ignore[reportPrivateUsage]

    assert result is None
    connection.cursor.assert_called_once()


# --- real database ----------------------------------------------------------------------------


async def _check_pooled_connections_force_setting_on(admin_url: str) -> None:
    """With a database whose default is standard_conforming_strings = off, connections obtained
    through DbConnPool report on, both when first opened and after a pool reset."""
    dbname = "scs_off_" + os.urandom(4).hex()
    admin = await psycopg.AsyncConnection.connect(admin_url, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        await admin.execute(sql.SQL("ALTER DATABASE {} SET standard_conforming_strings = off").format(sql.Identifier(dbname)))
        url = make_conninfo(admin_url, dbname=dbname)

        # Precondition: without the product's pool, a connection to this database has it off,
        # and DISCARD ALL returns a SET value to that default.
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as raw:
            assert raw.info.parameter_status("standard_conforming_strings") == "off"
            await raw.execute("SET standard_conforming_strings = on")
            await raw.execute("DISCARD ALL")
            row = await (await raw.execute("SHOW standard_conforming_strings")).fetchone()
            assert row is not None and _text(row[0]) == "off"

        db_pool = DbConnPool(url)
        try:
            pool = await db_pool.pool_connect()
            seen: dict[int, int] = {}
            for _ in range(10):
                async with pool.connection() as conn:
                    row = await (await conn.execute("SELECT pg_backend_pid(), current_setting('standard_conforming_strings')")).fetchone()
                    assert row is not None
                    pid, setting = row
                    assert _text(setting) == "on"
                    assert conn.info.parameter_status("standard_conforming_strings") == "on"
                    if seen:
                        # Turn it off on a checked-out connection; the reset that runs when the
                        # connection is returned must turn it back on.
                        await conn.execute("SET standard_conforming_strings = off")
                        await conn.commit()
                seen[pid] = seen.get(pid, 0) + 1
            assert max(seen.values()) > 1, f"no pooled connection was reused through a reset: {seen}"

            # Through the restricted-mode path, a backslash in an ordinary string literal is an
            # ordinary character: the literal 'a\b' is three characters long.
            driver = SafeSqlDriver(sql_driver=SqlDriver(conn=db_pool))
            rows = await driver.execute_query("SELECT length('a\\b') AS n, current_setting('standard_conforming_strings') AS scs")
            assert rows is not None
            assert int(rows[0].cells["n"]) == 3
            assert _text(rows[0].cells["scs"]) == "on"
        finally:
            await db_pool.close()
    finally:
        try:
            # Closed client connections can leave backends exiting; DROP DATABASE waits a few
            # seconds for them, and terminating them first keeps a failed run from leaking it.
            await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", [dbname])
            await admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_pooled_connections_force_standard_conforming_strings(test_postgres_connection_string):
    connection_string, _version = test_postgres_connection_string
    await _check_pooled_connections_force_setting_on(connection_string)


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URI, reason="DATABASE_URI is not set")
async def test_pooled_connections_force_standard_conforming_strings_database_uri():
    assert DATABASE_URI is not None
    await _check_pooled_connections_force_setting_on(DATABASE_URI)
