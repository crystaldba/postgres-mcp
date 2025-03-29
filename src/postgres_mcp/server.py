import asyncio
import os
import sys
from urllib.parse import urlparse

import psycopg2
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
from pydantic import AnyUrl
import mcp.server.stdio

from .dta.dta_tools import DTATool
from .dta.safe_sql import SafeSqlDriver
from .dta.sql_driver import SqlDriver

server = Server("postgres-mcp")

# Global connection pool
conn = None
SCHEMA_PATH = "schema"


def get_connection():
    global conn
    if not conn:
        raise ValueError("Database connection not initialized")
    return conn


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """List available database tables as resources."""
    try:
        sql_driver = SafeSqlDriver(sql_driver=SqlDriver(conn=get_connection()))
        rows = sql_driver.execute_query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tables = [row.cells["table_name"] for row in rows] if rows else []

        base_url = urlparse(os.environ.get("DATABASE_URL", ""))
        base_url = base_url._replace(scheme="postgres", password="")
        base_url = base_url.geturl()

        return [
            types.Resource(
                uri=AnyUrl(f"{base_url}/{table}/{SCHEMA_PATH}"),
                name=f'"{table}" database schema',
                description=f"Schema for table {table}",
                mimeType="application/json",
            )
            for table in tables
        ]

    except Exception as e:
        print(f"Error listing resources: {e}", file=sys.stderr)
        return []


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    """Read schema information for a specific table."""
    if uri.scheme != "postgres":
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

    if not uri.path:
        raise ValueError("Invalid resource URI")

    path_parts = uri.path.strip("/").split("/")
    if len(path_parts) != 2 or path_parts[1] != SCHEMA_PATH:
        raise ValueError("Invalid resource URI")

    table_name = path_parts[0]

    try:
        with get_connection().cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                [table_name],
            )
            columns = cursor.fetchall()
            return str(
                [{"column_name": col[0], "data_type": col[1]} for col in columns]
            )
    except Exception as e:
        print(f"Error reading resource: {e}", file=sys.stderr)
        raise


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available database tools."""
    return [
        types.Tool(
            name="query",
            description="Run a read-only SQL query",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="analyze_workload",
            description="Analyze frequently executed queries in the database and recommend optimal indexes",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_index_size_mb": {
                        "type": "integer",
                        "description": "Maximum total size for recommended indexes in MB",
                        "default": 10000,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="analyze_queries",
            description="Analyze a list of SQL queries and recommend optimal indexes",
            inputSchema={
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Two or more SQL queries to analyze",
                    },
                    "max_index_size_mb": {
                        "type": "integer",
                        "description": "Maximum total size for recommended indexes in MB",
                        "default": 10000,
                    },
                },
                "required": ["queries"],
            },
        ),
        types.Tool(
            name="analyze_single_query",
            description="Analyze a single SQL query and recommend optimal indexes",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL query to analyze",
                    },
                    "max_index_size_mb": {
                        "type": "integer",
                        "description": "Maximum total size for recommended indexes in MB",
                        "default": 10000,
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    if name == "query":
        if not arguments or "sql" not in arguments:
            raise ValueError("Missing SQL query")

        sql = arguments["sql"]

        try:
            sql_driver = SafeSqlDriver(sql_driver=SqlDriver(conn=get_connection()))
            rows = sql_driver.execute_query(sql)
            if rows is None:
                return [types.TextContent(type="text", text="No results")]
            return [
                types.TextContent(type="text", text=str(list([r.cells for r in rows])))
            ]
        except Exception as e:
            print(f"Error executing query: {e}", file=sys.stderr)
            raise

    elif name == "analyze_workload":
        max_index_size_mb = (
            arguments.get("max_index_size_mb", 10000) if arguments else 10000
        )

        try:
            dta_tool = DTATool(
                SafeSqlDriver(sql_driver=SqlDriver(conn=get_connection()))
            )
            result = dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
            return [types.TextContent(type="text", text=str(result))]
        except Exception as e:
            print(f"Error analyzing workload: {e}", file=sys.stderr)
            raise

    elif name == "analyze_queries":
        if not arguments or "queries" not in arguments:
            raise ValueError("Missing queries")

        queries = arguments["queries"]
        max_index_size_mb = arguments.get("max_index_size_mb", 10000)

        try:
            dta_tool = DTATool(
                SafeSqlDriver(sql_driver=SqlDriver(conn=get_connection()))
            )
            result = dta_tool.analyze_queries(
                queries=queries, max_index_size_mb=max_index_size_mb
            )
            return [types.TextContent(type="text", text=str(result))]
        except Exception as e:
            print(f"Error analyzing queries: {e}", file=sys.stderr)
            raise

    elif name == "analyze_single_query":
        if not arguments or "query" not in arguments:
            raise ValueError("Missing query")

        query = arguments["query"]
        max_index_size_mb = arguments.get("max_index_size_mb", 10000)

        try:
            dta_tool = DTATool(
                SafeSqlDriver(sql_driver=SqlDriver(conn=get_connection()))
            )
            result = dta_tool.analyze_single_query(
                query=query, max_index_size_mb=max_index_size_mb
            )
            return [types.TextContent(type="text", text=str(result))]
        except Exception as e:
            print(f"Error analyzing query: {e}", file=sys.stderr)
            raise

    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    # Get database URL from command line
    if len(sys.argv) != 2:
        print(
            "Please provide a database URL as a command-line argument", file=sys.stderr
        )
        sys.exit(1)

    database_url = sys.argv[1]

    # Initialize global connection
    global conn
    try:
        conn = psycopg2.connect(database_url)
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

    # Run the server using stdin/stdout streams
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        try:
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="postgres-mcp",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    asyncio.run(main())
