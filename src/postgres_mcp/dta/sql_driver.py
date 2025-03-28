"""SQL driver adapter for PostgreSQL connections."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

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
            self.conn = psycopg2.connect(engine_url)
        else:
            raise ValueError("Either conn or engine_url must be provided")

    def execute_query(self, query: str) -> Optional[List[RowResult]]:
        """
        Execute a query and return results.

        Args:
            query: SQL query to execute

        Returns:
            List of RowResult objects or None on error
        """
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Start read-only transaction
                cursor.execute("BEGIN TRANSACTION READ ONLY")
                try:
                    cursor.execute(query)
                    if cursor.description is None:  # No results (like DDL statements)
                        return None
                    rows = cursor.fetchall()
                    return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
                finally:
                    cursor.execute("ROLLBACK")
        except Exception as e:
            logger.error(f"Error executing query: {e}")
            self.conn.rollback()
            return None
