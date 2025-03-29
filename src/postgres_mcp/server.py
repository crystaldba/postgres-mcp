import asyncio
import os
import sys
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
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
EXTENSIONS_PATH = "_extensions"
PG_STAT_STATEMENTS = "pg_stat_statements"


def get_connection():
    global conn
    if not conn:
        raise ValueError("Database connection not initialized")
    return conn


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """List available database tables as resources."""
    try:
        with get_connection().cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            tables = cursor.fetchall()

            base_url = urlparse(os.environ.get("DATABASE_URL", ""))
            base_url = base_url._replace(scheme="postgres", password="")
            base_url = base_url.geturl()

            resources = [
                types.Resource(
                    uri=AnyUrl(f"{base_url}/{table[0]}/{SCHEMA_PATH}"),
                    name=f'"{table[0]}" database schema',
                    description=f"Schema for table {table[0]}",
                    mimeType="application/json",
                )
                for table in tables
            ]

            extensions_uri = AnyUrl(f"{base_url}/{EXTENSIONS_PATH}")
            resources.append(
                types.Resource(
                    uri=extensions_uri,
                    name="Installed PostgreSQL Extensions",
                    description="List of installed PostgreSQL extensions in the current database.",
                    mimeType="application/json",
                )
            )

            return resources
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

    if len(path_parts) == 1 and path_parts[0] == EXTENSIONS_PATH:
        try:
            with get_connection().cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT extname, extversion FROM pg_extension")
                extensions = cursor.fetchall()
                return str([dict(ext) for ext in extensions])
        except Exception as e:
            print(f"Error reading extensions resource: {e}", file=sys.stderr)
            raise

    elif len(path_parts) == 2 and path_parts[1] == SCHEMA_PATH:
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
    else:
        raise ValueError("Invalid resource URI")


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
        types.Tool(
            name="list_installed_extensions",
            description="Lists all extensions currently installed in the PostgreSQL database.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="install_extension_pg_stat_statements",
            description=f"Installs the '{PG_STAT_STATEMENTS}' extension if it's not already installed. This extension tracks execution statistics for all SQL statements executed in the database. Requires appropriate database privileges (often superuser).",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="top_slow_queries",
            description=f"Reports the slowest SQL queries based on total execution time, using data from the '{PG_STAT_STATEMENTS}' extension. If the extension is not installed, provides instructions on how to install it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of slowest queries to return.",
                        "default": 10,
                    },
                },
                "required": [],
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

    elif name == "list_installed_extensions":
        try:
            with get_connection().cursor(cursor_factory=RealDictCursor) as cursor:
                # Use read-only transaction for safety, although not strictly necessary for pg_extension
                cursor.execute("BEGIN TRANSACTION READ ONLY;")
                try:
                    cursor.execute(
                        "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
                    )
                    extensions = cursor.fetchall()
                    result_text = "Installed PostgreSQL Extensions:\n"
                    result_text += str([dict(ext) for ext in extensions])
                    return [types.TextContent(type="text", text=result_text)]
                finally:
                    cursor.execute("ROLLBACK;")
        except Exception as e:
            get_connection().rollback()
            print(f"Error listing extensions: {e}", file=sys.stderr)
            raise
    elif name == "install_extension_pg_stat_statements":
        try:
            with get_connection().cursor() as cursor:
                # Use IF NOT EXISTS to avoid errors if already installed
                cursor.execute(f"CREATE EXTENSION IF NOT EXISTS {PG_STAT_STATEMENTS};")
                get_connection().commit()  # Important: Commit the change
                return [
                    types.TextContent(
                        type="text",
                        text=f"Successfully ensured '{PG_STAT_STATEMENTS}' extension is installed.",
                    )
                ]
        except Exception as e:
            get_connection().rollback()
            print(f"Error installing {PG_STAT_STATEMENTS}: {e}", file=sys.stderr)
            error_message = f"Error installing '{PG_STAT_STATEMENTS}': {e}. This might be due to insufficient permissions. Superuser privileges are often required to create extensions."
            return [types.TextContent(type="text", text=error_message)]

    elif name == "top_slow_queries":
        limit = arguments.get("limit", 10) if arguments else 10
        conn = get_connection()

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = %s",
                    (PG_STAT_STATEMENTS,),
                )
                extension_exists = cursor.fetchone()

            if extension_exists:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute("BEGIN TRANSACTION READ ONLY;")
                    try:
                        query = """
                            SELECT
                                query,
                                calls,
                                total_exec_time,
                                mean_exec_time,
                                rows
                            FROM pg_stat_statements
                            ORDER BY total_exec_time DESC
                            LIMIT %s;
                        """
                        cursor.execute(query, (limit,))
                        slow_queries = cursor.fetchall()
                        result_text = f"Top {len(slow_queries)} slowest queries by total execution time:\n"
                        result_text += str([dict(q) for q in slow_queries])
                        return [types.TextContent(type="text", text=result_text)]
                    finally:
                        cursor.execute("ROLLBACK;")
            else:
                message = (
                    f"The '{PG_STAT_STATEMENTS}' extension is required to report slow queries, but it is not currently installed.\n\n"
                    f"You can ask me to install it using the 'install_extension_pg_stat_statements' tool.\n\n"
                    f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is generally safe and a standard practice for performance monitoring. It adds performance overhead by tracking statistics, but this is usually negligible unless your server is under extreme load. It requires database privileges (often superuser) to install.\n\n"
                    f"**What does it do?** It records statistics (like execution time, number of calls, rows returned) for every query executed against the database.\n\n"
                    f"**How to undo?** If you later decide to remove it, you can ask me to run 'DROP EXTENSION {PG_STAT_STATEMENTS};'."
                )
                return [types.TextContent(type="text", text=message)]

        except Exception as e:
            conn.rollback()
            print(
                f"Error checking or querying {PG_STAT_STATEMENTS}: {e}", file=sys.stderr
            )
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
