"""Round-trip tests that exercise the MCP tools through a real MCP client session.

Everything else in the suite tests the Python functions behind the tools. These tests
go through the protocol layer instead: an in-process FastMCP server wired to a
ClientSession over the SDK's memory transport, backed by the real PostgreSQL
container the rest of the integration tests use. They cover tool discovery
(list_tools), argument schemas, call_tool round trips for every registered tool,
access-mode-dependent registration of execute_sql, and how errors surface to a client.

The client session is opened inside each test rather than in a fixture: the SDK's
memory transport uses anyio cancel scopes, which must be entered and exited in the
same task, and pytest-asyncio tears async fixtures down in a different task.
"""

import ast
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from typing import AsyncIterator

import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult
from mcp.types import TextContent

import postgres_mcp.server as server
from postgres_mcp.server import AccessMode
from postgres_mcp.server import configure_access_mode

logger = logging.getLogger(__name__)

EXPECTED_TOOLS = {
    "list_schemas",
    "list_objects",
    "get_object_details",
    "explain_query",
    "execute_sql",
    "analyze_workload_indexes",
    "analyze_query_indexes",
    "analyze_db_health",
    "get_top_queries",
}

TEST_TABLE = "mcp_protocol_test_items"


def _text(result: CallToolResult) -> str:
    """Concatenate the text content blocks of a tool result."""
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


@asynccontextmanager
async def mcp_session(access_mode: AccessMode) -> AsyncIterator[ClientSession]:
    """An initialised MCP client talking to the server module's FastMCP instance over the memory transport."""
    configure_access_mode(access_mode)
    async with create_connected_server_and_client_session(server.mcp) as session:
        yield session


@pytest_asyncio.fixture
async def connected_db(test_postgres_connection_string) -> AsyncGenerator[str, None]:
    """Point the server module's global connection pool at the test database and seed a table."""
    connection_string, version = test_postgres_connection_string
    logger.info(f"MCP protocol tests against PostgreSQL {version}")

    pool = await server.db_connection.pool_connect(connection_string)
    async with pool.connection() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
        await conn.execute(f"CREATE TABLE {TEST_TABLE} (id serial PRIMARY KEY, name text NOT NULL, qty integer)")
        await conn.execute(f"INSERT INTO {TEST_TABLE} (name, qty) VALUES ('apple', 3), ('pear', 5)")
        await conn.commit()
    try:
        yield connection_string
    finally:
        async with pool.connection() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
            await conn.commit()
        await server.db_connection.close()


