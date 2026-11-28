from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

import postgres_mcp.server as server


def _server_with_execute_sql() -> FastMCP:
    app = FastMCP("test")
    app.add_tool(server.execute_sql, description="Execute SQL")
    return app


@pytest.mark.asyncio
async def test_failed_query_is_flagged_as_error():
    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=ValueError("Error validating query"))

    with patch("postgres_mcp.server.get_sql_driver", AsyncMock(return_value=driver)):
        async with create_connected_server_and_client_session(_server_with_execute_sql()) as client:
            result = await client.call_tool("execute_sql", {"sql": "CREATE TABLE t(i int)"})

    assert result.isError is True
    first = result.content[0]
    assert isinstance(first, TextContent)
    assert "Error validating query" in first.text


@pytest.mark.asyncio
async def test_successful_query_is_not_flagged_as_error():
    row = MagicMock()
    row.cells = {"n": 1}
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[row])

    with patch("postgres_mcp.server.get_sql_driver", AsyncMock(return_value=driver)):
        async with create_connected_server_and_client_session(_server_with_execute_sql()) as client:
            result = await client.call_tool("execute_sql", {"sql": "SELECT 1 AS n"})

    assert result.isError is False
    first = result.content[0]
    assert isinstance(first, TextContent)
    assert "'n': 1" in first.text
