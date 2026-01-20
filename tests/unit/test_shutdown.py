import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_cleanup_closes_db_connection():
    """Test that cleanup properly closes database connections."""
    from postgres_mcp.server import cleanup

    mock_db = MagicMock()
    mock_db.close = AsyncMock()

    with patch("postgres_mcp.server.db_connection", mock_db):
        await cleanup()
        mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_handles_db_close_error():
    """Test that cleanup handles errors when closing database connections."""
    from postgres_mcp.server import cleanup

    mock_db = MagicMock()
    mock_db.close = AsyncMock(side_effect=Exception("Connection error"))

    with patch("postgres_mcp.server.db_connection", mock_db):
        # Should not raise, just log the error
        await cleanup()
        mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_main_calls_cleanup_on_normal_exit():
    """Test that main() calls cleanup when transport exits normally."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
        ]

        mock_cleanup = AsyncMock()

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock()),
            patch("postgres_mcp.server.cleanup", mock_cleanup),
        ):
            await main()
            mock_cleanup.assert_called_once()
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_main_calls_cleanup_on_exception():
    """Test that main() calls cleanup even when transport raises an exception."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
        ]

        mock_cleanup = AsyncMock()

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.mcp.run_stdio_async", AsyncMock(side_effect=Exception("Transport error"))),
            patch("postgres_mcp.server.cleanup", mock_cleanup),
        ):
            with pytest.raises(Exception, match="Transport error"):
                await main()
            # Cleanup should still be called due to finally block
            mock_cleanup.assert_called_once()
    finally:
        sys.argv = original_argv
