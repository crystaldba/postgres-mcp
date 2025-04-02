"""SQL driver adapter for PostgreSQL connections."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from typing_extensions import LiteralString
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from urllib.parse import urlparse, urlunparse
import asyncio


logger = logging.getLogger(__name__)

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds


def obfuscate_password(url: str) -> str:
    """Obfuscate password in connection URL."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            # Replace password with asterisks
            netloc = parsed.netloc.replace(parsed.password, "****")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        # If we can't parse the URL (e.g., it's not a URL), just return it as is
        pass
    return url


class DbConnPool:
    """Database connection manager using psycopg's connection pool."""

    def __init__(self, connection_url: Optional[str] = None):
        self.connection_url = connection_url
        self.pool = None
        self._is_valid = False
        self._last_error = None

    async def pool_connect(
        self, connection_url: Optional[str] = None
    ) -> AsyncConnectionPool:
        """Initialize connection pool with retry logic."""
        # If we already have a valid pool, return it
        if self.pool and self._is_valid:
            return self.pool

        url = connection_url or self.connection_url
        self.connection_url = url
        if not url:
            self._is_valid = False
            self._last_error = "Database connection URL not provided"
            raise ValueError(self._last_error)

        # Close any existing pool before creating a new one
        await self.close()

        for attempt in range(MAX_RETRIES):
            try:
                # Configure connection pool with appropriate settings
                self.pool = AsyncConnectionPool(
                    conninfo=url,
                    min_size=1,
                    max_size=5,
                    open=False,  # Don't connect immediately, let's do it explicitly
                )

                # Open the pool explicitly
                await self.pool.open()

                # Test the connection pool by executing a simple query
                async with self.pool.connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")

                self._is_valid = True
                self._last_error = None
                return self.pool
            except Exception as e:
                self._is_valid = False
                self._last_error = str(e)

                # Clean up failed pool
                await self.close()

                if attempt < MAX_RETRIES - 1:
                    logger.warning(
                        f"Connection attempt {attempt + 1} failed: {obfuscate_password(str(e))}. Retrying in {RETRY_DELAY} seconds..."
                    )
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.error(
                        f"All connection attempts failed: {obfuscate_password(str(e))}"
                    )
                    raise

    async def get_pool(self) -> Any:
        """Get a connection from the pool, initializing the pool if needed."""
        if not self.pool or not self._is_valid:
            return await self.pool_connect()

        return self.pool

    async def close(self) -> None:
        """Close the connection pool."""
        if self.pool:
            try:
                await self.pool.close()
            except Exception as e:
                logger.warning(f"Error closing connection pool: {e}")
            self.pool = None
            self._is_valid = False

    @property
    def is_valid(self) -> bool:
        """Check if the connection pool is valid."""
        return self._is_valid

    @property
    def last_error(self) -> Optional[str]:
        """Get the last error message."""
        return self._last_error


class SqlDriver:
    """Adapter class that wraps a PostgreSQL connection with the interface expected by DTA."""

    @dataclass
    class RowResult:
        """Simple class to match the Griptape RowResult interface."""

        cells: Dict[str, Any]

    def __init__(
        self,
        conn: Any = None,
        engine_url: str | None = None,
    ):
        """
        Initialize with a PostgreSQL connection or pool.

        Args:
            conn: PostgreSQL connection object or pool
            engine_url: Connection URL string as an alternative to providing a connection
        """
        if conn:
            self.conn = conn
            # Check if this is a connection pool
            self.is_pool = isinstance(conn, DbConnPool)
        elif engine_url:
            # Don't connect here since we need async connection
            self.engine_url = engine_url
            self.conn = None
            self.is_pool = False
        else:
            raise ValueError("Either conn or engine_url must be provided")

    async def connect(self):
        if self.conn is not None:
            return
        if self.engine_url:
            self.conn = DbConnPool(self.engine_url)
            self.is_pool = True
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
            if self.conn is None:
                await self.connect()
                if self.conn is None:
                    raise ValueError("Connection not established")

            # Handle connection pool vs direct connection
            if self.is_pool:
                # For pools, get a connection from the pool
                pool = await self.conn.get_pool()
                async with pool.connection() as connection:
                    return await self._execute_with_connection(
                        connection, query, params, force_readonly
                    )
            else:
                # Direct connection approach
                return await self._execute_with_connection(
                    self.conn, query, params, force_readonly
                )
        except Exception as e:
            # Mark pool as invalid if there was a connection issue
            if self.conn and self.is_pool:
                self.conn._is_valid = False
                self.conn._last_error = str(e)
            elif self.conn and not self.is_pool:
                self.conn = None

            raise e

    async def _execute_with_connection(
        self, connection, query, params, force_readonly
    ) -> Optional[List[RowResult]]:
        """Execute query with the given connection."""
        transaction_started = False
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # Start read-only transaction
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                    transaction_started = True

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
                    elif transaction_started:
                        await cursor.execute("ROLLBACK")
                        transaction_started = False
                    return None

                # Get results from the last statement only
                rows = await cursor.fetchall()

                # End the transaction appropriately
                if not force_readonly:
                    await cursor.execute("COMMIT")
                elif transaction_started:
                    await cursor.execute("ROLLBACK")
                    transaction_started = False

                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

        except Exception as e:
            # Try to roll back the transaction if it's still active
            if transaction_started:
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    logger.error(f"Error rolling back transaction: {rollback_error}")

            logger.error(f"Error executing query ({query}): {e}")
            raise e
