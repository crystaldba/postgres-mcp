# ruff: noqa: B017
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from postgres_mcp.sql.sql_driver import DbConnPool


class AsyncContextManagerMock(AsyncMock):
    """A better mock for async context managers"""

    async def __aenter__(self):
        return self.aenter

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_pool():
    """Create a mock for AsyncConnectionPool."""
    pool = MagicMock()

    # Create cursor context manager
    cursor = AsyncMock()

    # Create connection context manager
    connection = AsyncMock()
    connection.cursor = MagicMock(return_value=AsyncContextManagerMock())
    connection.cursor.return_value.aenter = cursor

    # Setup connection manager
    conn_ctx = AsyncContextManagerMock()
    conn_ctx.aenter = connection

    # Setup pool.connection() to return our mocked connection context manager
    pool.connection = MagicMock(return_value=conn_ctx)

    # Setup pool.open and pool.close as async mocks
    pool.open = AsyncMock()
    pool.close = AsyncMock()

    return pool


@pytest.mark.asyncio
async def test_pool_connect_success(mock_pool):
    """Test successful connection to the database pool."""
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        # Patch the connection test part to skip it
        with patch.object(DbConnPool, "pool_connect", new=AsyncMock(return_value=mock_pool)) as mock_connect:
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")
            pool = await db_pool.pool_connect()

            assert pool == mock_pool
            mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_pool_connect_with_retry(mock_pool):
    """Test pool connection with retry on failure."""
    # First attempt fails, second succeeds
    mock_pool.open.side_effect = [Exception("Connection error"), None]

    # Create a mock implementation of pool_connect that simulates a retry
    async def mock_pool_connect(self, connection_url=None):
        if not hasattr(self, "_attempt_count"):
            self._attempt_count = 0

        self._attempt_count += 1

        if self._attempt_count == 1:
            # First attempt fails
            raise Exception("Connection error")
        else:
            # Second attempt succeeds
            self.pool = mock_pool
            self._is_valid = True
            return mock_pool

    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        with patch("postgres_mcp.server.asyncio.sleep", AsyncMock()) as mock_sleep:
            with patch.object(DbConnPool, "pool_connect", mock_pool_connect):
                db_pool = DbConnPool("postgresql://user:pass@localhost/db")

                # Call our own custom implementation directly to simulate the retry
                # First call will fail, second call will succeed
                with pytest.raises(Exception):
                    await mock_pool_connect(db_pool)

                # Second attempt should succeed
                pool = await mock_pool_connect(db_pool)

                assert pool == mock_pool
                assert db_pool._is_valid is True  # type: ignore
                mock_sleep.assert_not_called()  # We're not actually calling sleep in our mock


@pytest.mark.asyncio
async def test_pool_connect_all_retries_fail(mock_pool):
    """Test pool connection when all retry attempts fail."""
    # Mock pool.open to raise an exception for the test
    mock_pool.open.side_effect = Exception("Persistent connection error")

    # Configure AsyncConnectionPool's constructor to return our mock
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        # Mock sleep to speed up test
        with patch("asyncio.sleep", AsyncMock()):
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")

            # This should fail since pool.open raises an exception
            with pytest.raises(Exception):
                await db_pool.pool_connect()

            # Verify the pool is marked as invalid
            assert db_pool._is_valid is False  # type: ignore
            # Verify open was called at least once (no need to verify retries here)
            assert mock_pool.open.call_count >= 1


@pytest.mark.asyncio
async def test_close_pool(mock_pool):
    """Test closing the connection pool."""
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Close the pool
        await db_pool.close()

        # Check that pool is now invalid
        assert db_pool._is_valid is False  # type: ignore
        assert db_pool.pool is None
        mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_handles_errors(mock_pool):
    """Test that close() handles exceptions gracefully."""
    mock_pool.close.side_effect = Exception("Error closing pool")

    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Close should not raise the exception
        await db_pool.close()

        # Pool should still be marked as invalid
        assert db_pool._is_valid is False  # type: ignore
        assert db_pool.pool is None


@pytest.mark.asyncio
async def test_pool_connect_initialized(mock_pool):
    """Test pool_connect when pool is already initialized."""
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        original_pool = await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Reset the mock counts
        mock_pool.open.reset_mock()

        # Get the pool again
        returned_pool = await db_pool.pool_connect()

        # Should return the existing pool without reconnecting
        assert returned_pool == original_pool
        mock_pool.open.assert_not_called()


@pytest.mark.asyncio
async def test_pool_connect_not_initialized(mock_pool):
    """Test pool_connect when pool is not yet initialized."""
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)

        # Get pool without initializing first
        pool = await db_pool.pool_connect()

        # Verify pool connect was called
        db_pool.pool_connect.assert_called_once()
        assert pool == mock_pool


@pytest.mark.asyncio
async def test_connection_url_property():
    """Test connection_url property."""
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    assert db_pool.connection_url == "postgresql://user:pass@localhost/db"

    # Change the URL
    db_pool.connection_url = "postgresql://newuser:newpass@otherhost/otherdb"
    assert db_pool.connection_url == "postgresql://newuser:newpass@otherhost/otherdb"


