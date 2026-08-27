# ruff: noqa: B008
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from enum import Enum
from typing import Any
from typing import List
from typing import Literal
from typing import LiteralString
from typing import Optional
from typing import Union
from typing import cast

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic import validate_call

from postgres_mcp.index.dta_calc import DatabaseTuningAdvisor

from .artifacts import ErrorResult
from .artifacts import ExplainPlanArtifact
from .database_health import DatabaseHealthTool
from .database_health import HealthType
from .explain import ExplainPlanTool
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .index.llm_opt import LLMOptimizerTool
from .index.presentation import TextPresentation
from .outbound_lock import run_outbound_lock
from .sql import DbConnPool
from .sql import SafeSqlDriver
from .sql import SqlDriver
from .sql import check_hypopg_installation_status
from .sql import obfuscate_password
from .top_queries import TopQueriesCalc

# Initialize FastMCP with default settings
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
shutdown_in_progress = False


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


@mcp.tool(
    description="List all schemas in the database",
    annotations=ToolAnnotations(
        title="List Schemas",
        readOnlyHint=True,
    ),
)
async def list_schemas() -> ResponseType:
    """List all schemas in the database."""
    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                    WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                    ELSE 'User Schema'
                END as schema_type
            FROM information_schema.schemata
            ORDER BY schema_type, schema_name
            """
        )
        schemas = [row.cells for row in rows] if rows else []
        return format_text_response(schemas)
    except Exception as e:
        logger.error(f"Error listing schemas: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List objects in a schema",
    annotations=ToolAnnotations(
        title="List Objects",
        readOnlyHint=True,
    ),
)
async def list_objects(
    schema_name: str = Field(description="Schema name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """List objects of a given type in a schema."""
    try:
        sql_driver = await get_sql_driver()

        if object_type in ("table", "view"):
            table_type = "BASE TABLE" if object_type == "table" else "VIEW"
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = {} AND table_type = {}
                ORDER BY table_name
                """,
                [schema_name, table_type],
            )
            objects = (
                [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type
                FROM information_schema.sequences
                WHERE sequence_schema = {}
                ORDER BY sequence_name
                """,
                [schema_name],
            )
            objects = (
                [{"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "extension":
            # Extensions are not schema-specific
            rows = await sql_driver.execute_query(
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                ORDER BY extname
                """
            )
            objects = (
                [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
                if rows
                else []
            )

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(objects)
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Show detailed information about a database object",
    annotations=ToolAnnotations(
        title="Get Object Details",
        readOnlyHint=True,
    ),
)
async def get_object_details(
    schema_name: str = Field(description="Schema name"),
    object_name: str = Field(description="Object name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """Get detailed information about a database object."""
    try:
        sql_driver = await get_sql_driver()

        if object_type in ("table", "view"):
            # Get columns
            col_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = {} AND table_name = {}
                ORDER BY ordinal_position
                """,
                [schema_name, object_name],
            )
            columns = (
                [
                    {
                        "column": r.cells["column_name"],
                        "data_type": r.cells["data_type"],
                        "is_nullable": r.cells["is_nullable"],
                        "default": r.cells["column_default"],
                    }
                    for r in col_rows
                ]
                if col_rows
                else []
            )

            # Get constraints
            con_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = {} AND tc.table_name = {}
                """,
                [schema_name, object_name],
            )

            constraints = {}
            if con_rows:
                for row in con_rows:
                    cname = row.cells["constraint_name"]
                    ctype = row.cells["constraint_type"]
                    col = row.cells["column_name"]

                    if cname not in constraints:
                        constraints[cname] = {"type": ctype, "columns": []}
                    if col:
                        constraints[cname]["columns"].append(col)

            constraints_list = [{"name": name, **data} for name, data in constraints.items()]

            # Get indexes
            idx_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = {} AND tablename = {}
                """,
                [schema_name, object_name],
            )

            indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

            result = {
                "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                "columns": columns,
                "constraints": constraints_list,
                "indexes": indexes,
            }

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type, start_value, increment
                FROM information_schema.sequences
                WHERE sequence_schema = {} AND sequence_name = {}
                """,
                [schema_name, object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {
                    "schema": row.cells["sequence_schema"],
                    "name": row.cells["sequence_name"],
                    "data_type": row.cells["data_type"],
                    "start_value": row.cells["start_value"],
                    "increment": row.cells["increment"],
                }
            else:
                result = {}

        elif object_type == "extension":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                WHERE extname = {}
                """,
                [object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
            else:
                result = {}

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting object details: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates.",
    annotations=ToolAnnotations(
        title="Explain Query",
        readOnlyHint=True,
    ),
)
async def explain_query(
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. "
        "Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] = Field(
        description="""A list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]
If there is no hypothetical index, you can pass an empty list.""",
        default=[],
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
        if hypothetical_indexes and len(hypothetical_indexes) > 0:
            if analyze:
                return format_error_response("Cannot use analyze and hypothetical indexes together")
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
                result = await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
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
            error_message = "Error processing explain plan"
            if isinstance(result, ErrorResult):
                error_message = result.to_text()
            return format_error_response(error_message)
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


# Query function declaration without the decorator - we'll add it dynamically based on access mode
async def execute_sql(
    sql: str = Field(description="SQL to run", default="all"),
) -> ResponseType:
    """Executes a SQL query against the database."""
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
    description="Analyze frequently executed queries in the database and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Workload Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
async def analyze_workload_indexes(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        sql_driver = await get_sql_driver()
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Query Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
async def analyze_query_indexes(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    if len(queries) == 0:
        return format_error_response("Please provide a non-empty list of queries to analyze.")
    if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
        return format_error_response(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")

    try:
        sql_driver = await get_sql_driver()
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyzes database health. Here are the available health checks:\n"
    "- index - checks for invalid, duplicate, and bloated indexes\n"
    "- connection - checks the number of connection and their utilization\n"
    "- vacuum - checks vacuum health for transaction id wraparound\n"
    "- sequence - checks sequences at risk of exceeding their maximum value\n"
    "- replication - checks replication health including lag and slots\n"
    "- buffer - checks for buffer cache hit rates for indexes and tables\n"
    "- constraint - checks for invalid constraints\n"
    "- all - runs all checks\n"
    "You can optionally specify a single health check or a comma-separated list of health checks. The default is 'all' checks.",
    annotations=ToolAnnotations(
        title="Analyze Database Health",
        readOnlyHint=True,
    ),
)
async def analyze_db_health(
    health_type: str = Field(
        description=f"Optional. Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
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
    name="get_top_queries",
    description=f"Reports the slowest or most resource-intensive queries using data from the '{PG_STAT_STATEMENTS}' extension.",
    annotations=ToolAnnotations(
        title="Get Top Queries",
        readOnlyHint=True,
    ),
)
async def get_top_queries(
    sort_by: str = Field(
        description="Ranking criteria: 'total_time' for total execution time or 'mean_time' for mean execution time per call, or 'resources' "
        "for resource-intensive queries",
        default="resources",
    ),
    limit: int = Field(description="Number of queries to return when ranking based on mean_time or total_time", default=10),
) -> ResponseType:
    try:
        sql_driver = await get_sql_driver()
        top_queries_tool = TopQueriesCalc(sql_driver=sql_driver)

        if sort_by == "resources":
            result = await top_queries_tool.get_top_resource_queries()
            return format_text_response(result)
        elif sort_by == "mean_time" or sort_by == "total_time":
            # Map the sort_by values to what get_top_queries_by_time expects
            result = await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")
        else:
            return format_error_response("Invalid sort criteria. Please use 'resources' or 'mean_time' or 'total_time'.")
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description=(
        "Fuzzy search across all communication data — messages (body, subject), "
        "participants (display_name), channels (name), and transcripts (transcript_text). "
        "Returns matched rows grouped by table, ranked by trigram similarity. "
        "Use this when you need to find communications that mention specific terms, "
        "people, or topics across the entire data store in one call. "
        "Keywords must be at least 3 characters each."
    ),
    annotations=ToolAnnotations(
        title="Universal Search",
        readOnlyHint=True,
    ),
)
async def universal_search(
    keywords: List[str] = Field(description="Search terms to fuzzy match, minimum 3 characters each (e.g. ['invoice', 'Dan'])"),
    mode: Literal["OR", "AND"] = Field(
        description="Search mode: 'OR' matches any keyword, 'AND' requires all keywords",
        default="OR",
    ),
    limit: int = Field(
        description="Maximum results per table",
        default=20,
    ),
) -> ResponseType:
    """Fuzzy search across messages, participants, channels, and transcripts."""
    # Validate inputs
    if not keywords:
        return format_error_response("At least one keyword is required")
    for kw in keywords:
        if len(kw) < 3:
            return format_error_response(f"Keyword '{kw}' is too short. Keywords must be at least 3 characters for effective fuzzy matching.")
    if limit < 1 or limit > 200:
        return format_error_response("limit must be between 1 and 200")

    try:
        sql_driver = await get_sql_driver()

        # Table configurations
        tables = [
            {
                "name": "Messages",
                "table": "messages",
                "search_cols": ["body", "subject"],
                "select": "id, source, channel_id, sender_participant_id, sent_at, "
                "left(coalesce(subject, ''), 100) AS subject, "
                "left(coalesce(body, ''), 200) AS body",
                "formatter": _format_message_row,
            },
            {
                "name": "Participants",
                "table": "participants",
                "search_cols": ["display_name"],
                "select": "id, source, participant_type, participant_key, display_name",
                "formatter": _format_participant_row,
            },
            {
                "name": "Channels",
                "table": "channels",
                "search_cols": ["name"],
                "select": "id, source, channel_type, source_channel_id, name",
                "formatter": _format_channel_row,
            },
            {
                "name": "Transcripts",
                "table": "transcripts",
                "search_cols": ["transcript_text"],
                "select": "id, call_id, message_id, left(transcript_text, 200) AS transcript_text, created_at",
                "formatter": _format_transcript_row,
            },
        ]

        sections = []
        for cfg in tables:
            rows = await _search_table(
                sql_driver,
                table=cfg["table"],
                search_cols=cfg["search_cols"],
                select=cfg["select"],
                keywords=keywords,
                mode=mode,
                limit=limit,
            )
            sections.append(_format_section(cfg["name"], rows, cfg["formatter"]))

        return format_text_response("\n\n".join(sections))
    except Exception as e:
        logger.error(f"Error in universal_search: {e}")
        return format_error_response(str(e))


async def _search_table(
    sql_driver: Union[SqlDriver, SafeSqlDriver],
    table: str,
    search_cols: List[str],
    select: str,
    keywords: List[str],
    mode: str,
    limit: int,
) -> List[Any]:
    """Build and execute a fuzzy search query for one table.

    NOTE: All non-parameter fragments (table, search_cols, select) are
    interpolated via f-string and must NEVER contain user input or literal
    `{` / `}` characters. Only `keywords` values go through the parameterized
    path via `{}` placeholders.
    """
    # Build ILIKE WHERE clause. For OR mode: any col ILIKE any keyword.
    # For AND mode: every keyword must match at least one col.
    params: List[Any] = []

    per_keyword_clauses = []
    for kw in keywords:
        like_pattern = f"%{kw}%"
        col_clauses = []
        for col in search_cols:
            col_clauses.append(f"{col} ILIKE {{}}")
            params.append(like_pattern)
        per_keyword_clauses.append("(" + " OR ".join(col_clauses) + ")")

    if mode == "AND":
        where_clause = " AND ".join(per_keyword_clauses)
    else:
        where_clause = " OR ".join(per_keyword_clauses)

    # Build ORDER BY: GREATEST of similarity scores for each (col, keyword) pair
    sim_terms = []
    for col in search_cols:
        for kw in keywords:
            sim_terms.append(f"similarity(coalesce({col}, ''), {{}})")
            params.append(kw)
    order_by = f"GREATEST({', '.join(sim_terms)})"

    # Append limit as parameter
    params.append(limit)

    query = f"SELECT {select} FROM {table} WHERE {where_clause} ORDER BY {order_by} DESC LIMIT {{}}"

    rows = await SafeSqlDriver.execute_param_query(
        sql_driver,
        cast(LiteralString, query),
        params,
    )
    return list(rows) if rows else []


def _format_section(title: str, rows: List[Any], formatter) -> str:
    """Format a result section for one table."""
    if not rows:
        return f"=== {title} (0 matches) ===\nNo matches found."

    lines = [f"=== {title} ({len(rows)} matches) ==="]
    for row in rows:
        lines.append(formatter(row.cells))
    return "\n".join(lines)


def _fmt_dt(value: Any) -> str:
    """Format a datetime-like value as ISO 8601, or pass through strings."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _format_message_row(cells: dict[str, Any]) -> str:
    return (
        f"[id={cells.get('id')}] source={cells.get('source')} "
        f"sent_at={_fmt_dt(cells.get('sent_at'))} "
        f"channel_id={cells.get('channel_id')} "
        f"sender_participant_id={cells.get('sender_participant_id')}\n"
        f"  subject: {cells.get('subject') or ''}\n"
        f"  body: {cells.get('body') or ''}"
    )


def _format_participant_row(cells: dict[str, Any]) -> str:
    return (
        f"[id={cells.get('id')}] source={cells.get('source')} "
        f"type={cells.get('participant_type')} key={cells.get('participant_key')}\n"
        f"  display_name: {cells.get('display_name') or ''}"
    )


def _format_channel_row(cells: dict[str, Any]) -> str:
    return (
        f"[id={cells.get('id')}] source={cells.get('source')} "
        f"type={cells.get('channel_type')} source_channel_id={cells.get('source_channel_id')}\n"
        f"  name: {cells.get('name') or ''}"
    )


def _format_transcript_row(cells: dict[str, Any]) -> str:
    return (
        f"[id={cells.get('id')}] call_id={cells.get('call_id')} "
        f"message_id={cells.get('message_id')} "
        f"created_at={_fmt_dt(cells.get('created_at'))}\n"
        f"  transcript_text: {cells.get('transcript_text') or ''}"
    )


@mcp.tool(
    description=(
        "Outbound intent lock — the single source of truth for 'did this customer-facing "
        "send already happen?'. Use BEFORE any outbound send to avoid duplicates. Strict JSON "
        "in/out; message IDs and proxy emails travel as args (never inline SQL). Ops:\n"
        "- acquire {identity_key, intent_kind, holder, property_scope?}: take the lock. Returns "
        "{acquired, lock_id, existing}. If acquired=false, `existing` carries the blocking lock "
        "with is_duplicate_send_evidence — if that is true the send ALREADY happened, do NOT resend.\n"
        "- complete {lock_id, holder, request_ref}: after a VERIFIED send; request_ref is the send "
        "handle (email request id / SMTP Message-ID / SMS id).\n"
        "- release {lock_id, holder}: abort without sending, freeing the scope.\n"
        "- check {identity_key, property_scope?, intent_kind?, days?}: recent locks + evidence, for "
        "dedup audits.\n"
        "is_duplicate_send_evidence = (completed_at set AND request_ref set), independent of "
        "released_at — a completed lock is evidence of a send; a plain release is not."
    ),
    annotations=ToolAnnotations(
        title="Outbound Lock",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
@validate_call
async def outbound_lock(
    op: Literal["acquire", "complete", "release", "check"] = Field(description="acquire | complete | release | check"),
    identity_key: Optional[str] = Field(
        default=None, description="Customer identity (phone digits / email / factbook:uuid). Required for acquire and check."
    ),
    property_scope: str = Field(default="", description="Unit/property scope, e.g. '317 S Main St #3'. Optional; normalized server-side."),
    intent_kind: Optional[str] = Field(
        default=None, description="What is being sent, e.g. 'showing-confirmation', 'correction'. Required for acquire."
    ),
    holder: Optional[str] = Field(default=None, description="This run's id (session/run id). Required for acquire/complete/release."),
    lock_id: Optional[int] = Field(default=None, description="Lock id from acquire. Required for complete/release."),
    request_ref: Optional[str] = Field(
        default=None, description="Verified send handle (email request id / Message-ID / SMS id). Required for complete."
    ),
    days: int = Field(default=7, description="check: look-back window in days."),
) -> ResponseType:
    """First-class outbound lock (wraps the migration-026 SQL functions)."""
    try:
        driver = await get_sql_driver()
        result = await run_outbound_lock(
            driver,
            op=op,
            identity_key=identity_key,
            property_scope=property_scope,
            intent_kind=intent_kind,
            holder=holder,
            lock_id=lock_id,
            request_ref=request_ref,
            days=days,
        )
        return format_text_response(json.dumps(result))
    except Exception as e:
        logger.error(f"Error in outbound_lock: {e}")
        return format_text_response(json.dumps({"op": op, "error": str(e)}))


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("database_url", help="Database connection URL", nargs="?")
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="Set SQL access mode: unrestricted (unrestricted) or restricted (read-only with protections)",
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Select MCP transport: stdio (default), sse, or streamable-http",
    )
    parser.add_argument(
        "--sse-host",
        type=str,
        default="localhost",
        help="Host to bind SSE server to (default: localhost)",
    )
    parser.add_argument(
        "--sse-port",
        type=int,
        default=8000,
        help="Port for SSE server (default: 8000)",
    )
    parser.add_argument(
        "--streamable-http-host",
        type=str,
        default="localhost",
        help="Host to bind streamable HTTP server to (default: localhost)",
    )
    parser.add_argument(
        "--streamable-http-port",
        type=int,
        default=8000,
        help="Port for streamable HTTP server (default: 8000)",
    )

    args = parser.parse_args()

    # Store the access mode in the global variable
    global current_access_mode
    current_access_mode = AccessMode(args.access_mode)

    # Add the query tool with a description and annotations appropriate to the access mode
    if current_access_mode == AccessMode.UNRESTRICTED:
        mcp.add_tool(
            execute_sql,
            description="Execute any SQL query",
            annotations=ToolAnnotations(
                title="Execute SQL",
                destructiveHint=True,
            ),
        )
    else:
        mcp.add_tool(
            execute_sql,
            description="Execute a read-only SQL query",
            annotations=ToolAnnotations(
                title="Execute SQL (Read-Only)",
                readOnlyHint=True,
            ),
        )

    logger.info(f"Starting PostgreSQL MCP Server in {current_access_mode.upper()} mode")

    # Get database URL from environment variable or command line
    database_url = os.environ.get("DATABASE_URI", args.database_url)

    if not database_url:
        raise ValueError(
            "Error: No database URL provided. Please specify via 'DATABASE_URI' environment variable or command-line argument.",
        )

    # Initialize database connection pool
    try:
        await db_connection.pool_connect(database_url)
        logger.info("Successfully connected to database and initialized connection pool")
    except Exception as e:
        logger.warning(
            f"Could not connect to database: {obfuscate_password(str(e))}",
        )
        logger.warning(
            "The MCP server will start but database operations will fail until a valid connection is established.",
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

    # Run the server with the selected transport (always async)
    if args.transport == "stdio":
        await mcp.run_stdio_async()
    elif args.transport == "sse":
        mcp.settings.host = args.sse_host
        mcp.settings.port = args.sse_port
        await mcp.run_sse_async()
    elif args.transport == "streamable-http":
        mcp.settings.host = args.streamable_http_host
        mcp.settings.port = args.streamable_http_port
        await mcp.run_streamable_http_async()


async def shutdown(sig=None):
    """Clean shutdown of the server."""
    global shutdown_in_progress

    if shutdown_in_progress:
        logger.warning("Forcing immediate exit")
        # Use sys.exit instead of os._exit to allow for proper cleanup
        sys.exit(1)

    shutdown_in_progress = True

    if sig:
        logger.info(f"Received exit signal {sig.name}")

    # Close database connections
    try:
        await db_connection.close()
        logger.info("Closed database connections")
    except Exception as e:
        logger.error(f"Error closing database connections: {e}")

    # Exit with appropriate status code
    sys.exit(128 + sig if sig is not None else 0)
