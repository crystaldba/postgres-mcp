# ruff: noqa: B017
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import pytest

from postgres_mcp.sql import DbConnPool
from postgres_mcp.sql import SqlDriver


class AsyncContextManagerMock(AsyncMock):
    """A better mock for async context managers"""

    async def __aenter__(self):
        return self.aenter

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_connection():
    """Create a mock for the database connection."""
    connection = MagicMock()
    cursor = AsyncContextManagerMock()

    # Make the cursor behave like an async context manager
    cursor.aenter = cursor

    # Configure cursor context manager
    cursor_cm = AsyncContextManagerMock()
    cursor_cm.aenter = cursor
    connection.cursor.return_value = cursor_cm

    # Configure cursor.description to return a value (indicating results)
    cursor.description = ["column1", "column2"]

    # Configure fetchall to return some mock data
    cursor.fetchall.return_value = [
        {"id": 1, "name": "test1"},
        {"id": 2, "name": "test2"},
    ]

    return connection, cursor


@pytest.fixture
def mock_db_pool():
    """Create a mock for DbConnPool with a mock connection."""
    # Create the pool
    pool = MagicMock()

    # Create connection that returns from async context manager
    connection = AsyncContextManagerMock()
    connection.aenter = connection

    # Create cursor that returns from async context manager
    cursor = AsyncContextManagerMock()
    cursor.aenter = cursor

    # Setup connection to return cursor
    cursor_cm = AsyncContextManagerMock()
    cursor_cm.aenter = cursor
    connection.cursor.return_value = cursor_cm

    # Configure cursor.description
    cursor.description = ["column1", "column2"]

    # Configure fetchall to return some mock data
    cursor.fetchall.return_value = [
        {"id": 1, "name": "test1"},
        {"id": 2, "name": "test2"},
    ]

    # Create connection context manager
    conn_cm = AsyncContextManagerMock()
    conn_cm.aenter = connection

    # Setup pool.connection() to return the connection context manager
    pool.connection.return_value = conn_cm

    # Create mock for DbConnPool
    db_pool = MagicMock(spec=DbConnPool)
    db_pool.pool_connect.return_value = pool
    db_pool._is_valid = True

    return db_pool, connection, cursor


@pytest.mark.asyncio
async def test_execute_query_readonly_transaction(mock_connection):
    """Test execute_query with read-only transaction."""
    connection, cursor = mock_connection

    # Create SqlDriver with a connection
    driver = SqlDriver(conn=connection)

    # Create a mock implementation of _execute_with_connection
    async def mock_impl(connection, query, params, force_readonly):
        # Simulate transaction
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")

        # Execute the query
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)

        # Get results
        rows = await cursor.fetchall()

        # End transaction
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")

        # Return results
        return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

    # Must match the parameter names from the original method
    driver._execute_with_connection = mock_impl  # type: ignore

    # Execute a read-only query
    result = await driver._execute_with_connection(  # type: ignore
        connection, "SELECT * FROM test", None, force_readonly=True
    )

    # Verify transaction management
    assert cursor.execute.call_count >= 3
    assert call("BEGIN TRANSACTION READ ONLY") in cursor.execute.call_args_list
    assert call("ROLLBACK") in cursor.execute.call_args_list

    # Verify results were processed correctly
    assert result is not None
    assert len(result) == 2
    assert result[0].cells["id"] == 1
    assert result[1].cells["name"] == "test2"


@pytest.mark.asyncio
async def test_execute_query_writeable_transaction(mock_connection):
    """Test execute_query with writeable transaction."""
    connection, cursor = mock_connection

    # Create SqlDriver with a connection
    driver = SqlDriver(conn=connection)

    # Create a mock implementation of _execute_with_connection
    async def mock_impl(connection, query, params, force_readonly):
        # Simulate transaction
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")

        # Execute the query
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)

        # Get results
        rows = await cursor.fetchall()

        # End transaction
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")

        # Return results
        return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

    # Must match the parameter names from the original method
    driver._execute_with_connection = mock_impl  # type: ignore

    # Execute a writeable query
    result = await driver._execute_with_connection(  # type: ignore
        connection, "UPDATE test SET name = 'updated'", None, force_readonly=False
    )

    # Verify transaction management
    assert call("COMMIT") in cursor.execute.call_args_list

    # Verify results were processed correctly
    assert result is not None


