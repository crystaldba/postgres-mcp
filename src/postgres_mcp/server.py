import asyncio
import sys
import signal
import logging
import argparse
from enum import Enum
from typing import Any, List, Union

from mcp.server.fastmcp import FastMCP
import mcp.types as types
from pydantic import AnyUrl, Field
import psycopg
from .sql import DbConnPool, obfuscate_password, SqlDriver

from .dta import DTATool
from .sql import SafeSqlDriver, check_hypopg_installation_status
from .database_health import DatabaseHealthTool, HealthType
from .dta import MAX_NUM_DTA_QUERIES_LIMIT
from .explain import ExplainPlanTool
from .explain import ExplainPlanArtifact, ErrorResult

mcp = FastMCP("postgres-mcp")

# Constants
PG_STAT_STATEMENTS = "pg_stat_statements"
HYPOPG_EXTENSION = "hypopg"

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]

logger = logging.getLogger(__name__)


class AccessMode(str, Enum):
    """SQL access modes for the server."""

    UNRESTRICTED = "unrestricted"  # Unrestricted access
    RESTRICTED = "restricted"  # Read-only with safety features


# Global variables
db_connection = DbConnPool()
current_access_mode = AccessMode.UNRESTRICTED


async def get_sql_driver() -> Union[SqlDriver, SafeSqlDriver]:
    """Get the appropriate SQL driver based on the current access mode."""
    base_driver = SqlDriver(conn=db_connection)

    if current_access_mode == AccessMode.RESTRICTED:
        logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)  # 30 second timeout
    else:
        logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
        return base_driver


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {error}")


@mcp.resource(
    name="list_resources",
    uri="postgres-mcp://resources",
    description="List available resources, such as database tables and extensions",
)
async def list_resources() -> list[types.Resource]:
    """List available database tables as resources."""
    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tables = [row.cells["table_name"] for row in rows] if rows else []

        base_url = "postgres-mcp://"

        resources = [
            types.Resource(
                uri=AnyUrl(f"{base_url}/{table}/schema"),
                name=f'"{table}" database schema',
                description=f"Schema for table {table}",
                mimeType="application/json",
            )
            for table in tables
        ]

        extensions_uri = AnyUrl(f"{base_url}/extensions")
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
        logger.error(f"Error listing resources: {e}")
        raise


@mcp.resource(
    name="list_extensions",
    uri="postgres-mcp://extensions",
    description="List available and installed extensions",
)
async def extensions_resource() -> str:
    """Get information about installed PostgreSQL extensions."""
    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(
            """
            SELECT
                pae.name AS extname,
                CASE WHEN pe.extversion IS NOT NULL THEN true ELSE false END AS installed,
                pe.extversion AS installed_version,
                pae.default_version,
                pae.comment
            FROM
                pg_available_extensions pae
            LEFT JOIN
                pg_extension pe
                ON pae.name = pe.extname
            ORDER BY
                pae.name;
            """
        )
        extensions = [row.cells for row in rows] if rows else []
        return str(extensions)
    except Exception as e:
        logger.error(f"Error listing extensions: {e}")
        raise


@mcp.resource(
    name="list_table_columns",
    uri="postgres-mcp://{table_name}/schema",
    description="Show columns for the table",
)
async def table_schema_resource(table_name: str) -> str:
    """Get schema information for a specific table."""
    try:
        sql_driver = await get_sql_driver()
        rows = await SafeSqlDriver.execute_param_query(
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
    except Exception as e:
        logger.error(f"Error getting table schema: {e}")
        raise


@mcp.tool(
    description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates."
)
async def explain_query(
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] | None = Field(
        description="""Optional list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]""",
        default=None,
    ),
) -> ResponseType:
    """
    Explains the execution plan for a SQL query.

    Args:
        sql: The SQL query to explain
        analyze: When True, actually runs the query for real statistics
        hypothetical_indexes: Optional list of indexes to simulate
    """
    try:
        sql_driver = await get_sql_driver()
        explain_tool = ExplainPlanTool(sql_driver=sql_driver)
        result: ExplainPlanArtifact | ErrorResult | None = None

        # If hypothetical indexes are specified, check for HypoPG extension
        if hypothetical_indexes:
            try:
                # Use the common utility function to check if hypopg is installed
                (
                    is_hypopg_installed,
                    hypopg_message,
                ) = await check_hypopg_installation_status(sql_driver)

                # If hypopg is not installed, return the message
                if not is_hypopg_installed:
                    return format_text_response(hypopg_message)

                # HypoPG is installed, proceed with explaining with hypothetical indexes
                result = await explain_tool.explain_with_hypothetical_indexes(
                    sql, hypothetical_indexes
                )
            except Exception:
                raise  # Re-raise the original exception
        elif analyze:
            try:
                # Use EXPLAIN ANALYZE
                result = await explain_tool.explain_analyze(sql)
            except Exception:
                raise  # Re-raise the original exception
        else:
            try:
                # Use basic EXPLAIN
                result = await explain_tool.explain(sql)
            except Exception:
                raise  # Re-raise the original exception

        if result and isinstance(result, ExplainPlanArtifact):
            return format_text_response(result.to_text())
        else:
            return format_error_response(
                "Error processing explain plan: " + result.to_text()
            )
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


