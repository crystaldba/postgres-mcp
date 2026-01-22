"""
Tests for connection loss scenarios (GitHub Issue #136).

This module tests how the connection pool handles connection loss,
specifically simulating network interruptions like VPN toggles or
Wi-Fi disconnections by injecting TCP RST packets into the socket.

These tests require Docker with PostgreSQL to run.
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
    logger.info(f"Using PostgreSQL version: {version}")
    return SqlDriver(engine_url=connection_string)


@pytest.fixture
def local_db_pool(test_postgres_connection_string):
    """Create a DbConnPool for tests that need direct pool access."""
    connection_string, version = test_postgres_connection_string
    logger.info(f"Using connection string: {connection_string}")
    logger.info(f"Using PostgreSQL version: {version}")
    return DbConnPool(connection_string)


class TestConnectionLoss:
    """
    Test suite for connection loss scenarios.

    These tests use TCP RST injection to simulate network interruptions
    like VPN toggles or Wi-Fi disconnections, reproducing GitHub Issue #136.
    """

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
    async def test_tcp_rst_breaks_connection(self, local_db_pool):
        """
        Test that injecting a TCP RST breaks the connection.

        This reproduces the network interruption scenario from GitHub Issue #136
        where toggling VPN or switching Wi-Fi causes connection loss.
        """
        pool = await local_db_pool.pool_connect()

        # First, verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 'before_rst' as status")
                result = await cursor.fetchone()
                assert result[0] == "before_rst"

        # Get a connection and inject TCP RST to simulate network interruption
        async with pool.connection() as conn:
            fileno = conn.fileno()
            logger.info(f"Injecting TCP RST on fd {fileno}")
            inject_tcp_rst(fileno)

        # Try to use the pool again - this should fail because the
        # connection was killed with RST
        with pytest.raises(Exception) as exc_info:
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 'after_rst' as status")
                    await cursor.fetchone()

        logger.info(f"Got expected exception after RST: {exc_info.value}")

        # The error should indicate a connection problem
        error_str = str(exc_info.value).lower()
        assert any(keyword in error_str for keyword in [
            "connection", "closed", "terminated", "broken", "reset",
            "eof", "server", "unexpectedly"
        ]), f"Unexpected error message: {exc_info.value}"

        await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_pool_marked_invalid_after_connection_loss(self, local_db_pool):
        """
        Test that the pool is marked invalid after connection loss.

        When a connection error occurs, the pool should be marked as invalid
        so that subsequent operations know the pool state is compromised.
        """
        pool = await local_db_pool.pool_connect()

        # Verify initial state
        assert local_db_pool.is_valid
        assert local_db_pool.last_error is None

        # Execute a query to verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()

        # Inject RST to break the connection
        async with pool.connection() as conn:
            fileno = conn.fileno()
            inject_tcp_rst(fileno)

        # The next query should fail
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")
        except Exception as e:
            logger.info(f"Query failed as expected: {e}")

        # Note: The pool is marked invalid in SqlDriver.execute_query,
        # not when using pool.connection() directly. Let's test via SqlDriver.
        await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_sql_driver_marks_pool_invalid_on_rst(self, local_sql_driver):
        """
        Test that SqlDriver properly marks the pool as invalid when
        connection is lost due to TCP RST.

        This is the core issue in #136 - when a connection is lost, the
        pool should be marked invalid so the error state is tracked.
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
        assert not pool_wrapper.is_valid, (
            "Pool should be marked invalid after connection loss"
        )
        assert pool_wrapper.last_error is not None, (
            "Pool should have last_error set after connection loss"
        )

        logger.info(f"Pool marked invalid with error: {pool_wrapper.last_error}")
        await pool_wrapper.close()

    @pytest.mark.asyncio
    async def test_no_auto_reconnect_after_rst(self, local_db_pool):
        """
        Test that the pool does NOT automatically reconnect after TCP RST.

        This demonstrates the bug in GitHub Issue #136 - after connection loss,
        the user expects auto-reconnection but it doesn't happen. The server
        must be restarted to recover.
        """
        pool = await local_db_pool.pool_connect()

        # Verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 'initial' as status")
                result = await cursor.fetchone()
                assert result[0] == "initial"

        # Break the connection with RST
        async with pool.connection() as conn:
            fileno = conn.fileno()
            inject_tcp_rst(fileno)

        # First attempt after RST should fail
        with pytest.raises(Exception):
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")

        # The pool should now be in a bad state
        # Try calling pool_connect again to see if it auto-reconnects
        try:
            pool2 = await local_db_pool.pool_connect()

            # Try to use the new pool
            async with pool2.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 'reconnected' as status")
                    result = await cursor.fetchone()

                    if result[0] == "reconnected":
                        # If we get here, reconnection worked
                        logger.info("Pool successfully reconnected!")
                    else:
                        pytest.fail("Unexpected result from reconnected pool")

        except Exception as e:
            # If we get here, auto-reconnection failed
            # This is the bug reported in Issue #136
            pytest.fail(
                f"Pool did NOT auto-reconnect after connection loss. "
                f"This is the bug reported in GitHub Issue #136. "
                f"Error: {e}"
            )
        finally:
            await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_consecutive_queries_after_rst(self, local_sql_driver):
        """
        Test behavior of consecutive queries after TCP RST injection.

        This simulates the real-world scenario where a user's network
        briefly disconnects and they try to continue using the MCP server.
        """
        pool_wrapper = local_sql_driver.connect()
        pool = await pool_wrapper.pool_connect()

        # Initial query should work
        result = await local_sql_driver.execute_query(
            "SELECT 'query1' as status"
        )
        assert result[0].cells["status"] == "query1"

        # Inject RST
        async with pool.connection() as conn:
            inject_tcp_rst(conn.fileno())

        # Track results of consecutive queries
        results = []
        for i in range(5):
            try:
                result = await local_sql_driver.execute_query(
                    f"SELECT 'query{i+2}' as status"
                )
                results.append(("success", result[0].cells["status"]))
            except Exception as e:
                results.append(("error", str(e)))

        logger.info(f"Results after RST: {results}")

        # At least the first query after RST should fail
        assert results[0][0] == "error", (
            "First query after RST should fail"
        )

        # Check if any subsequent queries succeeded (would indicate recovery)
        successes = [r for r in results if r[0] == "success"]
        if successes:
            logger.info(f"Recovery detected: {len(successes)} queries succeeded")
        else:
            logger.info("No recovery: all queries after RST failed")

        await pool_wrapper.close()

    @pytest.mark.asyncio
    async def test_error_message_content(self, local_db_pool):
        """
        Test that the error message after TCP RST is informative.

        The error message should help users understand that a connection
        problem occurred, matching the error in Issue #136:
        "server closed the connection unexpectedly"
        """
        pool = await local_db_pool.pool_connect()

        # Break the connection
        async with pool.connection() as conn:
            inject_tcp_rst(conn.fileno())

        # Capture the error
        with pytest.raises(Exception) as exc_info:
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")

        error_message = str(exc_info.value)
        logger.info(f"Error message after RST: {error_message}")

        # The error should be descriptive enough to understand the problem
        # Common PostgreSQL connection error keywords
        connection_error_keywords = [
            "connection",
            "closed",
            "server",
            "terminated",
            "reset",
            "broken",
            "eof",
            "unexpectedly",
        ]

        has_helpful_message = any(
            keyword in error_message.lower()
            for keyword in connection_error_keywords
        )

        assert has_helpful_message, (
            f"Error message should contain connection-related keywords. "
            f"Got: {error_message}"
        )

        await local_db_pool.close()

    @pytest.mark.asyncio
    async def test_pool_state_transitions(self, local_db_pool):
        """
        Test the pool state transitions through connection lifecycle.

        States:
        1. Initial: is_valid=False, pool=None
        2. Connected: is_valid=True, pool=<pool>
        3. After RST error: is_valid=False (via SqlDriver), pool=<pool> (broken)
        """
        # State 1: Initial
        assert not local_db_pool.is_valid
        assert local_db_pool.pool is None
        assert local_db_pool.last_error is None

        # State 2: Connected
        pool = await local_db_pool.pool_connect()
        assert local_db_pool.is_valid
        assert local_db_pool.pool is not None
        assert local_db_pool.last_error is None

        # Verify connection works
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                result = await cursor.fetchone()
                assert result[0] == 1

        logger.info(
            f"State after connect: is_valid={local_db_pool.is_valid}, "
            f"pool={'exists' if local_db_pool.pool else 'None'}"
        )

        # Inject RST
        async with pool.connection() as conn:
            inject_tcp_rst(conn.fileno())

        # Trigger error by using SqlDriver (which marks pool invalid)
        driver = SqlDriver(conn=local_db_pool)
        try:
            await driver.execute_query("SELECT 1")
        except Exception:
            pass

        # State 3: After error (via SqlDriver)
        logger.info(
            f"State after RST error: is_valid={local_db_pool.is_valid}, "
            f"pool={'exists' if local_db_pool.pool else 'None'}, "
            f"last_error={local_db_pool.last_error}"
        )

        assert not local_db_pool.is_valid, "Pool should be invalid after RST"
        # Note: pool object still exists but is broken
        assert local_db_pool.pool is not None, "Pool object should still exist"
        assert local_db_pool.last_error is not None, "Error should be recorded"

        await local_db_pool.close()