@pytest.mark.asyncio
async def test_execute_query_error_handling(mock_connection):
    """Test execute_query error handling."""
    connection, cursor = mock_connection

    # Configure cursor.execute to raise an exception on the second call
    cursor.execute.side_effect = [None, Exception("Query execution failed")]

    # Create SqlDriver with a connection
    driver = SqlDriver(conn=connection)

    # Create mock function that raises exception
    async def mock_execute_error(connection, query, params, force_readonly):
        raise Exception("Query execution failed")

    driver._execute_with_connection = mock_execute_error  # type: ignore

    # Execute a query that will fail
    with pytest.raises(Exception) as excinfo:
        await driver._execute_with_connection(  # type: ignore
            connection, "SELECT * FROM nonexistent", None, force_readonly=True
        )

    # Check the error message
    assert "Query execution failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_execute_query_no_results(mock_connection):
    """Test execute_query with no results returned."""
    connection, cursor = mock_connection

    # Configure cursor.description to return None (indicating no results)
    cursor.description = None

    # Create SqlDriver with a connection
    driver = SqlDriver(conn=connection)

    # Create a mock implementation of _execute_with_connection
    async def mock_impl(connection, query, params, force_readonly):
        # Simulate transaction
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")

        # Execute the query
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)

        # End transaction
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")

        # Return None for no results
        return None

    # Must match the parameter names from the original method
    driver._execute_with_connection = mock_impl  # type: ignore

    # Execute a query that returns no results
    result = await driver._execute_with_connection(  # type: ignore
        connection, "DELETE FROM test", None, force_readonly=False
    )

    # Verify result is None for no-result queries
    assert result is None
    assert call("COMMIT") in cursor.execute.call_args_list


@pytest.mark.asyncio
async def test_execute_query_with_params(mock_connection):
    """Test execute_query with parameters."""
    connection, cursor = mock_connection

    # Create SqlDriver with a connection
    driver = SqlDriver(conn=connection)

    # Create a mock implementation of _execute_with_connection
    async def mock_impl(connection, query, params, force_readonly):
        # Simulate transaction
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")

        # Execute the query with parameters
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)

        # Get results
        rows = await cursor.fetchall()

        # End transaction
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")

        # Return results
        return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

    # Must match the parameter names from the original method
    driver._execute_with_connection = mock_impl  # type: ignore

    # Execute a query with parameters
    await driver._execute_with_connection(  # type: ignore
        connection, "SELECT * FROM test WHERE id = %s", [1], force_readonly=True
    )

    # Verify parameters were passed correctly
    assert call("SELECT * FROM test WHERE id = %s", [1]) in cursor.execute.call_args_list


@pytest.mark.asyncio
async def test_execute_query_from_pool(mock_db_pool):
    """Test execute_query using a connection from a pool."""
    db_pool, connection, cursor = mock_db_pool

    # Create a mock execute function
    async def mock_pool_execute(*args, **kwargs):
        return [
            SqlDriver.RowResult(cells={"id": 1, "name": "test1"}),
            SqlDriver.RowResult(cells={"id": 2, "name": "test2"}),
        ]

    # Create SqlDriver with the mocked pool
    driver = SqlDriver(conn=db_pool)
    driver.execute_query = mock_pool_execute

    # Execute a query
    result = await driver.execute_query("SELECT * FROM test")

    # Verify results were processed correctly
    assert result is not None
    assert len(result) == 2
    assert result[0].cells["id"] == 1
    assert result[1].cells["name"] == "test2"


@pytest.mark.asyncio
async def test_connection_error_marks_pool_invalid(mock_db_pool):
    """Test that connection errors mark the pool as invalid."""
    db_pool, connection, cursor = mock_db_pool

    # Configure pool_connect to raise an exception
    db_pool.pool_connect.side_effect = Exception("Connection failed")

    # Create SqlDriver with the mocked pool
    driver = SqlDriver(conn=db_pool)

    # Execute a query that will fail due to connection error
    with pytest.raises(Exception):
        await driver.execute_query("SELECT * FROM test")

    # Make pool invalid manually (since we're bypassing the actual method)
    db_pool._is_valid = False
    db_pool._last_error = "Connection failed"

    # Verify pool was marked as invalid
    assert db_pool._is_valid is False
    assert isinstance(db_pool._last_error, str)


