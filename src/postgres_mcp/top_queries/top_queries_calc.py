from typing import Literal
from typing import Union

from ..sql import SafeSqlDriver
from ..sql import SqlDriver
from ..sql.extension_utils import check_extension
from ..sql.extension_utils import get_postgres_version

PG_STAT_STATEMENTS = "pg_stat_statements"


class TopQueriesCalc:
    """Tool for retrieving the slowest SQL queries."""

    def __init__(self, sql_driver: Union[SqlDriver, SafeSqlDriver]):
        self.sql_driver = sql_driver

    async def get_top_queries(self, limit: int = 10, sort_by: Literal["total", "mean"] = "mean") -> str:
        """Reports the slowest SQL queries based on execution time.

        Args:
            limit: Number of slow queries to return
            sort_by: Sort criteria - 'total' for total execution time or
                'mean' for mean execution time per call (default)

        Returns:
            A string with the top queries or installation instructions
        """
        try:
            extension_status = await check_extension(
                self.sql_driver,
                PG_STAT_STATEMENTS,
                include_messages=False,
            )

            if not extension_status["is_installed"]:
                # Return installation instructions if the extension is not installed
                monitoring_message = (
                    f"The '{PG_STAT_STATEMENTS}' extension is required to "
                    f"report slow queries, but it is not currently "
                    f"installed.\n\n"
                    f"You can install it by running: "
                    f"`CREATE EXTENSION {PG_STAT_STATEMENTS};`\n\n"
                    f"**What does it do?** It records statistics (like "
                    f"execution time, number of calls, rows returned) for "
                    f"every query executed against the database.\n\n"
                    f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is "
                    f"generally safe and a standard practice for performance "
                    f"monitoring. It adds overhead by tracking statistics, "
                    f"but this is usually negligible unless under extreme load."
                )
                return monitoring_message

            # Check PostgreSQL version to determine column names
            pg_version = await get_postgres_version(self.sql_driver)

            # Column names changed in PostgreSQL 13
            if pg_version >= 13:
                # PostgreSQL 13 and newer
                total_time_col = "total_exec_time"
                mean_time_col = "mean_exec_time"
            else:
                # PostgreSQL 12 and older
                total_time_col = "total_time"
                mean_time_col = "mean_time"

            # Determine which column to sort by based on sort_by parameter and version
            order_by_column = total_time_col if sort_by == "total" else mean_time_col

            query = f"""
                SELECT
                    query,
                    calls,
                    {total_time_col},
                    {mean_time_col},
                    rows
                FROM pg_stat_statements
                ORDER BY {order_by_column} DESC
                LIMIT {{}};
            """
            slow_query_rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                query,
                [limit],
            )
            slow_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []

            # Create result description based on sort criteria
            if sort_by == "total":
                criteria = "total execution time"
            else:
                criteria = "mean execution time per call"

            result = f"Top {len(slow_queries)} slowest queries by {criteria}:\n"
            result += str(slow_queries)
            return result
        except Exception as e:
            return f"Error getting slow queries: {e}"
