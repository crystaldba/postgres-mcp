from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import Optional

from typing_extensions import LiteralString

from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


class ReadOnlySqlDriver(SqlDriver):
    """A wrapper around any SqlDriver that enforces read-only mode at the database level.

    Unlike SafeSqlDriver, this driver does NOT perform pglast SQL validation.
    Instead, it relies on PostgreSQL's READ ONLY transaction mode to prevent writes.
    This allows complex but safe read-only queries (nested CTEs, PERCENTILE_CONT
    WITHIN GROUP, complex window functions, etc.) that pglast may reject.
    """

    def __init__(self, sql_driver: SqlDriver, timeout: float | None = None):
        """Initialize with an underlying SQL driver and optional timeout.

        Args:
            sql_driver: The underlying SQL driver to wrap
            timeout: Optional timeout in seconds for query execution
        """
        self.sql_driver = sql_driver
        self.timeout = timeout

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,  # do not use value passed in
    ) -> Optional[list[SqlDriver.RowResult]]:  # noqa: UP007
        """Execute a query with forced read-only mode, without SQL validation."""
        # NOTE: Always force readonly=True in ReadOnlySqlDriver regardless of what was passed
        if self.timeout:
            try:
                async with asyncio.timeout(self.timeout):
                    return await self.sql_driver.execute_query(
                        f"/* crystaldba */ {query}",
                        params=params,
                        force_readonly=True,
                    )
            except asyncio.TimeoutError as e:
                logger.warning(f"Query execution timed out after {self.timeout} seconds: {query[:100]}...")
                raise ValueError(
                    f"Query execution timed out after {self.timeout} seconds in readonly mode. "
                    "Consider simplifying your query or increasing the timeout."
                ) from e
            except Exception as e:
                logger.error(f"Error executing query: {e}")
                raise
        else:
            return await self.sql_driver.execute_query(
                f"/* crystaldba */ {query}",
                params=params,
                force_readonly=True,
            )