class TestToolDiscovery:
    @pytest.mark.asyncio
    async def test_list_tools_exposes_every_tool(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_every_tool_has_description_and_object_schema(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            tools = (await session.list_tools()).tools
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"
            assert tool.inputSchema.get("type") == "object", f"{tool.name} schema is not an object"

    @pytest.mark.asyncio
    async def test_required_arguments_are_marked_required(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert "sql" in tools["explain_query"].inputSchema["required"]
        assert "schema_name" in tools["list_objects"].inputSchema["required"]
        assert set(tools["get_object_details"].inputSchema["required"]) >= {"schema_name", "object_name"}
        assert "queries" in tools["analyze_query_indexes"].inputSchema["required"]
        # Tools whose arguments all have defaults advertise nothing as required.
        assert not tools["list_schemas"].inputSchema.get("required")
        assert not tools["analyze_db_health"].inputSchema.get("required")

    @pytest.mark.asyncio
    async def test_execute_sql_is_read_only_in_restricted_mode(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        execute_sql = tools["execute_sql"]
        assert execute_sql.description == "Execute a read-only SQL query"
        assert execute_sql.annotations is not None
        assert execute_sql.annotations.title == "Execute SQL (Read-Only)"
        assert execute_sql.annotations.readOnlyHint is True
        assert execute_sql.annotations.destructiveHint is not True

    @pytest.mark.asyncio
    async def test_execute_sql_is_destructive_in_unrestricted_mode(self, connected_db):
        async with mcp_session(AccessMode.UNRESTRICTED) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        execute_sql = tools["execute_sql"]
        assert execute_sql.description == "Execute any SQL query"
        assert execute_sql.annotations is not None
        assert execute_sql.annotations.title == "Execute SQL"
        assert execute_sql.annotations.destructiveHint is True
        assert execute_sql.annotations.readOnlyHint is not True

    @pytest.mark.asyncio
    async def test_switching_access_mode_replaces_execute_sql(self, connected_db):
        """configure_access_mode can be called repeatedly without duplicating or stacking registrations."""
        async with mcp_session(AccessMode.UNRESTRICTED) as session:
            first = {tool.name: tool for tool in (await session.list_tools()).tools}
        async with mcp_session(AccessMode.RESTRICTED) as session:
            tools = (await session.list_tools()).tools
        assert [tool.name for tool in tools].count("execute_sql") == 1
        second = {tool.name: tool for tool in tools}
        assert first["execute_sql"].description != second["execute_sql"].description
        assert second["execute_sql"].description == "Execute a read-only SQL query"


class TestToolCalls:
    @pytest.mark.asyncio
    async def test_list_schemas(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("list_schemas", {})
        assert not result.isError
        assert "public" in _text(result)

    @pytest.mark.asyncio
    async def test_list_objects_sees_seeded_table(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("list_objects", {"schema_name": "public", "object_type": "table"})
        assert not result.isError
        assert TEST_TABLE in _text(result)

    @pytest.mark.asyncio
    async def test_get_object_details_returns_columns(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool(
                "get_object_details",
                {"schema_name": "public", "object_name": TEST_TABLE, "object_type": "table"},
            )
        assert not result.isError
        text = _text(result)
        for column in ("id", "name", "qty"):
            assert column in text

    @pytest.mark.asyncio
    async def test_execute_sql_returns_rows(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("execute_sql", {"sql": f"SELECT name, qty FROM {TEST_TABLE} ORDER BY id"})
        assert not result.isError
        rows = ast.literal_eval(_text(result))
        assert rows == [{"name": "apple", "qty": 3}, {"name": "pear", "qty": 5}]

    @pytest.mark.asyncio
    async def test_explain_query_returns_plan(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("explain_query", {"sql": f"SELECT * FROM {TEST_TABLE} WHERE qty > 1"})
        assert not result.isError
        text = _text(result)
        assert "Plan" in text or "Seq Scan" in text

    @pytest.mark.asyncio
    async def test_analyze_db_health_runs(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("analyze_db_health", {"health_type": "index"})
        assert not result.isError
        assert _text(result).strip()

    @pytest.mark.asyncio
    async def test_analyze_query_indexes_runs(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool(
                "analyze_query_indexes",
                {"queries": [f"SELECT * FROM {TEST_TABLE} WHERE name = 'apple'"]},
            )
        assert not result.isError
        assert _text(result).strip()

    @pytest.mark.asyncio
    async def test_analyze_workload_indexes_runs(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("analyze_workload_indexes", {})
        assert not result.isError
        assert _text(result).strip()

    @pytest.mark.asyncio
    async def test_get_top_queries_runs(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("get_top_queries", {"sort_by": "total_time", "limit": 5})
        assert not result.isError
        assert _text(result).strip()


class TestErrorsThroughTheProtocol:
    @pytest.mark.asyncio
    async def test_restricted_mode_blocks_writes_and_leaves_data_intact(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("execute_sql", {"sql": f"DELETE FROM {TEST_TABLE}"})
            check = await session.call_tool("execute_sql", {"sql": f"SELECT count(*) AS n FROM {TEST_TABLE}"})
        # Tool-level failures are reported in the content, not as a protocol-level error.
        assert _text(result).startswith("Error:")
        assert not check.isError
        assert ast.literal_eval(_text(check)) == [{"n": 2}]

    @pytest.mark.asyncio
    async def test_unrestricted_mode_allows_writes(self, connected_db):
        async with mcp_session(AccessMode.UNRESTRICTED) as session:
            result = await session.call_tool("execute_sql", {"sql": f"UPDATE {TEST_TABLE} SET qty = qty + 1 WHERE name = 'apple'"})
            check = await session.call_tool("execute_sql", {"sql": f"SELECT qty FROM {TEST_TABLE} WHERE name = 'apple'"})
        assert not result.isError
        assert not _text(result).startswith("Error:")
        assert ast.literal_eval(_text(check)) == [{"qty": 4}]

    @pytest.mark.asyncio
    async def test_invalid_sql_is_reported_not_raised(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("execute_sql", {"sql": "SELECT * FROM table_that_does_not_exist"})
        assert _text(result).startswith("Error:")

    @pytest.mark.asyncio
    async def test_missing_required_argument_is_a_tool_error(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("explain_query", {})
        assert result.isError
        assert "sql" in _text(result)

    @pytest.mark.asyncio
    async def test_unknown_tool_is_a_tool_error(self, connected_db):
        async with mcp_session(AccessMode.RESTRICTED) as session:
            result = await session.call_tool("no_such_tool", {})
        assert result.isError
        assert "no_such_tool" in _text(result)