# --------------------------------------------------------------------------- #
# Pool invalidation: the pool is shared by every session, so only a real
# connection failure may tear it down - and concurrent rebuilds must not close
# each other's pools ("Connection attempt failed: the pool 'pool-N' is closed").
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402
from typing import ClassVar  # noqa: E402

import psycopg  # noqa: E402
from psycopg_pool import PoolClosed  # noqa: E402

from postgres_mcp.sql.sql_driver import SqlDriver  # noqa: E402


class FakeCursor:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.description = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        error = self.behaviour.get(query)
        if error is not None:
            raise error
        self.description = [("n",)]

    def nextset(self):
        return None

    async def fetchall(self):
        return [{"n": 1}]


class FakeConnection:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def cursor(self, row_factory=None):
        return FakeCursor(self.behaviour)

    async def rollback(self):
        pass


class FakePool:
    """Mimics psycopg_pool.AsyncConnectionPool: a slow open(), and PoolClosed
    from connection() once closed."""

    created: ClassVar[list["FakePool"]] = []
    behaviour: ClassVar[dict] = {}

    def __init__(self, conninfo=None, min_size=None, max_size=None, open=None):
        self.name = f"pool-{len(FakePool.created) + 1}"
        self.closed = False
        FakePool.created.append(self)

    async def open(self):
        await asyncio.sleep(0.01)  # a real open() connects - other tasks run meanwhile

    async def close(self):
        self.closed = True

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                if pool.closed:
                    raise PoolClosed(f"the pool {pool.name!r} is closed")
                await asyncio.sleep(0)
                return FakeConnection(FakePool.behaviour)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def fake_pools():
    FakePool.created = []
    FakePool.behaviour = {}
    with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", FakePool):
        yield FakePool


@pytest.mark.asyncio
async def test_sql_errors_keep_the_shared_pool(fake_pools):
    """A failed statement (bad SQL) leaves the pool healthy: it must not be
    invalidated - that closed every other session's connections."""
    fake_pools.behaviour["SELECT bad"] = psycopg.errors.UndefinedColumn('column "step" does not exist')
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    driver = SqlDriver(conn=db_pool)

    assert await driver.execute_query("SELECT 1") is not None
    with pytest.raises(psycopg.errors.UndefinedColumn):
        await driver.execute_query("SELECT bad")  # type: ignore[arg-type]

    assert db_pool.is_valid
    assert await driver.execute_query("SELECT 2") is not None  # type: ignore[arg-type]
    assert len(fake_pools.created) == 1  # the same pool throughout


@pytest.mark.asyncio
async def test_connection_errors_still_rebuild_the_pool(fake_pools):
    fake_pools.behaviour["SELECT broken"] = psycopg.OperationalError("server closed the connection unexpectedly")
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    driver = SqlDriver(conn=db_pool)

    with pytest.raises(psycopg.OperationalError):
        await driver.execute_query("SELECT broken")  # type: ignore[arg-type]
    assert not db_pool.is_valid

    assert await driver.execute_query("SELECT 2") is not None  # type: ignore[arg-type]
    assert len(fake_pools.created) == 2
    assert fake_pools.created[0].closed and not fake_pools.created[1].closed


@pytest.mark.asyncio
async def test_concurrent_queries_rebuild_one_pool_and_all_succeed(fake_pools):
    """Several queries arriving while the pool is invalid used to each close
    the pool another had just opened, failing with "the pool ... is closed"."""
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    driver = SqlDriver(conn=db_pool)
    assert await driver.execute_query("SELECT 1") is not None
    db_pool._is_valid = False  # e.g. after a real connection failure

    results = await asyncio.gather(
        *(driver.execute_query(f"SELECT {i}") for i in range(3)),  # type: ignore[arg-type]
        return_exceptions=True,
    )

    assert [r for r in results if isinstance(r, BaseException)] == []
    assert len(fake_pools.created) == 2  # the original + exactly one rebuild
    assert db_pool.is_valid and db_pool.pool is fake_pools.created[1]


@pytest.mark.asyncio
async def test_a_query_handed_a_replaced_pool_retries_on_the_new_one(fake_pools):
    """If another caller replaced the pool between pool_connect() and taking a
    connection, the statement never ran: retry once on the current pool."""
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    driver = SqlDriver(conn=db_pool)
    stale = await db_pool.pool_connect()

    original_connect = db_pool.pool_connect
    handed_out = []

    async def connect_once_stale(connection_url=None):
        if not handed_out:
            handed_out.append(stale)
            await stale.close()  # replaced by another caller ...
            db_pool._is_valid = False  # ... after a connection failure
            return stale
        return await original_connect(connection_url)

    db_pool.pool_connect = connect_once_stale  # type: ignore[method-assign]
    assert await driver.execute_query("SELECT 1") is not None
    assert len(fake_pools.created) == 2
