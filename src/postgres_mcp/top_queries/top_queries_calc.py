from typing import Union

from ..sql import SafeSqlDriver
from ..sql import SqlDriver
from ..sql.extension_utils import check_extension

PG_STAT_STATEMENTS = "pg_stat_statements"


class TopQueriesCalc:
    """Tool for retrieving the slowest SQL queries."""

    def __init__(self, sql_driver: Union[SqlDriver, SafeSqlDriver]):
        self.sql_driver = sql_driver

    async def get_top_queries(self, limit: int = 10) -> str:
        """Reports the slowest SQL queries based on total execution time.

        Args:
            limit: Number of slow queries to return

        Returns:
            A string with the top queries or installation instructions
        """
        try:
            extension_status = await check_extension(
                self.sql_driver,
                PG_STAT_STATEMENTS,
                include_messages=False,
            )

            if extension_status["is_installed"]:
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
                result_prefix = "Top {} slowest queries by total execution time:\n"
                result_text = result_prefix.format(len(slow_queries))
                result_text += str(slow_queries)
                return result_text
            else:
                # Use the message from extension_status with customization
                monitoring_message = (
                    f"The '{PG_STAT_STATEMENTS}' extension is required to report "
                    f"slow queries, but it is not currently installed.\n\n"
                    f"You can install it by running: "
                    f"`CREATE EXTENSION {PG_STAT_STATEMENTS};`\n\n"
                    f"**What does it do?** It records statistics (like execution "
                    f"time, number of calls, rows returned) for every query "
                    f"executed against the database.\n\n"
                    f"**Is it safe?** Installing '{PG_STAT_STATEMENTS}' is "
                    f"generally safe and a standard practice for performance "
                    f"monitoring. It adds overhead by tracking statistics, but "
                    f"this is usually negligible unless under extreme load."
                )
                return monitoring_message
        except Exception as e:
            return f"Error getting slow queries: {e}"
