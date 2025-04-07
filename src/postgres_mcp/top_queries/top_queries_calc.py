from typing import Union

from ..sql import SafeSqlDriver
from ..sql import SqlDriver

PG_STAT_STATEMENTS = "pg_stat_statements"


class TopQueriesCalc:
    """Tool for retrieving the slowest SQL queries based on total execution time."""

    def __init__(self, sql_driver: Union[SqlDriver, SafeSqlDriver]):
        self.sql_driver = sql_driver

    async def is_pg_stat_statements_installed(self) -> bool:
        """Check if pg_stat_statements extension is installed.

        Returns:
            True if the extension is installed, False otherwise
        """
        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            "SELECT 1 FROM pg_extension WHERE extname = {}",
            [PG_STAT_STATEMENTS],
        )
        return len(rows) > 0 if rows else False

    async def get_top_queries(self, limit: int = 10) -> str:
        """Reports the slowest SQL queries based on total execution time.

        Args:
            limit: Number of slow queries to return

        Returns:
            A string with the top queries or installation instructions
        """
        try:
            extension_exists = await self.is_pg_stat_statements_installed()

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
                    self.sql_driver,
                    query,
                    [limit],
                )
                slow_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
                result_text = f"Top {len(slow_queries)} slowest queries by total execution time:\n"
                result_text += str(slow_queries)
                return result_text
            else:
                message = (
                    f"The '{PG_STAT_STATEMENTS}' extension is required to report slow queries, but it is not currently installed.\n\n"
                    f"You can ask me to install 'pg_stat_statements' using the 'execute_sql' tool.\n\n"
                    f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is generally safe and a standard practice for performance monitoring. "
                    f"It adds performance overhead by tracking statistics, but this is usually negligible unless your server is under extreme load. "
                    f"It requires database privileges (often superuser) to install.\n\n"
                    f"**What does it do?** It records statistics (like execution time, number of calls, rows returned) "
                    f"for every query executed against the database.\n\n"
                    f"**How to undo?** If you later decide to remove it, you can ask me to run 'DROP EXTENSION {PG_STAT_STATEMENTS};'."
                )
                return message
        except Exception as e:
            return f"Error getting slow queries: {e}"
