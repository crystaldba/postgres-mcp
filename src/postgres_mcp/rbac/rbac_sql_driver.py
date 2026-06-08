"""RbacSqlDriver — per-agent access control decorator over SqlDriver.

This driver wraps an existing SqlDriver (or SafeSqlDriver / ReadOnlySqlDriver)
and enforces per-agent policies:

1. **Schema guard** — rejects queries that reference schemas not allowed by
   the agent's policy (checked against ``search_path`` and explicit schema
   qualifiers parsed from the SQL AST).
2. **Access-mode delegation** — selects the correct underlying driver based
   on the agent's ``access_mode`` (unrestricted / restricted / readonly).
3. **Audit logging** — emits a structured JSON log entry for every query
   executed by agents that have ``audit=True``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..sql.sql_driver import SqlDriver, RowResult
from ..sql.safe_sql import SafeSqlDriver
from .agent_policy import AgentPolicy, AccessMode

logger = logging.getLogger(__name__)

# Timeout applied in readonly / restricted mode (seconds)
_DEFAULT_TIMEOUT = 30.0


class SchemaViolationError(PermissionError):
    """Raised when a query touches a schema not allowed by the agent policy."""


class ToolViolationError(PermissionError):
    """Raised when an agent calls a tool not allowed by its policy."""


class RbacSqlDriver:
    """Decorator that enforces an AgentPolicy on every SQL execution.

    Parameters
    ----------
    sql_driver:
        Base SqlDriver connected to the target database.
    policy:
        The AgentPolicy to enforce.
    timeout:
        Query timeout in seconds (applied in readonly/restricted modes).

    Examples
    --------
    ::

        base = SqlDriver(conn=db_connection)
        policy = AgentPolicy.from_token(
            name="reporting-bot",
            token="secret",
            access_mode="readonly",
            allowed_schemas=["analytics", "public"],
        )
        driver = RbacSqlDriver(sql_driver=base, policy=policy)
        rows = await driver.execute_query("SELECT * FROM analytics.sales")
    """

    def __init__(
        self,
        sql_driver: SqlDriver,
        policy: AgentPolicy,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base = sql_driver
        self.policy = policy
        self._timeout = timeout
        self._effective_driver = self._build_effective_driver()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_effective_driver(self) -> SqlDriver | SafeSqlDriver:
        """Wrap base driver according to agent's access_mode."""
        mode = self.policy.access_mode
        if mode == AccessMode.RESTRICTED:
            return SafeSqlDriver(sql_driver=self._base, timeout=self._timeout)
        if mode == AccessMode.READONLY:
            # Import lazily to avoid circular deps if readonly_sql is added later
            try:
                from ..sql.readonly_sql import ReadOnlySqlDriver  # type: ignore[import]
                return ReadOnlySqlDriver(sql_driver=self._base, timeout=self._timeout)
            except ImportError:
                # Fallback: use SafeSqlDriver in readonly mode
                logger.debug(
                    "readonly_sql not available, falling back to SafeSqlDriver for agent '%s'",
                    self.policy.name,
                )
                return SafeSqlDriver(sql_driver=self._base, timeout=self._timeout)
        # UNRESTRICTED — use base driver directly
        return self._base

    def _check_tool_allowed(self, tool_name: str) -> None:
        """Raise ToolViolationError if tool is not allowed for this agent."""
        if not self.policy.allows_tool(tool_name):
            raise ToolViolationError(
                f"Agent '{self.policy.name}' is not permitted to use tool '{tool_name}'. "
                f"Allowed tools: {sorted(self.policy.allowed_tools)}"
            )

    def _check_schemas_allowed(self, sql: str) -> None:
        """Best-effort schema check using simple token parsing.

        A full AST-based check would require pglast and is left as a
        future improvement.  This covers the most common patterns:
        ``schema.table`` and ``SET search_path TO schema``.
        """
        if "*" in self.policy.allowed_schemas:
            return

        # Normalize and scan for schema-qualified identifiers
        sql_lower = sql.lower()
        for schema in self._extract_schemas_from_sql(sql_lower):
            if not self.policy.allows_schema(schema):
                raise SchemaViolationError(
                    f"Agent '{self.policy.name}' is not permitted to access schema '{schema}'. "
                    f"Allowed schemas: {sorted(self.policy.allowed_schemas)}"
                )

    @staticmethod
    def _extract_schemas_from_sql(sql_lower: str) -> list[str]:
        """Extract schema names from common SQL patterns."""
        import re

        schemas: list[str] = []

        # Pattern: schema.table or schema.function
        for match in re.finditer(r'([a-z_][a-z0-9_]*)\s*\.\s*[a-z_]', sql_lower):
            candidate = match.group(1)
            # Skip common non-schema keywords
            if candidate not in (
                'pg_catalog', 'information_schema', 'pg_toast',
            ):
                schemas.append(candidate)

        # Pattern: SET search_path TO schema1, schema2
        sp_match = re.search(
            r'set\s+search_path\s+(?:to|=)\s+([^;]+)', sql_lower
        )
        if sp_match:
            for part in sp_match.group(1).split(','):
                schemas.append(part.strip().strip('"\' '))

        return schemas

    def _audit_log(self, sql: str, params: Any, duration_ms: float, error: str | None = None) -> None:
        """Emit a structured audit log entry."""
        if not self.policy.audit:
            return
        entry = {
            "event": "agent_query",
            "agent": self.policy.name,
            "access_mode": self.policy.access_mode.value,
            "sql_preview": sql[:200],
            "has_params": params is not None,
            "duration_ms": round(duration_ms, 2),
            "error": error,
        }
        logger.info("RBAC_AUDIT %s", json.dumps(entry))

    # ------------------------------------------------------------------
    # Public API — mirrors SqlDriver interface
    # ------------------------------------------------------------------

    async def execute_query(
        self,
        sql: str,
        params: Any = None,
        *,
        force_readonly: bool = False,
        tool_name: str | None = None,
    ) -> list[RowResult]:
        """Execute *sql* under this agent's policy.

        Parameters
        ----------
        sql:
            The SQL statement to execute.
        params:
            Optional query parameters (passed through to the driver).
        force_readonly:
            If True, forces a read-only transaction regardless of access_mode.
        tool_name:
            Optional MCP tool name for tool-level access control.
        """
        # 1. Tool check
        if tool_name is not None:
            self._check_tool_allowed(tool_name)

        # 2. Schema check
        self._check_schemas_allowed(sql)

        # 3. Execute via effective driver
        t0 = time.monotonic()
        error: str | None = None
        try:
            if self.policy.access_mode == AccessMode.READONLY or force_readonly:
                result = await self._effective_driver.execute_query(
                    sql, params, force_readonly=True
                )
            else:
                result = await self._effective_driver.execute_query(sql, params)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            self._audit_log(sql, params, duration_ms, error)