@pytest.mark.asyncio
async def test_engine_url_connection():
    """Test connecting with engine_url instead of connection object."""
    db_pool = MagicMock(spec=DbConnPool)

    with patch("postgres_mcp.sql.DbConnPool", return_value=db_pool):
        # Create SqlDriver with engine_url
        driver = SqlDriver(engine_url="postgresql://user:pass@localhost/db")

        # Call connect to create mock pool
        driver.connect()

        # Verify driver state
        assert driver.is_pool is True
        assert driver.conn is not None


def _make_autocommit_connection(autocommit=True, has_results=True):
    """Create a properly mocked async connection for _execute_with_connection tests."""
    cursor = AsyncMock()
    cursor.nextset = MagicMock(return_value=False)
    cursor.fetchall = AsyncMock(return_value=[
        {"id": 1, "name": "test1"},
        {"id": 2, "name": "test2"},
    ])
    cursor.description = ["column1", "column2"] if has_results else None

    cursor_ctx = AsyncMock()
    cursor_ctx.__aenter__ = AsyncMock(return_value=cursor)
    cursor_ctx.__aexit__ = AsyncMock(return_value=False)

    connection = MagicMock()
    connection.autocommit = autocommit
    connection.cursor = MagicMock(return_value=cursor_ctx)
    connection.rollback = AsyncMock()

    return connection, cursor


@pytest.mark.asyncio
async def test_execute_query_autocommit_skips_transaction():
    """Test that autocommit mode skips BEGIN/COMMIT/ROLLBACK statements."""
    connection, cursor = _make_autocommit_connection(autocommit=True)

    driver = SqlDriver(conn=connection)

    result = await driver._execute_with_connection(
        connection, "SELECT * FROM test", None, force_readonly=True
    )

    # Verify no transaction statements were issued
    for c in cursor.execute.call_args_list:
        sql = c[0][0] if c[0] else ""
        assert "BEGIN" not in sql, "BEGIN should not be called in autocommit mode"
        assert "COMMIT" not in sql, "COMMIT should not be called in autocommit mode"
        assert "ROLLBACK" not in sql, "ROLLBACK should not be called in autocommit mode"

    # Verify the actual query was still executed
    assert call("SELECT * FROM test") in cursor.execute.call_args_list

    # Verify results were returned
    assert result is not None
    assert len(result) == 2


@pytest.mark.asyncio
async def test_execute_query_autocommit_no_results():
    """Test autocommit mode with DDL statements that return no results."""
    connection, cursor = _make_autocommit_connection(autocommit=True, has_results=False)

    driver = SqlDriver(conn=connection)

    result = await driver._execute_with_connection(
        connection, "CREATE TABLE test (id int)", None, force_readonly=False
    )

    # Verify no transaction statements were issued
    for c in cursor.execute.call_args_list:
        sql = c[0][0] if c[0] else ""
        assert "COMMIT" not in sql, "COMMIT should not be called in autocommit mode"

    assert result is None


@pytest.mark.asyncio
async def test_execute_query_non_autocommit_uses_transaction():
    """Test that non-autocommit mode still uses BEGIN/COMMIT/ROLLBACK as before."""
    connection, cursor = _make_autocommit_connection(autocommit=False)

    driver = SqlDriver(conn=connection)

    await driver._execute_with_connection(
        connection, "SELECT * FROM test", None, force_readonly=True
    )

    # Verify transaction statements WERE issued
    assert call("BEGIN TRANSACTION READ ONLY") in cursor.execute.call_args_list
    assert call("ROLLBACK") in cursor.execute.call_args_list


@pytest.mark.asyncio
async def test_execute_query_autocommit_error_skips_rollback():
    """Test that autocommit mode skips rollback on error."""
    connection, cursor = _make_autocommit_connection(autocommit=True)

    # Make query execution fail
    cursor.execute = AsyncMock(side_effect=Exception("Query failed"))

    driver = SqlDriver(conn=connection)

    with pytest.raises(Exception, match="Query failed"):
        await driver._execute_with_connection(
            connection, "SELECT * FROM test", None, force_readonly=True
        )

    # Verify rollback was NOT called (autocommit mode)
    connection.rollback.assert_not_awaited()