@mcp.tool(description="Run a read-only SQL query")
async def query(
    sql: str = Field(description="SQL to run", default="all"),
) -> ResponseType:
    """Run a read-only SQL query."""
    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(sql)  # type: ignore
        if rows is None:
            return format_text_response("No results")
        return format_text_response(list([r.cells for r in rows]))
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze frequently executed queries in the database and recommend optimal indexes"
)
async def analyze_workload(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        sql_driver = await get_sql_driver()
        dta_tool = DTATool(sql_driver)
        result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes"
)
async def analyze_queries(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    if len(queries) == 0:
        return format_error_response(
            "Please provide a non-empty list of queries to analyze."
        )
    if len(queries) > MAX_NUM_DTA_QUERIES_LIMIT:
        return format_error_response(
            f"Please provide a list of up to {MAX_NUM_DTA_QUERIES_LIMIT} queries to analyze."
        )

    try:
        sql_driver = await get_sql_driver()
        dta_tool = DTATool(sql_driver)
        result = await dta_tool.analyze_queries(
            queries=queries, max_index_size_mb=max_index_size_mb
        )
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyzes database health for specified components including buffer cache hit rates, "
    "identifies duplicate, unused, or invalid indexes, sequence health, constraint health "
    "vacuum health, and connection health."
)
async def database_health(
    health_type: str = Field(
        description=f"Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
        default="all",
    ),
) -> ResponseType:
    """Analyze database health for specified components.

    Args:
        health_type: Comma-separated list of health check types to perform.
                    Valid values: index, connection, vacuum, sequence, replication, buffer, constraint, all
    """
    health_tool = DatabaseHealthTool(await get_sql_driver())
    result = await health_tool.health(health_type=health_type)
    return format_text_response(result)


@mcp.tool(
    description="Lists all extensions currently installed in the PostgreSQL database."
)
async def list_installed_extensions(ctx) -> ResponseType:
    """Lists all extensions currently installed in the PostgreSQL database."""
    try:
        extensions = await ctx.read_resource("postgres-mcp://extensions")
        result_text = f"Installed PostgreSQL Extensions:\n{extensions}"
        return format_text_response(result_text)
    except Exception as e:
        logger.error(f"Error listing installed extensions: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Installs a PostgreSQL extension if it's available but not already installed. Requires appropriate database privileges (often superuser)."
)
async def install_extension(
    extension_name: str = Field(
        description="Extension to install. e.g. pg_stat_statements"
    ),
) -> ResponseType:
    """Installs a PostgreSQL extension if it's available but not already installed. Requires appropriate database privileges (often superuser)."""

    try:
        # First check if the extension exists in pg_available_extensions
        sql_driver = await get_sql_driver()
        check_rows = await SafeSqlDriver.execute_param_query(
            sql_driver,
            "SELECT name, default_version FROM pg_available_extensions WHERE name = {}",
            [extension_name],
        )

        if not check_rows:
            return format_text_response(
                f"Error: Extension '{extension_name}' is not available in the PostgreSQL installation. Please check if the extension is properly installed on the server."
            )

        # Check if extension is already installed
        installed_rows = await SafeSqlDriver.execute_param_query(
            sql_driver,
            "SELECT extversion FROM pg_extension WHERE extname = {}",
            [extension_name],
        )

        if installed_rows:
            return format_text_response(
                f"Extension '{extension_name}' version {installed_rows[0].cells['extversion']} is already installed."
            )

        # Attempt to create the extension
        await sql_driver.execute_query(
            f"CREATE EXTENSION {extension_name}",  # type: ignore
            # NOTE: cannot escape because an escaped extension_name is invalid SQL
            force_readonly=False,
        )

        return format_text_response(
            f"Successfully installed '{extension_name}' extension.",
        )
    except psycopg.OperationalError as e:
        error_msg = (
            f"Error installing '{extension_name}': {e}\n\n"
            "This is likely due to insufficient permissions. The following are common causes:\n"
            "1. The database user lacks superuser privileges\n"
            "2. The extension is not available in the PostgreSQL installation\n"
            "3. The extension requires additional system-level dependencies\n\n"
            "Please ensure you have the necessary permissions and the extension is available on your PostgreSQL server."
        )
        return format_error_response(error_msg)
    except Exception as e:
        error_msg = (
            f"Unexpected error installing '{extension_name}': {e}\n\n"
            "Please check the error message and ensure all prerequisites are met."
        )
        return format_error_response(error_msg)


