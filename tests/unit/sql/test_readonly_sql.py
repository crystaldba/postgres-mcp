import asyncio
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from postgres_mcp.sql.readonly_sql import ReadOnlySqlDriver
from postgres_mcp.sql.sql_driver import SqlDriver


@pytest.fixture
def mock_sql_driver():
    """Create a mock base SqlDriver."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"test": "value"})])
    return driver


@pytest.mark.asyncio
async def test_readonly_driver_forces_readonly(mock_sql_driver):
    """Test that force_readonly=True is always passed, even if caller passes False."""
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)

    # Default call
    await readonly_driver.execute_query("SELECT 1")
    assert mock_sql_driver.execute_query.call_args[1]["force_readonly"] is True

    # Explicit False should still result in True
    mock_sql_driver.execute_query.reset_mock()
    await readonly_driver.execute_query("SELECT 1", force_readonly=False)
    assert mock_sql_driver.execute_query.call_args[1]["force_readonly"] is True

    # Explicit True
    mock_sql_driver.execute_query.reset_mock()
    await readonly_driver.execute_query("SELECT 1", force_readonly=True)
    assert mock_sql_driver.execute_query.call_args[1]["force_readonly"] is True


@pytest.mark.asyncio
async def test_readonly_driver_no_validation(mock_sql_driver):
    """Test that any SQL passes through without pglast validation (INSERT, DROP, etc.)."""
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)

    # These would be rejected by SafeSqlDriver's pglast validation,
    # but ReadOnlySqlDriver should pass them through (DB will reject at transaction level)
    dangerous_queries = [
        "INSERT INTO users (name) VALUES ('test')",
        "DROP TABLE users",
        "UPDATE users SET name = 'hacked'",
        "DELETE FROM users",
        "CREATE TABLE evil (id int)",
    ]

    for query in dangerous_queries:
        mock_sql_driver.execute_query.reset_mock()
        await readonly_driver.execute_query(query)
        assert mock_sql_driver.execute_query.call_count == 1


@pytest.mark.asyncio
async def test_readonly_driver_prepends_comment(mock_sql_driver):
    """Test that /* crystaldba */ prefix is added to queries."""
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)

    await readonly_driver.execute_query("SELECT 1")

    called_query = mock_sql_driver.execute_query.call_args[0][0]
    assert called_query == "/* crystaldba */ SELECT 1"


@pytest.mark.asyncio
async def test_readonly_driver_timeout(mock_sql_driver):
    """Test that timeout raises ValueError on expiry."""

    async def slow_query(*args, **kwargs):
        await asyncio.sleep(10)
        return [SqlDriver.RowResult(cells={"test": "value"})]

    mock_sql_driver.execute_query = slow_query

    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=0.01)

    with pytest.raises(ValueError, match=r"timed out.*readonly mode"):
        await readonly_driver.execute_query("SELECT pg_sleep(10)")


@pytest.mark.asyncio
async def test_readonly_driver_no_timeout(mock_sql_driver):
    """Test that queries work without timeout when timeout is None."""
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=None)

    result = await readonly_driver.execute_query("SELECT 1")
    assert result == [SqlDriver.RowResult(cells={"test": "value"})]
    assert mock_sql_driver.execute_query.call_args[1]["force_readonly"] is True


@pytest.mark.asyncio
async def test_readonly_driver_passes_params(mock_sql_driver):
    """Test that query parameters are forwarded correctly."""
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)

    params = ["param1", 42]
    await readonly_driver.execute_query("SELECT * FROM t WHERE a = $1 AND b = $2", params=params)

    call_kwargs = mock_sql_driver.execute_query.call_args[1]
    assert call_kwargs["params"] == params
    assert call_kwargs["force_readonly"] is True


@pytest.mark.asyncio
async def test_readonly_driver_forwards_exceptions(mock_sql_driver):
    """Test that exceptions from the underlying driver propagate."""
    mock_sql_driver.execute_query = AsyncMock(side_effect=RuntimeError("connection lost"))
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)
    with pytest.raises(RuntimeError, match="connection lost"):
        await readonly_driver.execute_query("SELECT 1")


@pytest.mark.asyncio
async def test_readonly_driver_none_result(mock_sql_driver):
    """Test that None result (DDL/no-result queries) is forwarded."""
    mock_sql_driver.execute_query = AsyncMock(return_value=None)
    readonly_driver = ReadOnlySqlDriver(sql_driver=mock_sql_driver, timeout=30)
    result = await readonly_driver.execute_query("VACUUM")
    assert result is None
