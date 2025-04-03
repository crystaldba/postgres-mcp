import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from postgres_mcp.server import AccessMode, get_sql_driver
from postgres_mcp.dta.safe_sql import SafeSqlDriver
from postgres_mcp.dta.sql_driver import SqlDriver, DbConnPool


@pytest.fixture
def mock_db_connection():
    """Mock database connection pool."""
    conn = MagicMock(spec=DbConnPool)
    conn.is_valid = True
    return conn


@pytest.mark.parametrize(
    "access_mode,expected_driver_type",
    [
        (AccessMode.YOLO, SqlDriver),
        (AccessMode.RESTRICTED, SafeSqlDriver),
    ],
)
@pytest.mark.asyncio
async def test_get_sql_driver_returns_correct_driver(
    access_mode, expected_driver_type, mock_db_connection
):
    """Test that get_sql_driver returns the correct driver type based on access mode."""
    with (
        patch("postgres_mcp.server.current_access_mode", access_mode),
        patch("postgres_mcp.server.db_connection", mock_db_connection),
    ):
        driver = await get_sql_driver()
        assert isinstance(driver, expected_driver_type)

        # When in RESTRICTED mode, verify timeout is set
        if access_mode == AccessMode.RESTRICTED:
            assert isinstance(driver, SafeSqlDriver)
            assert driver.timeout == 30


@pytest.mark.asyncio
async def test_get_sql_driver_sets_timeout_in_restricted_mode(mock_db_connection):
    """Test that get_sql_driver sets the timeout in restricted mode."""
    with (
        patch("postgres_mcp.server.current_access_mode", AccessMode.RESTRICTED),
        patch("postgres_mcp.server.db_connection", mock_db_connection),
    ):
        driver = await get_sql_driver()
        assert isinstance(driver, SafeSqlDriver)
        assert driver.timeout == 30
        assert hasattr(driver, "sql_driver")


@pytest.mark.asyncio
async def test_get_sql_driver_in_yolo_mode_no_timeout(mock_db_connection):
    """Test that get_sql_driver in yolo mode is a regular SqlDriver."""
    with (
        patch("postgres_mcp.server.current_access_mode", AccessMode.YOLO),
        patch("postgres_mcp.server.db_connection", mock_db_connection),
    ):
        driver = await get_sql_driver()
        assert isinstance(driver, SqlDriver)
        assert not hasattr(driver, "timeout")


@pytest.mark.asyncio
async def test_command_line_parsing():
    """Test that command-line arguments correctly set the access mode."""
    from postgres_mcp.server import main
    import sys

    # Mock sys.argv and asyncio.run
    original_argv = sys.argv
    original_run = asyncio.run

    try:
        # Test with --access-mode=restricted
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--access-mode=restricted",
        ]
        asyncio.run = AsyncMock()

        with (
            patch("postgres_mcp.server.current_access_mode", AccessMode.YOLO),
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()),
            patch("postgres_mcp.server.shutdown", AsyncMock()),
        ):
            # Reset the current_access_mode to YOLO
            import postgres_mcp.server

            postgres_mcp.server.current_access_mode = AccessMode.YOLO

            # Run main (partially mocked to avoid actual connection)
            try:
                await main()
            except Exception:
                pass

            # Verify the mode was changed to RESTRICTED
            assert postgres_mcp.server.current_access_mode == AccessMode.RESTRICTED

    finally:
        # Restore original values
        sys.argv = original_argv
        asyncio.run = original_run
