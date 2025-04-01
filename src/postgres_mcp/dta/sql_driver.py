"""SQL driver adapter for PostgreSQL connections."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from typing_extensions import LiteralString
from psycopg.rows import dict_row


import psycopg

logger = logging.getLogger(__name__)


class SqlDriver:
    """Adapter class that wraps a PostgreSQL connection with the interface expected by DTA."""

    @dataclass
    class RowResult:
        """Simple class to match the Griptape RowResult interface."""

        cells: Dict[str, Any]

    def __init__(self, conn: Any = None, engine_url: str | None = None):
        """
        Initialize with a PostgreSQL connection.

        Args:
            conn: PostgreSQL connection object
        """
        if conn:
            self.conn = conn
        elif engine_url:
            # Don't connect here since we need async connection
            self.engine_url = engine_url
            self.conn = None
        else:
            raise ValueError("Either conn or engine_url must be provided")

    async def connect(self):
        if self.conn is not None:
            return
        if self.engine_url:
            self.conn = await psycopg.AsyncConnection.connect(self.engine_url)
        else:
            raise ValueError(
                "Connection not established. Either conn or engine_url must be provided"
            )

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,
    ) -> Optional[List[RowResult]]:
        """
        Execute a query and return results.

        Args:
            query: SQL query to execute
            params: Query parameters
            force_readonly: Whether to enforce read-only mode

        Returns:
            List of RowResult objects or None on error
        """
        try:
            await self.connect()
            if self.conn is None:
                raise ValueError("Connection not established")
            async with self.conn.cursor(row_factory=dict_row) as cursor:
                # Start read-only transaction
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                try:
                    if params:
                        await cursor.execute(query, params)
                    else:
                        await cursor.execute(query)

                    # For multiple statements, move to the last statement's results
                    while cursor.nextset():
                        pass

                    if cursor.description is None:  # No results (like DDL statements)
                        if not force_readonly:
                            await cursor.execute("COMMIT")
                        return None

                    # Get results from the last statement only
                    rows = await cursor.fetchall()

                    if not force_readonly:
                        await cursor.execute("COMMIT")

                    return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
                finally:
                    if force_readonly:
                        await cursor.execute("ROLLBACK")
        except Exception as e:
            logger.error(f"Error executing query ({query}): {e}")
            if self.conn:
                try:
                    await self.conn.rollback()
                except Exception as e:
                    logger.error(f"Error rolling back transaction: {e}")
                self.conn = None
            raise e
