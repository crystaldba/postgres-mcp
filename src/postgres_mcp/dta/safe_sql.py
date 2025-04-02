from __future__ import annotations

import logging
import re
from typing import Any
from typing import ClassVar
from typing import Optional

import pglast
from psycopg.sql import SQL, Composable, Literal
from typing_extensions import LiteralString

from .sql_driver import SqlDriver

from pglast.ast import A_ArrayExpr
from pglast.ast import A_Const
from pglast.ast import A_Expr
from pglast.ast import A_Star
from pglast.ast import Alias
from pglast.ast import BitString
from pglast.ast import Boolean
from pglast.ast import BooleanTest
from pglast.ast import BoolExpr
from pglast.ast import CaseExpr
from pglast.ast import CaseWhen
from pglast.ast import CoalesceExpr
from pglast.ast import ColumnRef
from pglast.ast import CommonTableExpr
from pglast.ast import CreateExtensionStmt
from pglast.ast import DefElem
from pglast.ast import ExplainStmt
from pglast.ast import Float
from pglast.ast import FromExpr
from pglast.ast import FuncCall
from pglast.ast import Integer
from pglast.ast import JoinExpr
from pglast.ast import MinMaxExpr
from pglast.ast import NamedArgExpr
from pglast.ast import Node
from pglast.ast import NullTest
from pglast.ast import ParamRef
from pglast.ast import RangeFunction
from pglast.ast import RangeSubselect
from pglast.ast import RangeVar
from pglast.ast import RawStmt
from pglast.ast import ResTarget
from pglast.ast import RowExpr
from pglast.ast import SelectStmt
from pglast.ast import SortBy
from pglast.ast import SortGroupClause
from pglast.ast import SQLValueFunction
from pglast.ast import String
from pglast.ast import SubLink
from pglast.ast import TypeCast
from pglast.ast import TypeName
from pglast.ast import VariableShowStmt
from pglast.ast import WithClause
from pglast.enums import A_Expr_Kind

logger = logging.getLogger(__name__)


