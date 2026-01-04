from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import ClassVar
from typing import Optional

import pglast
from pglast.ast import DeleteStmt
from pglast.ast import ExplainStmt
from pglast.ast import IndexElem
from pglast.ast import InferClause
from pglast.ast import InsertStmt
from pglast.ast import Node
from pglast.ast import OnConflictClause
from pglast.ast import RawStmt
from pglast.ast import SelectStmt
from pglast.ast import UpdateStmt
from pglast.ast import VariableShowStmt
from typing_extensions import LiteralString

from .safe_sql import SafeSqlDriver
from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


class DmlOnlySqlDriver(SqlDriver):
    """A wrapper around SqlDriver that allows DML operations but blocks DDL.

    Uses pglast to parse and validate SQL statements before execution.
    Allows: SELECT, INSERT, UPDATE, DELETE, and other read operations
    Blocks: CREATE, ALTER, DROP, TRUNCATE, and other DDL operations
    """

    # Allowed statement types for DML_ONLY mode
    ALLOWED_STMT_TYPES: ClassVar[set[type]] = {
        SelectStmt,  # SELECT queries
        InsertStmt,  # INSERT
        UpdateStmt,  # UPDATE
        DeleteStmt,  # DELETE
        ExplainStmt,  # EXPLAIN
        VariableShowStmt,  # SHOW statements
    }

    # Reuse allowed functions from SafeSqlDriver
    ALLOWED_FUNCTIONS: ClassVar[set[str]] = SafeSqlDriver.ALLOWED_FUNCTIONS

    # Reuse allowed node types from SafeSqlDriver, plus DML statement types and UPSERT-related nodes
    ALLOWED_NODE_TYPES: ClassVar[set[type]] = SafeSqlDriver.ALLOWED_NODE_TYPES | {
        InsertStmt,
        UpdateStmt,
        DeleteStmt,
        OnConflictClause,  # For INSERT ... ON CONFLICT (UPSERT)
        InferClause,  # For conflict target specification in UPSERT
        IndexElem,  # For index element specification in UPSERT conflict target
    }

    def __init__(self, sql_driver: SqlDriver, timeout: float | None = None):
        """Initialize with an underlying SQL driver and optional timeout.

        Args:
            sql_driver: The underlying SQL driver to wrap
            timeout: Optional timeout in seconds for query execution
        """
        self.sql_driver = sql_driver
        self.timeout = timeout

    def _validate_node(self, node: Node) -> None:
        """Recursively validate a node and all its children"""
        # Check if node type is allowed
        if not isinstance(node, tuple(self.ALLOWED_NODE_TYPES)):
            raise ValueError(f"Node type {type(node)} is not allowed")

        # Validate function calls (reuse logic from SafeSqlDriver)
        if hasattr(node, "funcname") and node.funcname:
            func_name = ".".join([str(n.sval) for n in node.funcname]).lower() if node.funcname else ""
            # Strip pg_catalog schema if present
            match = SafeSqlDriver.PG_CATALOG_PATTERN.match(func_name)
            unqualified_name = match.group(1) if match else func_name
            if unqualified_name not in self.ALLOWED_FUNCTIONS:
                raise ValueError(f"Function {func_name} is not allowed")

        # Reject EXPLAIN ANALYZE statements
        if isinstance(node, ExplainStmt):
            for option in node.options or []:
                if hasattr(option, "defname") and option.defname == "analyze":
                    raise ValueError("EXPLAIN ANALYZE is not supported")

        # Recursively validate all attributes that might be nodes
        for attr_name in node.__slots__:
            # Skip private attributes and methods
            if attr_name.startswith("_"):
                continue

            try:
                attr = getattr(node, attr_name)
            except AttributeError:
                # Skip attributes that don't exist (this is normal in pglast)
                continue

            # Handle lists of nodes
            if isinstance(attr, list):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)

            # Handle tuples of nodes
            elif isinstance(attr, tuple):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)

            # Handle single nodes
            elif isinstance(attr, Node):
                self._validate_node(attr)

    def _validate(self, query: str) -> None:
        """Validate query allows DML but blocks DDL"""
        try:
            # Parse the SQL using pglast
            parsed = pglast.parse_sql(query)

            # Validate each statement
            try:
                for stmt in parsed:
                    if isinstance(stmt, RawStmt):
                        # Check if the inner statement type is allowed
                        if not isinstance(stmt.stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                f"Only SELECT, INSERT, UPDATE, DELETE, EXPLAIN, and SHOW statements are allowed. "
                                f"DDL operations are blocked. Received: {type(stmt.stmt).__name__}"
                            )
                    else:
                        if not isinstance(stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                f"Only SELECT, INSERT, UPDATE, DELETE, EXPLAIN, and SHOW statements are allowed. "
                                f"DDL operations are blocked. Received: {type(stmt).__name__}"
                            )
                    self._validate_node(stmt)
            except Exception as e:
                raise ValueError(f"Error validating query: {query}") from e

        except pglast.parser.ParseError as e:
            raise ValueError("Failed to parse SQL statement") from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = False,  # Allow writes by default in DML_ONLY mode
    ) -> Optional[list[SqlDriver.RowResult]]:  # noqa: UP007
        """Execute a query after validating it is safe"""
        self._validate(query)

        # Execute with timeout if configured
        if self.timeout:
            try:
                async with asyncio.timeout(self.timeout):
                    return await self.sql_driver.execute_query(
                        f"/* crystaldba */ {query}",
                        params=params,
                        force_readonly=force_readonly,
                    )
            except asyncio.TimeoutError as e:
                logger.warning(f"Query execution timed out after {self.timeout} seconds: {query[:100]}...")
                raise ValueError(
                    f"Query execution timed out after {self.timeout} seconds in DML_ONLY mode. "
                    "Consider simplifying your query or increasing the timeout."
                ) from e
            except Exception as e:
                logger.error(f"Error executing query: {e}")
                raise
        else:
            return await self.sql_driver.execute_query(
                f"/* crystaldba */ {query}",
                params=params,
                force_readonly=force_readonly,
            )
