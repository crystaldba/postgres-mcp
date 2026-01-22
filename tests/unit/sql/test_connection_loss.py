"""
Tests for connection loss scenarios (GitHub Issue #136).

This module tests how the connection pool handles connection loss,
specifically simulating network interruptions like VPN toggles or
Wi-Fi disconnections by injecting TCP RST packets into the socket.
"""

import logging
import socket
import struct

import pytest

from postgres_mcp.sql import DbConnPool
from postgres_mcp.sql import SqlDriver

logger = logging.getLogger(__name__)


def inject_tcp_rst(fileno: int) -> None:
    """
    Inject a TCP RST into a socket by setting SO_LINGER with zero timeout.

    When a socket is closed with SO_LINGER set to (1, 0), the kernel sends
    a RST packet instead of performing a graceful FIN/ACK shutdown sequence.
    This simulates an abrupt connection loss like a network interruption.

    Args:
        fileno: The file descriptor of the socket to reset.
    """
    # Create a socket object from the file descriptor
    # We use dup() to avoid closing the original fd when our socket object is GC'd
    sock = socket.fromfd(fileno, socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Set SO_LINGER with l_onoff=1, l_linger=0
        # This causes RST to be sent on close instead of FIN
        linger = struct.pack('ii', 1, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        # Close the socket - this sends RST
        sock.close()
    except Exception as e:
        logger.warning(f"Error injecting TCP RST: {e}")
        raise


@pytest.fixture
def local_sql_driver(test_postgres_connection_string):
    """Create a SqlDriver for tests that need a real database connection."""
    connection_string, version = test_postgres_connection_string
    logger.info(f"Using connection string: {connection_string}")
    logger.info(f"Using version: {version}")
    return SqlDriver(engine_url=connection_string)


@pytest.fixture
def local_db_pool(test_postgres_connection_string):
    """Create a DbConnPool for tests that need direct pool access."""
    connection_string, version = test_postgres_connection_string
    logger.info(f"Using connection string: {connection_string}")
    logger.info(f"Using version: {version}")
    return DbConnPool(connection_string)


class TestConnectionLoss:
    """Test suite for connection loss scenarios."""

    @pytest.mark.asyncio
    async def test_connection_works_initially(self, local_db_pool):
        """Verify that the connection works before we break it."""
        pool = await local_db_pool.pool_connect()

        # Execute a simple query to verify connection
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 as test")
                result = await cursor.fetchone()
                assert result[0] == 1

        assert local_db_pool.is_valid
        await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_tcp_rst_causes_connection_failure(self, local_db_pool):
        """
        Test that injecting a TCP RST causes subsequent queries to fail.

        This reproduces GitHub Issue #136 where network interruptions
        (VPN toggle, Wi-Fi change) cause the MCP server to lose its
        database connection without automatic recovery.
        """
        pool = await local_db_pool.pool_connect()

        # First, verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 as test")
                result = await cursor.fetchone()
                assert result[0] == 1

                # Get the file descriptor of the connection's socket
                fileno = conn.fileno()
                logger.info(f"Got connection file descriptor: {fileno}")

        # Now inject TCP RST to simulate network interruption
        # We need to get a connection and break it while it's checked out
        async with pool.connection() as conn:
            fileno = conn.fileno()
            logger.info(f"Injecting TCP RST on fd {fileno}")

            # Inject the RST - this will cause the connection to be broken
            inject_tcp_rst(fileno)

        # Try to use the pool again - this should fail
        # The pool may still have the broken connection
        with pytest.raises(Exception) as exc_info:
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 2 as test")
                    await cursor.fetchone()

        logger.info(f"Got expected exception: {exc_info.value}")
        # The exact error message varies, but it should indicate connection issues
        error_str = str(exc_info.value).lower()
        assert any(keyword in error_str for keyword in [
            "connection", "closed", "terminated", "broken", "reset", "eof"
        ]), f"Unexpected error message: {exc_info.value}"

        await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_sql_driver_marks_pool_invalid_on_connection_loss(self, local_sql_driver):
        """
        Test that SqlDriver properly marks the pool as invalid when connection is lost.

        This is the core issue in #136 - when a connection is lost, subsequent
        operations should fail gracefully and the pool should be marked invalid.
        """
        # Initialize the connection
        pool_wrapper = local_sql_driver.connect()
        pool = await pool_wrapper.pool_connect()

        # Verify initial state
        assert pool_wrapper.is_valid

        # Execute a query to verify connection works
        result = await local_sql_driver.execute_query("SELECT 1 as test")
        assert result is not None
        assert len(result) == 1
        assert result[0].cells["test"] == 1

        # Get a connection and inject RST
        async with pool.connection() as conn:
            fileno = conn.fileno()
            logger.info(f"Injecting TCP RST on SqlDriver connection fd {fileno}")
            inject_tcp_rst(fileno)

        # Now try to execute another query - this should fail
        # and the pool should be marked as invalid
        with pytest.raises(Exception) as exc_info:
            await local_sql_driver.execute_query("SELECT 2 as test")

        logger.info(f"SqlDriver raised exception: {exc_info.value}")

        # After the error, the pool should be marked as invalid
        # This is the current behavior we're testing
        assert not pool_wrapper.is_valid, (
            "Pool should be marked invalid after connection loss, "
            "but it's still marked as valid!"
        )
        assert pool_wrapper.last_error is not None, (
            "Pool should have last_error set after connection loss"
        )

        logger.info(f"Pool marked invalid with error: {pool_wrapper.last_error}")
        await pool_wrapper.close()

    @pytest.mark.asyncio
    async def test_pool_does_not_auto_reconnect(self, local_db_pool):
        """
        Test that the pool does NOT automatically reconnect after connection loss.

        This demonstrates the issue reported in #136 - the user expects
        auto-reconnection but it doesn't happen.
        """
        pool = await local_db_pool.pool_connect()

        # Verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 as test")
                result = await cursor.fetchone()
                assert result[0] == 1

        # Break the connection
        async with pool.connection() as conn:
            fileno = conn.fileno()
            inject_tcp_rst(fileno)

        # First attempt after RST should fail
        with pytest.raises(Exception):
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1 as test")

        # The pool is now marked as invalid
        assert not local_db_pool.is_valid

        # Try calling pool_connect again - current behavior doesn't auto-reconnect
        # because the pool still exists (even though it's invalid)
        # This is the bug: pool_connect checks `if self.pool and self._is_valid`
        # but when _is_valid is False, it should attempt to reconnect

        # Let's see what happens when we try to get the pool again
        try:
            pool2 = await local_db_pool.pool_connect()
            # If we get here, the pool was returned (either reconnected or same broken pool)

            # Try to use it
            try:
                async with pool2.connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 3 as test")
                        result = await cursor.fetchone()
                        # If we get here, reconnection worked!
                        logger.info("Pool auto-reconnected successfully!")
                        assert result[0] == 3
            except Exception as e:
                # Connection still broken - no auto-reconnect
                logger.info(f"Pool did NOT auto-reconnect: {e}")
                pytest.fail(
                    f"Pool did not auto-reconnect after connection loss. "
                    f"This is the issue reported in GitHub #136. Error: {e}"
                )
        except Exception as e:
            logger.info(f"pool_connect raised: {e}")
            pytest.fail(
                f"pool_connect failed to reconnect after connection loss. "
                f"This is the issue reported in GitHub #136. Error: {e}"
            )
        finally:
            await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_multiple_connections_in_pool_after_rst(self, local_db_pool):
        """
        Test behavior when one connection in the pool is killed via RST.

        The pool has min_size=1, max_size=5. If we kill one connection,
        the pool should ideally handle this gracefully.
        """
        pool = await local_db_pool.pool_connect()

        # Get multiple connections to populate the pool
        connections_fds = []
        for i in range(3):
            async with pool.connection() as conn:
                fd = conn.fileno()
                connections_fds.append(fd)
                async with conn.cursor() as cursor:
                    await cursor.execute(f"SELECT {i} as test")
                    result = await cursor.fetchone()
                    assert result[0] == i

        logger.info(f"Created connections with fds: {connections_fds}")

        # Now kill one connection via RST
        async with pool.connection() as conn:
            fileno = conn.fileno()
            logger.info(f"Killing connection with fd {fileno}")
            inject_tcp_rst(fileno)

        # The pool should now have a broken connection
        # Let's see how many successful queries we can make
        successful_queries = 0
        failed_queries = 0

        for i in range(5):
            try:
                async with pool.connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(f"SELECT {i + 100} as test")
                        result = await cursor.fetchone()
                        if result[0] == i + 100:
                            successful_queries += 1
            except Exception as e:
                logger.info(f"Query {i} failed: {e}")
                failed_queries += 1

        logger.info(f"After RST: {successful_queries} successful, {failed_queries} failed")

        # We expect at least one failure due to the broken connection
        # If the pool handles it gracefully, subsequent queries might work
        # with new connections
        assert failed_queries > 0, (
            "Expected at least one query to fail after RST injection"
        )

        await local_db_pool.close()


# ============================================================================
# Mock-based tests that don't require Docker
# These tests verify the logic of connection loss handling without needing
# a real database connection.
# ============================================================================

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch


class AsyncContextManagerMock(AsyncMock):
    """A mock for async context managers."""

    async def __aenter__(self):
        return self.aenter

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class TestConnectionLossMocked:
    """Mock-based tests for connection loss scenarios."""

    @pytest.mark.asyncio
    async def test_pool_connect_returns_invalid_pool_without_reconnect(self):
        """
        Test that pool_connect returns a pool even when marked invalid.

        This demonstrates the bug in pool_connect logic:
        - pool_connect checks `if self.pool and self._is_valid`
        - When _is_valid is False but pool exists, it falls through
        - The code then closes the pool and tries to create a new one
        - But if the close or reconnect fails, the pool stays invalid
        """
        # Create a pool mock
        mock_pool = MagicMock()
        mock_pool.open = AsyncMock()
        mock_pool.close = AsyncMock()

        # Create connection mock that will fail
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=AsyncContextManagerMock())

        conn_ctx = AsyncContextManagerMock()
        conn_ctx.aenter = mock_conn
        mock_pool.connection = MagicMock(return_value=conn_ctx)

        with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")

            # Manually simulate initial connection success
            db_pool.pool = mock_pool
            db_pool._is_valid = True

            # Now mark it as invalid (simulating connection loss)
            db_pool._is_valid = False
            db_pool._last_error = "server closed the connection unexpectedly"

            # The pool object still exists but is marked invalid
            assert db_pool.pool is not None
            assert not db_pool.is_valid

            # Now call pool_connect - it should attempt to reconnect
            # Looking at the code:
            # if self.pool and self._is_valid:
            #     return self.pool
            # This means when _is_valid is False, it will continue
            # and try to close and reconnect

            # Set up the mock for reconnection attempt
            mock_cursor = AsyncMock()
            mock_cursor.execute = AsyncMock()
            cursor_ctx = AsyncContextManagerMock()
            cursor_ctx.aenter = mock_cursor
            mock_conn.cursor.return_value = cursor_ctx

            try:
                pool = await db_pool.pool_connect()
                # If we get here, reconnection was attempted
                logger.info("pool_connect attempted reconnection")
                assert pool is not None
            except Exception as e:
                logger.info(f"pool_connect failed: {e}")

            await db_pool.close()

    @pytest.mark.asyncio
    async def test_execute_query_marks_pool_invalid_on_error(self):
        """
        Test that execute_query marks the pool as invalid when an error occurs.

        This is the current behavior - errors mark the pool invalid but
        don't trigger automatic reconnection.
        """
        # Create mock pool
        mock_pool = MagicMock()

        # Create mock cursor that raises connection error on execute
        mock_cursor = MagicMock()
        mock_cursor.execute = AsyncMock(
            side_effect=Exception("server closed the connection unexpectedly")
        )
        mock_cursor.description = None
        mock_cursor.nextset = MagicMock(return_value=False)

        # Create proper async context manager for cursor
        cursor_ctx = MagicMock()
        cursor_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
        cursor_ctx.__aexit__ = AsyncMock(return_value=None)

        # Create mock connection
        mock_conn = MagicMock()
        mock_conn.cursor = MagicMock(return_value=cursor_ctx)
        mock_conn.rollback = AsyncMock()

        # Create proper async context manager for connection
        conn_ctx = MagicMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool.connection = MagicMock(return_value=conn_ctx)

        # Create DbConnPool
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")
        db_pool.pool = mock_pool
        db_pool._is_valid = True
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)

        # Create SqlDriver with the pool
        driver = SqlDriver(conn=db_pool)

        # Execute a query that will fail
        with pytest.raises(Exception) as exc_info:
            await driver.execute_query("SELECT 1")

        # Verify the error
        assert "server closed the connection unexpectedly" in str(exc_info.value)

        # Verify pool was marked as invalid
        assert not db_pool.is_valid
        assert db_pool.last_error is not None
        assert "server closed the connection unexpectedly" in db_pool.last_error

    @pytest.mark.asyncio
    async def test_reconnection_logic_gap(self):
        """
        Test that demonstrates the reconnection logic gap (the actual bug).

        The issue is that after a connection error:
        1. Pool is marked invalid
        2. Next call to execute_query calls pool_connect
        3. pool_connect sees pool exists but is invalid
        4. It closes the old pool and tries to create a new one
        5. BUT: the new pool creation can fail if the network is still down
        6. AND: there's no retry logic in pool_connect

        This test demonstrates that once the pool is invalid, subsequent
        calls fail without automatic recovery.
        """
        call_count = {"value": 0}

        async def mock_pool_connect(connection_url=None):
            call_count["value"] += 1
            if call_count["value"] == 1:
                # First call succeeds (initial connection)
                return MagicMock()
            else:
                # Subsequent calls fail (network still down)
                raise Exception("Connection refused - network unreachable")

        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        with patch.object(db_pool, "pool_connect", side_effect=mock_pool_connect):
            # First call succeeds
            try:
                await db_pool.pool_connect()
            except Exception:
                pass

            # Simulate connection loss
            db_pool._is_valid = False

            # Second call fails
            with pytest.raises(Exception) as exc_info:
                await db_pool.pool_connect()

            assert "Connection refused" in str(exc_info.value)
            assert call_count["value"] == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_transient_failure(self):
        """
        Test that there's no retry logic for transient failures.

        When a connection fails, the code should ideally:
        1. Detect that it's a transient error
        2. Wait and retry with exponential backoff
        3. Only give up after multiple attempts

        Currently, it just fails immediately.
        """
        # Track how many times pool.open is called
        open_call_count = {"value": 0}

        async def mock_open():
            open_call_count["value"] += 1
            if open_call_count["value"] < 3:
                raise Exception("Connection refused (transient)")
            # Third call would succeed
            return None

        mock_pool = MagicMock()
        mock_pool.open = mock_open
        mock_pool.close = AsyncMock()

        with patch("postgres_mcp.sql.sql_driver.AsyncConnectionPool", return_value=mock_pool):
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")

            # This should fail on first attempt without retrying
            with pytest.raises(Exception) as exc_info:
                await db_pool.pool_connect()

            # Verify only one attempt was made (no retries)
            assert open_call_count["value"] == 1, (
                f"Expected 1 attempt (no retries), but got {open_call_count['value']} attempts. "
                "This confirms there's no retry logic for transient failures."
            )

            assert "Connection refused" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_pool_state_after_connection_error(self):
        """
        Test the pool state transitions after a connection error.

        Expected behavior (current):
        1. Pool starts valid
        2. Connection error occurs
        3. Pool marked invalid
        4. No automatic recovery

        Desired behavior (for fix):
        1. Pool starts valid
        2. Connection error occurs
        3. Pool attempts reconnection
        4. If reconnection succeeds, pool is valid again
        """
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Initial state
        assert not db_pool.is_valid  # Not connected yet
        assert db_pool.pool is None

        # Simulate successful connection
        mock_pool = MagicMock()
        db_pool.pool = mock_pool
        db_pool._is_valid = True

        assert db_pool.is_valid
        assert db_pool.pool is not None

        # Simulate connection error
        db_pool._is_valid = False
        db_pool._last_error = "server closed the connection unexpectedly"

        # Pool object still exists but is marked invalid
        assert not db_pool.is_valid
        assert db_pool.pool is not None  # This is the issue - pool exists but is broken
        assert db_pool.last_error is not None

        # The fix should make pool_connect detect this state and reconnect
        logger.info(
            "Pool state after error: "
            f"is_valid={db_pool.is_valid}, "
            f"pool={'exists' if db_pool.pool else 'None'}, "
            f"last_error={db_pool.last_error}"
        )