class SafeSqlDriver(SqlDriver):
    """A wrapper around any SqlDriver that only allows SELECT, EXPLAIN SELECT, and SHOW queries.

    Uses pglast to parse and validate SQL statements before execution.
    All other statement types (DDL, DML etc) are rejected.
    Performs deep validation of the query tree to prevent unsafe operations.
    """

    # Pattern to match pg_catalog schema qualification
    PG_CATALOG_PATTERN = re.compile(r"^pg_catalog\.(.+)$")
    # Pattern to validate LIKE expressions - must either start with % or end with %, but not both
    LIKE_PATTERN = re.compile(r"^[^%]+%$")

    ALLOWED_STMT_TYPES: ClassVar[set[type]] = {
        SelectStmt,  # Regular SELECT
        ExplainStmt,  # EXPLAIN SELECT
        CreateExtensionStmt,  # CREATE EXTENSION
        VariableShowStmt,  # SHOW statements
    }

    ALLOWED_FUNCTIONS: ClassVar[set[str]] = {
        # Aggregate functions
        "array_agg",
        "avg",
        "bit_and",
        "bit_or",
        "bool_and",
        "bool_or",
        "count",
        "every",
        "json_agg",
        "jsonb_agg",
        "max",
        "min",
        "string_agg",
        "sum",
        "xmlagg",
        # Mathematical functions
        "abs",
        "cbrt",
        "ceil",
        "ceiling",
        "degrees",
        "div",
        "erf",
        "erfc",
        "exp",
        "factorial",
        "floor",
        "gcd",
        "lcm",
        "ln",
        "log",
        "log10",
        "min_scale",
        "mod",
        "pi",
        "power",
        "radians",
        "random",
        "random_normal",
        "round",
        "scale",
        "setseed",
        "sign",
        "sqrt",
        "trim_scale",
        "trunc",
        "width_bucket",
        # Trigonometric functions
        "acos",
        "acosd",
        "asin",
        "asind",
        "atan",
        "atand",
        "atan2",
        "atan2d",
        "cos",
        "cosd",
        "cot",
        "cotd",
        "sin",
        "sind",
        "tan",
        "tand",
        # Hyperbolic functions
        "sinh",
        "cosh",
        "tanh",
        "asinh",
        "acosh",
        "atanh",
        # Array functions and operators
        "array",
        "array_append",
        "array_cat",
        "array_dims",
        "array_fill",
        "array_length",
        "array_lower",
        "array_ndims",
        "array_position",
        "array_positions",
        "array_prepend",
        "array_remove",
        "array_replace",
        "array_sample",
        "array_shuffle",
        "array_to_string",
        "array_upper",
        "cardinality",
        "string_to_array",
        "trim_array",
        "unnest",
        "any",
        # String functions
        "ascii",
        "bit_length",
        "btrim",
        "char_length",
        "character_length",
        "chr",
        "concat",
        "concat_ws",
        "convert",
        "convert_from",
        "convert_to",
        "decode",
        "encode",
        "format",
        "initcap",
        "left",
        "length",
        "lower",
        "lpad",
        "ltrim",
        "md5",
        "normalize",
        "octet_length",
        "overlay",
        "parse_ident",
        "position",
        "quote_ident",
        "quote_literal",
        "quote_nullable",
        "repeat",
        "replace",
        "reverse",
        "right",
        "rpad",
        "rtrim",
        "split_part",
        "starts_with",
        "string_to_table",
        "strpos",
        "substr",
        "substring",
        "to_ascii",
        "to_bin",
        "to_hex",
        "to_oct",
        "translate",
        "trim",
        "upper",
        "unistr",
        "unicode_assigned",
        # Pattern matching functions
        "regexp_match",
        "regexp_matches",
        "regexp_replace",
        "regexp_split_to_array",
        "regexp_split_to_table",
        "regexp_substr",
        "regexp_count",
        "regexp_instr",
        "regexp_like",
        # Type casting functions
        "regclass",
        "to_char",
        "to_date",
        "to_number",
        "to_timestamp",
        # Information functions
        "current_catalog",
        "current_database",
        "current_query",
        "current_role",
        "current_schema",
        "current_schemas",
        "current_setting",
        "current_user",
        "pg_backend_pid",
        "pg_blocking_pids",
        "pg_conf_load_time",
        "pg_current_logfile",
        "pg_jit_available",
        "pg_safe_snapshot_blocking_pids",
        "pg_trigger_depth",
        "session_user",
        "system_user",
        "user",
        "version",
        "unicode_version",
        "icu_unicode_version",
        # Database object information functions
        "pg_column_size",
        "pg_column_compression",
        "pg_column_toast_chunk_id",
        "pg_database_size",
        "pg_indexes_size",
        "pg_get_indexdef",
        "pg_relation_filenode",
        "pg_relation_size",
        "pg_size_bytes",
        "pg_size_pretty",
        "pg_table_size",
        "pg_tablespace_size",
        "pg_total_relation_size",
        # Security privilege check functions
        "has_any_column_privilege",
        "has_column_privilege",
        "has_database_privilege",
        "has_foreign_data_wrapper_privilege",
        "has_function_privilege",
        "has_language_privilege",
        "has_parameter_privilege",
        "has_schema_privilege",
        "has_sequence_privilege",
        "has_server_privilege",
        "has_table_privilege",
        "has_tablespace_privilege",
        "has_type_privilege",
        "pg_has_role",
        "row_security_active",
        # JSON functions
        "json",
        "json_array_length",
        "jsonb_array_length",
        "json_each",
        "jsonb_each",
        "json_each_text",
        "jsonb_each_text",
        "json_extract_path",
        "jsonb_extract_path",
        "json_extract_path_text",
        "jsonb_extract_path_text",
        "json_object_keys",
        "jsonb_object_keys",
        "json_array_elements",
        "jsonb_array_elements",
        "json_array_elements_text",
        "jsonb_array_elements_text",
        "json_typeof",
        "jsonb_typeof",
        "json_strip_nulls",
        "jsonb_strip_nulls",
        "jsonb_set",
        "jsonb_set_path",
        "jsonb_set_lax",
        "jsonb_pretty",
        "json_build_array",
        "jsonb_build_array",
        "json_build_object",
        "jsonb_build_object",
        "json_object",
        "jsonb_object",
        "json_scalar",
        "json_serialize",
        "json_populate_record",
        "jsonb_populate_record",
        "jsonb_populate_record_valid",
        "json_populate_recordset",
        "jsonb_populate_recordset",
        "json_to_record",
        "jsonb_to_record",
        "json_to_recordset",
        "jsonb_to_recordset",
        "jsonb_insert",
        "jsonb_path_exists",
        "jsonb_path_match",
        "jsonb_path_query",
        "jsonb_path_query_array",
        "jsonb_path_query_first",
        "jsonb_path_exists_tz",
        "jsonb_path_match_tz",
        "jsonb_path_query_tz",
        "jsonb_path_query_array_tz",
        "jsonb_path_query_first_tz",
        "jsonbv_typeof",
        "to_json",
        "to_jsonb",
        "array_to_json",
        "row_to_json",
        # Object Info
        "pg_get_expr",
        "pg_get_functiondef",
        "pg_get_function_arguments",
        "pg_get_function_identity_arguments",
        "pg_get_function_result",
        "pg_get_catalog_foreign_keys",
        "pg_get_constraintdef",
        "pg_get_userbyid",
        "pg_get_keywords",
        "pg_get_partkeydef",
        # Encoding Functions
        "pg_basetype",
        "pg_client_encoding",
        "pg_encoding_to_char",
        "pg_char_to_encoding",
        # Validity Checking
        "pg_input_is_valid",
        "pg_input_error_info",
        # Object Definition/Information Functions
        "pg_get_serial_sequence",
        "pg_get_viewdef",
        "pg_get_ruledef",
        "pg_get_triggerdef",
        "pg_get_statisticsobjdef",
        # Type Information and Conversion
        "pg_typeof",
        "format_type",
        "to_regtype",
        "to_regtypemod",
        # Object Name/OID Translation
        "to_regclass",
        "to_regcollation",
        "to_regnamespace",
        "to_regoper",
        "to_regoperator",
        "to_regproc",
        "to_regprocedure",
        "to_regrole",
        # Index Property Functions
        "pg_index_column_has_property",
        "pg_index_has_property",
        "pg_indexam_has_property",
        # Schema Visibility Functions
        "pg_collation_is_visible",
        "pg_conversion_is_visible",
        "pg_function_is_visible",
        "pg_opclass_is_visible",
        "pg_operator_is_visible",
        "pg_opfamily_is_visible",
        "pg_statistics_obj_is_visible",
        "pg_table_is_visible",
        "pg_ts_config_is_visible",
        "pg_ts_dict_is_visible",
        "pg_ts_parser_is_visible",
        "pg_ts_template_is_visible",
        "pg_type_is_visible",
        # Date/Time Functions
        "age",  # When used with timestamp arguments (not transaction IDs)
        "clock_timestamp",
        "current_date",
        "current_time",
        "current_timestamp",
        "date_part",
        "date_trunc",
        "extract",
        "isfinite",
        "justify_days",
        "justify_hours",
        "justify_interval",
        "localtime",
        "localtimestamp",
        "make_date",
        "make_interval",
        "make_time",
        "make_timestamp",
        "make_timestamptz",
        "now",
        "statement_timestamp",
        "timeofday",
        "transaction_timestamp",
        # Additional Type Conversion
        "cast",
        "text",
        "bool",
        "int2",
        "int4",
        "int8",
        "float4",
        "float8",
        "numeric",
        "date",
        "time",
        "timetz",
        "timestamp",
        "timestamptz",
        "interval",
        # ACL functions
        "acldefault",
        "aclexplode",
        "makeaclitem",
        # System/Configuration Information
        "pg_tablespace_location",  # Exposes filesystem paths
        "pg_tablespace_databases",  # Exposes system-wide information
        "pg_settings_get_flags",  # Exposes configuration details
        "pg_options_to_table",  # Exposes internal storage options
        # Transaction/WAL Information
        "pg_current_xact_id",  # Exposes transaction details
        "pg_current_snapshot",  # Exposes transaction snapshots
        "pg_snapshot_xip",  # Exposes in-progress transactions
        "pg_xact_commit_timestamp",  # Transaction timestamp details
        # Control Data Functions
        "pg_control_checkpoint",  # Internal checkpoint state
        "pg_control_system",  # Control file state
        "pg_control_init",  # Cluster initialization state
        "pg_control_recovery",  # Recovery state
        # WAL Functions
        "pg_available_wal_summaries",  # WAL summary information
        "pg_wal_summary_contents",  # WAL contents
        "pg_get_wal_summarizer_state",  # WAL summarizer state
        # Network Information
        "inet_client_addr",  # Client network details
        "inet_client_port",  # Client port information
        "inet_server_addr",  # Server network details
        "inet_server_port",  # Server port information
        # Temporary Schema Information
        "pg_my_temp_schema",  # Exposes temp schema OIDs
        "pg_is_other_temp_schema",  # Shows other sessions' temp schemas
        # Notification/Channel Information
        "pg_listening_channels",  # Shows active notification channels
        "pg_notification_queue_usage",  # Exposes notification queue state
        # Server State Information
        "pg_postmaster_start_time",  # Shows server start time
        # Recovery Information Functions (safe ones)
        "pg_is_in_recovery",
        # Hypopg functions
        "hypopg_create_index",
        "hypopg_reset",
        "hypopg_relation_size",
        "hypopg_list_indexes",
        "hypopg_get_indexdef",
        "hypopg_hide_index",
        "hypopg_unhide_index",
    }

    ALLOWED_NODE_TYPES: ClassVar[set[type]] = ALLOWED_STMT_TYPES | {
        # Basic SELECT components
        ResTarget,
        ColumnRef,
        A_Star,
        A_Const,
        A_Expr,
        BoolExpr,
        BooleanTest,
        NullTest,
        RangeVar,
        JoinExpr,
        FromExpr,
        WithClause,
        CommonTableExpr,
        # Basic operators
        A_Expr,
        BoolExpr,
        SubLink,
        MinMaxExpr,
        RowExpr,
        # EXPLAIN components
        ExplainStmt,
        DefElem,
        # SHOW components
        VariableShowStmt,
        # Sorting/grouping
        SortBy,
        SortGroupClause,
        # Constants and basic types
        Integer,
        Float,
        String,
        BitString,
        Boolean,
        RawStmt,
        ParamRef,
        SQLValueFunction,
        # Function calls and type casting
        FuncCall,
        TypeCast,
        DefElem,
        TypeName,
        Alias,
        CaseExpr,
        CaseWhen,
        RangeSubselect,
        CoalesceExpr,
        NamedArgExpr,
        RangeFunction,
        A_ArrayExpr,
    }

    ALLOWED_EXTENSIONS: ClassVar[set[str]] = {
        "hypopg",
        "pg_stat_statements",
    }

    def __init__(self, sql_driver: SqlDriver):
        """Initialize with an underlying SQL driver"""
        self.sql_driver = sql_driver

    def _validate_node(self, node: Node) -> None:
        """Recursively validate a node and all its children"""
        # Check if node type is allowed
        if not isinstance(node, tuple(self.ALLOWED_NODE_TYPES)):
            raise ValueError(f"Node type {type(node)} is not allowed")

        # Validate LIKE patterns
        if isinstance(node, A_Expr) and node.kind in (
            A_Expr_Kind.AEXPR_LIKE,
            A_Expr_Kind.AEXPR_ILIKE,
        ):
            # Get the right-hand side of the LIKE expression (the pattern)
            if (
                isinstance(node.rexpr, A_Const)
                and node.rexpr.val is not None
                and hasattr(node.rexpr.val, "sval")
                and node.rexpr.val.sval is not None
            ):
                # Nothing to do for now
                pass
            else:
                raise ValueError("LIKE pattern must be a constant string")

        # Validate function calls
        if isinstance(node, FuncCall):
            func_name = (
                ".".join([str(n.sval) for n in node.funcname]).lower()
                if node.funcname
                else ""
            )
            # Strip pg_catalog schema if present
            match = self.PG_CATALOG_PATTERN.match(func_name)
            unqualified_name = match.group(1) if match else func_name
            if unqualified_name not in self.ALLOWED_FUNCTIONS:
                raise ValueError(f"Function {func_name} is not allowed")

        # Reject SELECT statements with locking clauses
        if isinstance(node, SelectStmt) and getattr(node, "lockingClause", None):
            raise ValueError("Locking clause on select is prohibited")

        # Reject EXPLAIN ANALYZE statements
        if isinstance(node, ExplainStmt):
            for option in node.options or []:
                if isinstance(option, DefElem) and option.defname == "analyze":
                    raise ValueError("EXPLAIN ANALYZE is not supported")

        # Reject CREATE EXTENSION statements
        if isinstance(node, CreateExtensionStmt):
            if node.extname not in self.ALLOWED_EXTENSIONS:
                raise ValueError(f"CREATE EXTENSION {node.extname} is not supported")

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
        """Validate query is safe to execute"""
        try:
            # Parse the SQL using pglast
            parsed = pglast.parse_sql(query)
            # Pretty print the parsed SQL for debugging
            # print("Parsed SQL:")
            # import pprint
            # pprint.pprint(parsed)

            # Validate each statement
            try:
                for stmt in parsed:
                    if isinstance(stmt, RawStmt):
                        # Check if the inner statement type is allowed
                        if not isinstance(stmt.stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, EXPLAIN, and SHOW statements are allowed"
                            )
                    else:
                        if not isinstance(stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, EXPLAIN, and SHOW statements are allowed"
                            )
                    self._validate_node(stmt)
            except Exception as e:
                logger.error(f"Error validating query: {query}. Error: {e}")
                raise e

        except pglast.parser.ParseError as e:
            raise ValueError("Failed to parse SQL statement") from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,
    ) -> Optional[list[SqlDriver.RowResult]]:  # noqa: UP007
        """Execute a query after validating it is safe"""
        self._validate(query)
        return await self.sql_driver.execute_query(
            f"/* crystaldba */ {query}",
            params=params,
            force_readonly=force_readonly,
        )

    @staticmethod
    def sql_to_query(sql: Composable) -> str:
        """Convert a SQL string to a query string."""
        return sql.as_string()

    @staticmethod
    def param_sql_to_query(query: str, params: list[Any]) -> str:
        """Convert a SQL string to a query string."""
        # Convert each parameter to a Literal only if it's not already a Composable
        sql_params = [p if isinstance(p, Composable) else Literal(p) for p in params]

        return SafeSqlDriver.sql_to_query(
            SQL(query).format(*sql_params)  # type: ignore
        )

    @staticmethod
    async def execute_param_query(
        sql_driver: SqlDriver, query: str, params: list[Any] | None = None
    ) -> list[SqlDriver.RowResult] | None:
        """Execute a query after validating it is safe"""
        if params:
            query_params = SafeSqlDriver.param_sql_to_query(query, params)
            return await sql_driver.execute_query(query_params)  # type: ignore
        else:
            return await sql_driver.execute_query(query)  # type: ignore
