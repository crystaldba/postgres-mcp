import asyncio
import sys
import functools
from typing import Any, Callable, TypeVar, Awaitable, List, Optional

from mcp.server.fastmcp import Context, FastMCP
import mcp.types as types
import psycopg2
from pydantic import AnyUrl

from .dta.dta_tools import DTATool
from .dta.safe_sql import SafeSqlDriver
from .dta.sql_driver import SqlDriver

mcp = FastMCP("postgres-mcp")

# Constants
SCHEMA_PATH = "schema"
EXTENSIONS_PATH = "_extensions"
PG_STAT_STATEMENTS = "pg_stat_statements"

# Type definitions for type hinting
T = TypeVar("T")
ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]


class DatabaseConnection:
    """Database connection manager."""

    def __init__(self, connection_url: Optional[str] = None):
        self.connection_url = connection_url
        self.conn = None

    def connect(self, connection_url: Optional[str] = None) -> Any:
        """Connect to the database."""
        url = connection_url or self.connection_url
        if not url:
            raise ValueError("Database connection URL not provided")
        try:
            self.conn = psycopg2.connect(url)
            return self.conn
        except Exception as e:
            print(f"Error connecting to database: {e}", file=sys.stderr)
            raise

    def get_connection(self) -> Any:
        """Get the database connection."""
        if not self.conn:
            raise ValueError("Database connection not initialized")
        return self.conn

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None


# Global connection manager
db_connection = DatabaseConnection()