@mcp.tool(
    description=f"Reports the slowest SQL queries based on total execution time, using data from the '{PG_STAT_STATEMENTS}' extension. If the extension is not installed, provides instructions on how to install it."
)
async def top_slow_queries(
    limit: int = Field(description="Number of slow queries to return", default=10),
) -> ResponseType:
    """Reports the slowest SQL queries based on total execution time."""
    try:
        sql_driver = await get_sql_driver()

        rows = await SafeSqlDriver.execute_param_query(
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
            slow_query_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                query,
                [limit],
            )
            slow_queries = (
                [row.cells for row in slow_query_rows] if slow_query_rows else []
            )
            result_text = (
                f"Top {len(slow_queries)} slowest queries by total execution time:\n"
            )
            result_text += str(slow_queries)
            return format_text_response(result_text)
        else:
            message = (
                f"The '{PG_STAT_STATEMENTS}' extension is required to report slow queries, but it is not currently installed.\n\n"
                f"You can ask me to install 'pg_stat_statements' using the 'install_extension' tool.\n\n"
                f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is generally safe and a standard practice for performance monitoring. It adds performance overhead by tracking statistics, but this is usually negligible unless your server is under extreme load. It requires database privileges (often superuser) to install.\n\n"
                f"**What does it do?** It records statistics (like execution time, number of calls, rows returned) for every query executed against the database.\n\n"
                f"**How to undo?** If you later decide to remove it, you can ask me to run 'DROP EXTENSION {PG_STAT_STATEMENTS};'."
            )
            return format_text_response(message)
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("database_url", help="Database connection URL")
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="Set SQL access mode: unrestricted (unrestricted) or restricted (read-only with protections)",
    )

    args = parser.parse_args()

    # Store the access mode in the global variable
    global current_access_mode
    current_access_mode = AccessMode(args.access_mode)

    logger.info(f"Starting PostgreSQL MCP Server in {current_access_mode.upper()} mode")

    database_url = args.database_url

    # Initialize database connection pool
    try:
        await db_connection.pool_connect(database_url)
        logger.info(
            "Successfully connected to database and initialized connection pool"
        )
    except Exception as e:
        print(
            f"Warning: Could not connect to database: {obfuscate_password(str(e))}",
            file=sys.stderr,
        )
        print(
            "The MCP server will start but database operations will fail until a valid connection is established.",
            file=sys.stderr,
        )

    # Set up proper shutdown handling
    try:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s)))
    except NotImplementedError:
        # Windows doesn't support signals properly
        logger.warning("Signal handling not supported on Windows")
        pass

    # Run the app with FastMCP's stdio method
    try:
        await mcp.run_stdio_async()
    finally:
        # Close the connection pool when exiting
        await shutdown()


async def shutdown(sig=None):
    """Clean shutdown of the server."""
    if sig:
        logger.info(f"Received exit signal {sig.name}")

    logger.info("Closing database connections...")
    await db_connection.close()

    # Give tasks a chance to complete
    try:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Waiting for {len(tasks)} tasks to complete...")
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.warning(f"Error during shutdown: {e}")

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