def get_safe_sql_driver() -> SafeSqlDriver:
    """Get a SafeSqlDriver instance with the current connection."""
    return SafeSqlDriver(sql_driver=SqlDriver(conn=db_connection.get_connection()))


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def handle_db_errors(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Decorator to handle database operation errors."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            print(f"Error in {func.__name__}: {e}", file=sys.stderr)
            raise

    return wrapper


@mcp.resource("postgres://resources")
@handle_db_errors
async def list_resources() -> list[types.Resource]:
    """List available database tables as resources."""
    sql_driver = get_safe_sql_driver()
    rows = sql_driver.execute_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    tables = [row.cells["table_name"] for row in rows] if rows else []

    base_url = "postgres://"

    resources = [
        types.Resource(
            uri=AnyUrl(f"{base_url}/{table}/schema"),
            name=f'"{table}" database schema',
            description=f"Schema for table {table}",
            mimeType="application/json",
        )
        for table in tables
    ]

    extensions_uri = AnyUrl(f"{base_url}/_extensions")
    resources.append(
        types.Resource(
            uri=extensions_uri,
            name="Installed PostgreSQL Extensions",
            description="List of installed PostgreSQL extensions in the current database.",
            mimeType="application/json",
        )
    )

    return resources


@mcp.resource("postgres://_extensions")
@handle_db_errors
async def extensions_resource() -> str:
    """Get information about installed PostgreSQL extensions."""
    sql_driver = get_safe_sql_driver()
    rows = sql_driver.execute_query(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
    )
    extensions = [row.cells for row in rows] if rows else []
    return str(extensions)


@mcp.resource("postgres://{table_name}/schema")
@handle_db_errors
async def table_schema_resource(table_name: str) -> str:
    """Get schema information for a specific table."""
    sql_driver = get_safe_sql_driver()
    rows = SafeSqlDriver.execute_param_query(
        sql_driver,
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = {}
        """,
        [table_name],
    )
    columns = [row.cells for row in rows] if rows else []
    return str(columns)


@mcp.tool()
@handle_db_errors
async def query(sql: str) -> ResponseType:
    """Run a read-only SQL query."""
    sql_driver = get_safe_sql_driver()
    rows = sql_driver.execute_query(sql)
    if rows is None:
        return format_text_response("No results")
    return format_text_response(list([r.cells for r in rows]))


@mcp.tool()
@handle_db_errors
async def analyze_workload(max_index_size_mb: int = 10000) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    dta_tool = DTATool(get_safe_sql_driver())
    result = dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
    return format_text_response(result)


@mcp.tool()
@handle_db_errors
async def analyze_queries(
    queries: list[str], max_index_size_mb: int = 10000
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    dta_tool = DTATool(get_safe_sql_driver())
    result = dta_tool.analyze_queries(
        queries=queries, max_index_size_mb=max_index_size_mb
    )
    return format_text_response(result)


@mcp.tool()
@handle_db_errors
async def analyze_single_query(
    query: str, max_index_size_mb: int = 10000
) -> ResponseType:
    """Analyze a single SQL query and recommend optimal indexes."""
    dta_tool = DTATool(get_safe_sql_driver())
    result = dta_tool.analyze_single_query(
        query=query, max_index_size_mb=max_index_size_mb
    )
    return format_text_response(result)


@mcp.tool()
@handle_db_errors
async def list_installed_extensions(ctx: Context) -> ResponseType:
    """Lists all extensions currently installed in the PostgreSQL database."""
    extensions = await ctx.read_resource("postgres://_extensions")
    result_text = f"Installed PostgreSQL Extensions:\n{extensions}"
    return format_text_response(result_text)


@mcp.tool()
@handle_db_errors
async def install_extension_pg_stat_statements() -> ResponseType:
    """Installs the 'pg_stat_statements' extension if it's not already installed."""
    sql_driver = get_safe_sql_driver()
    sql_driver.execute_query(
        f"CREATE EXTENSION IF NOT EXISTS {PG_STAT_STATEMENTS};",
        force_readonly=False,
    )
    return format_text_response(
        f"Successfully ensured '{PG_STAT_STATEMENTS}' extension is installed."
    )


@mcp.tool()
@handle_db_errors
async def top_slow_queries(limit: int = 10) -> ResponseType:
    """Reports the slowest SQL queries based on total execution time."""
    sql_driver = get_safe_sql_driver()

    rows = SafeSqlDriver.execute_param_query(
        sql_driver,
        "SELECT 1 FROM pg_extension WHERE extname = {}",
        [PG_STAT_STATEMENTS],
    )
    extension_exists = len(rows) > 0 if rows else False

    if extension_exists:
        query = """
            SELECT
                query,
                calls,
                total_exec_time,
                mean_exec_time,
                rows
            FROM pg_stat_statements
            ORDER BY total_exec_time DESC
            LIMIT {};
        """
        slow_query_rows = SafeSqlDriver.execute_param_query(
            sql_driver,
            query,
            [limit],
        )
        slow_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
        result_text = (
            f"Top {len(slow_queries)} slowest queries by total execution time:\n"
        )
        result_text += str(slow_queries)
        return format_text_response(result_text)
    else:
        message = (
            f"The '{PG_STAT_STATEMENTS}' extension is required to report slow queries, but it is not currently installed.\n\n"
            f"You can ask me to install it using the 'install_extension_pg_stat_statements' tool.\n\n"
            f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is generally safe and a standard practice for performance monitoring. It adds performance overhead by tracking statistics, but this is usually negligible unless your server is under extreme load. It requires database privileges (often superuser) to install.\n\n"
            f"**What does it do?** It records statistics (like execution time, number of calls, rows returned) for every query executed against the database.\n\n"
            f"**How to undo?** If you later decide to remove it, you can ask me to run 'DROP EXTENSION {PG_STAT_STATEMENTS};'."
        )
        return format_text_response(message)


async def main():
    # Get database URL from command line
    if len(sys.argv) != 2:
        print(
            "Please provide a database URL as a command-line argument", file=sys.stderr
        )
        sys.exit(1)

    database_url = sys.argv[1]

    # Initialize database connection
    try:
        db_connection.connect(database_url)
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

    # Run the app with FastMCP's stdio method
    try:
        await mcp.run_stdio_async()
    finally:
        db_connection.close()


if __name__ == "__main__":
    asyncio.run(main())
