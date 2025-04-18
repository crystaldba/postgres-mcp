import json
import logging
import time
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Iterable

from pglast import parse_sql
from pglast.ast import SelectStmt

from ..artifacts import calculate_improvement_multiple
from ..explain import ExplainPlanTool
from ..sql import IndexConfig
from ..sql import SafeSqlDriver
from ..sql import SqlBindParams
from ..sql import SqlDriver
from ..sql import TableAliasVisitor
from ..sql import check_hypopg_installation_status

logger = logging.getLogger(__name__)

MAX_NUM_INDEX_TUNING_QUERIES = 10


def _pp_list(lst) -> str:
    """Pretty print a list."""
    return ("\n  - " if len(lst) > 0 else "") + "\n  - ".join([str(item) for item in lst])


@dataclass
class Index:
    """Represents a database index with size estimation and definition."""

    index_config: IndexConfig
    estimated_size: int = 0

    def __init__(
        self,
        table: str,
        columns: tuple[str, ...],
        using: str = "btree",
        estimated_size: int = 0,
        potential_problematic_reason: str | None = None,
    ):
        self.index_config = IndexConfig(table, columns, using, potential_problematic_reason)
        self.estimated_size = estimated_size

    @property
    def definition(self) -> str:
        return self.index_config.definition

    @property
    def name(self) -> str:
        return self.index_config.name

    @property
    def columns(self) -> tuple[str, ...]:
        return self.index_config.columns

    @property
    def table(self) -> str:
        return self.index_config.table

    @property
    def using(self) -> str:
        return self.index_config.using

    @property
    def potential_problematic_reason(self) -> str | None:
        return self.index_config.potential_problematic_reason

    @potential_problematic_reason.setter
    def potential_problematic_reason(self, reason: str | None) -> None:
        self.index_config = IndexConfig(self.table, self.columns, self.using, reason)

    def __hash__(self) -> int:
        return self.index_config.__hash__()

    def __eq__(self, other: Any) -> bool:
        return self.index_config.__eq__(other.index_config)

    def __str__(self) -> str:
        return self.index_config.__str__() + f" (estimated_size: {self.estimated_size})"

    def __repr__(self) -> str:
        return self.index_config.__repr__() + f" (estimated_size: {self.estimated_size})"


@dataclass
class IndexRecommendation:
    """Represents a recommended index with benefit estimation."""

    table: str
    columns: tuple[str, ...]
    using: str
    potential_problematic_reason: str | None
    estimated_size_bytes: int
    progressive_base_cost: float
    progressive_recommendation_cost: float
    individual_base_cost: float
    individual_recommendation_cost: float
    queries: list[str]
    definition: str

    @property
    def progressive_improvement_multiple(self) -> float:
        """Calculate the progressive percentage improvement from this recommendation."""
        return calculate_improvement_multiple(self.progressive_base_cost, self.progressive_recommendation_cost)

    @property
    def individual_improvement_multiple(self) -> float:
        """Calculate the individual percentage improvement from this recommendation."""
        return calculate_improvement_multiple(self.individual_base_cost, self.individual_recommendation_cost)


@dataclass
class DTASession:
    """Tracks a DTA analysis session."""

    session_id: str
    budget_mb: int
    workload_source: str = "n/a"  # 'args', 'query_list', 'query_store', 'sql_file'
    recommendations: list[IndexRecommendation] = field(default_factory=list)
    workload: list[dict[str, Any]] | None = None
    error: str | None = None
    dta_traces: list[str] = field(default_factory=list)


def candidate_str(indexes: Iterable[Index] | Iterable[IndexConfig]) -> str:
    return ", ".join(f"{idx.table}({','.join(idx.columns)})" for idx in indexes) if indexes else "(no indexes)"

class IndexTuningBase(ABC):
    def __init__(
        self,
        sql_driver: SqlDriver,
        # budget_mb: int = -1,  # no limit by default
        # max_runtime_seconds: int = 30,  # 30 seconds
        # max_index_width: int = 3,
        # min_column_usage: int = 1,  # skip columns used in fewer than this many queries
        # seed_columns_count: int = 3,  # how many single-col seeds to pick
        # pareto_alpha: float = 2.0,
        # min_time_improvement: float = 0.1,
    ):
        """
        :param sql_driver: Griptape SqlDriver
        :param budget_mb: Storage budget
        :param max_runtime_seconds: Time limit for entire analysis (anytime approach)
        :param max_index_width: Maximum columns in an index
        :param min_column_usage: skip columns that appear in fewer than X queries
        :param seed_columns_count: how many top single-column indexes to pick as seeds
        :param pareto_alpha: stop when relative improvement falls below this threshold
        :param min_time_improvement: stop when relative improvement falls below this threshold
        """
        self.sql_driver = sql_driver
        # self.budget_mb = budget_mb
        # self.max_runtime_seconds = max_runtime_seconds
        # self.max_index_width = max_index_width
        # self.min_column_usage = min_column_usage
        # self.seed_columns_count = seed_columns_count
        # self._analysis_start_time = 0.0
        # self.pareto_alpha = pareto_alpha
        # self.min_time_improvement = min_time_improvement

        # Add memoization caches
        self.cost_cache: dict[frozenset[IndexConfig], float] = {}
        self._size_estimate_cache: dict[tuple[str, frozenset[str]], int] = {}
        self._table_size_cache = {}
        self._estimate_table_size_cache = {}
        self._explain_plans_cache = {}
        self._sql_bind_params = SqlBindParams(self.sql_driver)

        # Add trace accumulator
        self._dta_traces: list[str] = []

    async def analyze_workload(
        self,
        workload: list[dict[str, Any]] | None = None,
        sql_file: str | None = None,
        query_list: list[str] | None = None,
        min_calls: int = 50,
        min_avg_time_ms: float = 5.0,
        limit: int = MAX_NUM_INDEX_TUNING_QUERIES,
        max_index_size_mb: int = -1,
    ) -> DTASession:
        """
        Analyze query workload and recommend indexes.

        This method can analyze workload from three different sources (in order of priority):
        1. Explicit workload passed as a parameter
        2. Direct list of SQL queries passed as query_list
        3. SQL file with queries
        4. Query statistics from pg_stat_statements

        Args:
            workload: Optional explicit workload data
            sql_file: Optional path to a file containing SQL queries
            query_list: Optional list of SQL query strings to analyze
            min_calls: Minimum number of calls for a query to be considered (for pg_stat_statements)
            min_avg_time_ms: Minimum average execution time in ms (for pg_stat_statements)
            limit: Maximum number of queries to analyze (for pg_stat_statements)
            max_index_size_mb: Maximum total size of recommended indexes in MB

        Returns:
            DTASession with analysis results
        """
        session_id = str(int(time.time()))
        self._analysis_start_time = time.time()
        self._dta_traces = []  # Reset traces at start of analysis

        # Clear the cache at the beginning of each analysis
        self._size_estimate_cache = {}

        if max_index_size_mb > 0:
            self.budget_mb = max_index_size_mb

        session = DTASession(
            session_id=session_id,
            budget_mb=max_index_size_mb,
        )

        try:
            # Run pre-checks
            precheck_result = await self._run_prechecks(session)
            if precheck_result:
                return precheck_result

            # First try to use explicit workload if provided
            if workload:
                logger.debug(f"Using explicit workload with {len(workload)} queries")
                session.workload_source = "args"
                session.workload = workload
            # Then try direct query list if provided
            elif query_list:
                logger.debug(f"Using provided query list with {len(query_list)} queries")
                session.workload_source = "query_list"
                session.workload = []
                for i, query in enumerate(query_list):
                    # Create a synthetic workload entry for each query
                    session.workload.append(
                        {
                            "query": query,
                            "queryid": f"direct-{i}",
                        }
                    )

            # Then try SQL file if provided
            elif sql_file:
                logger.debug(f"Reading queries from file: {sql_file}")
                session.workload_source = "sql_file"
                session.workload = self._get_workload_from_file(sql_file)

            # Finally fall back to query stats
            else:
                logger.debug("Using query statistics from the database")
                session.workload_source = "query_store"
                session.workload = await self._get_query_stats(min_calls, min_avg_time_ms, limit)

            if not session.workload:
                logger.warning("No workload to analyze")
                return session

            session.workload = await self._validate_and_parse_workload(session.workload)

            query_weights = self._covert_workload_to_query_weights(session.workload)

            if query_weights is None or len(query_weights) == 0:
                self.dta_trace("No query provided")
                session.recommendations = []
            else:
                # Gather queries as strings
                workload_queries = [q for q, _, _ in query_weights]

                self.dta_trace(f"Workload queries ({len(workload_queries)}): {_pp_list(workload_queries)}")

                # get existing indexes
                existing_defs = {idx["definition"] for idx in await self._get_existing_indexes()}

                logger.debug(f"Existing indexes ({len(existing_defs)}): {_pp_list(existing_defs)}")

                # Generate and evaluate index recommendations
                recommendations = await self._generate_recommendations(query_weights, existing_defs)
                session.recommendations = await self._format_recommendations(query_weights, recommendations)

                # Reset HypoPG only once at the end
                await self.sql_driver.execute_query("SELECT hypopg_reset();")

        except Exception as e:
            logger.error(f"Error in workload analysis: {e}", exc_info=True)
            session.error = f"Error in workload analysis: {e}"

        session.dta_traces = self._dta_traces
        return session

    async def _run_prechecks(self, session: DTASession) -> DTASession | None:
        """
        Run pre-checks before analysis and return a session with error if any check fails.

        Args:
            session: The current DTASession object

        Returns:
            The DTASession with error information if any check fails, None if all checks pass
        """
        # Pre-check 1: Check HypoPG with more granular feedback
        # Use our new utility function to check HypoPG status
        is_hypopg_installed, hypopg_message = await check_hypopg_installation_status(self.sql_driver)

        # If hypopg is not installed or not available, add error to session
        if not is_hypopg_installed:
            session.error = hypopg_message
            return session

        # Pre-check 2: Check if ANALYZE has been run at least once
        result = await self.sql_driver.execute_query("SELECT s.last_analyze FROM pg_stat_user_tables s ORDER BY s.last_analyze LIMIT 1;")
        if not result or not any(row.cells.get("last_analyze") is not None for row in result):
            error_message = (
                "Statistics are not up-to-date. The database needs to be analyzed first. "
                "Please run 'ANALYZE;' on your database before using the tuning advisor. "
                "Without up-to-date statistics, the index recommendations may be inaccurate."
            )
            session.error = error_message
            logger.error(error_message)
            return session

        # All checks passed
        return None

    async def _get_existing_indexes(self) -> list[dict[str, Any]]:
        """Get existing indexes"""
        query = """
        SELECT schemaname as schema,
               tablename as table,
               indexname as name,
               indexdef as definition
        FROM pg_indexes
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY schemaname, tablename, indexname
        """
        result = await self.sql_driver.execute_query(query)
        if result is not None:
            return [dict(row.cells) for row in result]
        return []

    async def _validate_and_parse_workload(self, workload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate the workload to ensure it is analyzable."""
        validated_workload = []
        for q in workload:
            query_text = q["query"]
            if not query_text:
                logger.debug("Skipping empty query")
                continue
            query_text = query_text.strip().lower()

            # Replace parameter placeholders with dummy values
            query_text = await self._sql_bind_params.replace_parameters(query_text)

            parsed = parse_sql(query_text)
            if not parsed:
                logger.debug(f"Skipping non-parseable query: {query_text[:50]}...")
                continue
            stmt = parsed[0].stmt
            if not self._is_analyzable_stmt(stmt):
                logger.debug(f"Skipping non-analyzable query: {query_text[:50]}...")
                continue

            q["query"] = query_text
            q["stmt"] = stmt
            validated_workload.append(q)
        return validated_workload

    def _covert_workload_to_query_weights(self, workload: list[dict[str, Any]]) -> list[tuple[str, SelectStmt, float]]:
        """Convert workload to query weights based on query frequency."""
        return [(q["query"], q["stmt"], self.convert_query_info_to_weight(q)) for q in workload]

    def convert_query_info_to_weight(self, query_info: dict[str, Any]) -> float:
        """Convert query info to weight based on query frequency."""
        return query_info.get("calls", 1.0) * query_info.get("avg_exec_time", 1.0)

    async def get_explain_plan_with_indexes(self, query_text: str, indexes: frozenset[IndexConfig]) -> dict[str, Any]:
        """
        Get the explain plan for a query with a specific set of indexes.
        Results are memoized to avoid redundant explain operations.

        Args:
            query_text: The SQL query to explain
            indexes: A frozenset of IndexConfig objects representing the indexes to enable

        Returns:
            The explain plan as a dictionary
        """
        # Create a cache key from the query and indexes
        cache_key = (query_text, indexes)

        # Return cached result if available
        existing_plan = self._explain_plans_cache.get(cache_key)
        if existing_plan:
            return existing_plan

        # Generate the plan using the static method
        explain_plan_tool = ExplainPlanTool(self.sql_driver)
        plan = await explain_plan_tool.generate_explain_plan_with_hypothetical_indexes(query_text, indexes, False, self)

        # Cache the result
        self._explain_plans_cache[cache_key] = plan
        return plan

    def _get_workload_from_file(self, file_path: str) -> list[dict[str, Any]]:
        """Load queries from an SQL file."""
        try:
            with open(file_path) as f:
                content = f.read()

            # Split the file content by semicolons to get individual queries
            query_texts = [q.strip() for q in content.split(";") if q.strip()]
            queries = []

            for i, text in enumerate(query_texts):
                queries.append(
                    {
                        "queryid": i,
                        "query": text,
                    }
                )

            return queries
        except Exception as e:
            raise ValueError(f"Error loading queries from file {file_path}") from e

    async def _get_query_stats(self, min_calls: int, min_avg_time_ms: float, limit: int) -> list[dict[str, Any]]:
        """Get query statistics from pg_stat_statements"""

        # Reference to original implementation
        return await self._get_query_stats_direct(min_calls, min_avg_time_ms, limit)

    async def _get_query_stats_direct(self, min_calls: int = 50, min_avg_time_ms: float = 5.0, limit: int = 100) -> list[dict[str, Any]]:
        """Direct implementation of query stats collection."""
        query = """
        SELECT queryid, query, calls, total_exec_time/calls as avg_exec_time
        FROM pg_stat_statements
        WHERE calls >= {}
        AND total_exec_time/calls >= {}
        ORDER BY total_exec_time DESC
        LIMIT {}
        """
        result = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
            [min_calls, min_avg_time_ms, limit],
        )
        return [dict(row.cells) for row in result] if result else []

    def _is_analyzable_stmt(self, stmt: Any) -> bool:
        """Check if a statement can be analyzed for index recommendations."""
        # It should be a SelectStmt
        if not isinstance(stmt, SelectStmt):
            return False

        visitor = TableAliasVisitor()
        visitor(stmt)

        # Skip queries that only access system tables
        if all(table.startswith("pg_") or table.startswith("aurora_") for table in visitor.tables):
            return False
        return True

    def _index_exists(self, index: Index, existing_defs: set[str]) -> bool:
        """Check if an index with the same table, columns, and type already exists in the database.

        Uses pglast to parse index definitions and compare their structure rather than
        doing simple string matching.
        """
        from pglast import parser

        try:
            # Parse the candidate index
            candidate_stmt = parser.parse_sql(index.definition)[0]
            candidate_node = candidate_stmt.stmt

            # Extract key information from candidate index
            candidate_info = self._extract_index_info(candidate_node)

            # If we couldn't parse the candidate index, fall back to string comparison
            if not candidate_info:
                return index.definition in existing_defs

            # Check each existing index
            for existing_def in existing_defs:
                try:
                    # Skip if it's obviously not an index
                    if not ("CREATE INDEX" in existing_def.upper() or "CREATE UNIQUE INDEX" in existing_def.upper()):
                        continue

                    # Parse the existing index
                    existing_stmt = parser.parse_sql(existing_def)[0]
                    existing_node = existing_stmt.stmt

                    # Extract key information
                    existing_info = self._extract_index_info(existing_node)

                    # Compare the key components
                    if existing_info and self._is_same_index(candidate_info, existing_info):
                        return True
                except Exception as e:
                    raise ValueError("Error parsing existing index") from e

            return False
        except Exception as e:
            raise ValueError("Error in robust index comparison") from e

    def _extract_index_info(self, node) -> dict[str, Any] | None:
        """Extract key information from a parsed index node."""
        try:
            # Handle differences in node structure between pglast versions
            if hasattr(node, "IndexStmt"):
                index_stmt = node.IndexStmt
            else:
                index_stmt = node

            # Extract table name
            if hasattr(index_stmt.relation, "relname"):
                table_name = index_stmt.relation.relname
            else:
                # Extract from RangeVar
                table_name = index_stmt.relation.RangeVar.relname

            # Extract columns
            columns = []
            for idx_elem in index_stmt.indexParams:
                if hasattr(idx_elem, "name") and idx_elem.name:
                    columns.append(idx_elem.name)
                elif hasattr(idx_elem, "IndexElem") and idx_elem.IndexElem:
                    columns.append(idx_elem.IndexElem.name)
                elif hasattr(idx_elem, "expr") and idx_elem.expr:
                    # Convert the expression to a proper string representation
                    expr_str = self._ast_expr_to_string(idx_elem.expr)
                    columns.append(expr_str)
            # Extract index type
            index_type = "btree"  # default
            if hasattr(index_stmt, "accessMethod") and index_stmt.accessMethod:
                index_type = index_stmt.accessMethod

            # Check if unique
            is_unique = False
            if hasattr(index_stmt, "unique"):
                is_unique = index_stmt.unique

            return {
                "table": table_name.lower(),
                "columns": [col.lower() for col in columns],
                "type": index_type.lower(),
                "unique": is_unique,
            }
        except Exception as e:
            self.dta_trace(f"Error extracting index info: {e}")
            raise ValueError("Error extracting index info") from e

    def _ast_expr_to_string(self, expr) -> str:
        """Convert an AST expression (like FuncCall) to a proper string representation.

        For example, converts a FuncCall node representing lower(name) to "lower(name)"
        """
        try:
            # Import FuncCall and ColumnRef for type checking
            from pglast.ast import ColumnRef
            from pglast.ast import FuncCall

            # Check for FuncCall type directly
            if isinstance(expr, FuncCall):
                # Extract function name
                if hasattr(expr, "funcname") and expr.funcname:
                    func_name = ".".join([name.sval for name in expr.funcname if hasattr(name, "sval")])
                else:
                    func_name = "unknown_func"

                # Extract arguments
                args = []
                if hasattr(expr, "args") and expr.args:
                    for arg in expr.args:
                        args.append(self._ast_expr_to_string(arg))

                # Format as function call
                return f"{func_name}({','.join(args)})"

            # Check for ColumnRef type directly
            elif isinstance(expr, ColumnRef):
                if hasattr(expr, "fields") and expr.fields:
                    return ".".join([field.sval for field in expr.fields if hasattr(field, "sval")])
                return "unknown_column"

            # Try to handle direct values
            elif hasattr(expr, "sval"):  # String value
                return expr.sval
            elif hasattr(expr, "ival"):  # Integer value
                return str(expr.ival)
            elif hasattr(expr, "fval"):  # Float value
                return expr.fval

            # Fallback for other expression types
            return str(expr)
        except Exception as e:
            raise ValueError("Error converting expression to string") from e

    def _is_same_index(self, index1: dict[str, Any], index2: dict[str, Any]) -> bool:
        """Check if two indexes are functionally equivalent."""
        if not index1 or not index2:
            return False

        # Same table?
        if index1["table"] != index2["table"]:
            return False

        # Same index type?
        if index1["type"] != index2["type"]:
            return False

        # Same columns (order matters for most index types)?
        if index1["columns"] != index2["columns"]:
            # For hash indexes, order doesn't matter
            if index1["type"] == "hash" and set(index1["columns"]) == set(index2["columns"]):
                return True
            return False

        # If one is unique and the other is not, they're different
        # Except when a primary key (which is unique) exists and we're considering a non-unique index on same column
        if index1["unique"] and not index2["unique"]:
            return False

        # Same core definition
        return True

    def dta_trace(self, message: Any, exc_info: bool = False):
        """Convenience function to log DTA thinking process."""

        # Always log to debug
        if exc_info:
            logger.debug(message, exc_info=True)
        else:
            logger.debug(message)

        self._dta_traces.append(message)

    async def _evaluate_configuration_cost(
        self,
        weighted_workload: list[tuple[str, SelectStmt, float]],
        indexes: frozenset[IndexConfig],
    ) -> float:
        """Evaluate total cost with selective enabling and caching."""
        # Use indexes as cache key
        if indexes in self.cost_cache:
            self.dta_trace(f"  - Using cached cost for configuration: {candidate_str(indexes)}")
            return self.cost_cache[indexes]

        self.dta_trace(f"  - Evaluating cost for configuration: {candidate_str(indexes)}")

        total_cost = 0.0
        valid_queries = 0

        try:
            # Calculate cost for all queries with this configuration
            for query_text, _stmt, weight in weighted_workload:
                try:
                    # Get the explain plan using our memoized helper
                    plan_data = await self.get_explain_plan_with_indexes(query_text, indexes)

                    # Extract cost from the plan data
                    cost = self.extract_cost_from_json_plan(plan_data)
                    total_cost += cost * weight
                    valid_queries += 1
                except Exception as e:
                    raise ValueError(f"Error executing explain for query: {query_text}") from e

            if valid_queries == 0:
                self.dta_trace("    + no valid queries found for cost evaluation")
                return float("inf")

            avg_cost = total_cost / valid_queries
            self.cost_cache[indexes] = avg_cost
            self.dta_trace(f"    + config cost: {avg_cost:.2f} (from {valid_queries} queries)")
            return avg_cost

        except Exception as e:
            self.dta_trace(f"    + error evaluating configuration: {e}")
            raise ValueError("Error evaluating configuration") from e

    async def _estimate_index_size(self, table: str, columns: list[str]) -> int:
        # Create a hashable key for the cache
        cache_key = (table, frozenset(columns))

        # Check if we already have a cached result
        if cache_key in self._size_estimate_cache:
            return self._size_estimate_cache[cache_key]

        try:
            # Use parameterized query instead of f-string for security
            stats_query = """
            SELECT COALESCE(SUM(avg_width), 0) AS total_width,
                   COALESCE(SUM(n_distinct), 0) AS total_distinct
            FROM pg_stats
            WHERE tablename = {} AND attname = ANY({})
            """
            result = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                stats_query,
                [table, columns],
            )
            if result and result[0].cells:
                size_estimate = self._estimate_index_size_internal(dict(result[0].cells))

                # Cache the result
                self._size_estimate_cache[cache_key] = size_estimate
                return size_estimate
            return 0
        except Exception as e:
            raise ValueError("Error estimating index size") from e

    def _estimate_index_size_internal(self, stats: dict[str, Any]) -> int:
        width = (stats["total_width"] or 0) + 8  # 8 bytes for the heap TID
        ndistinct = stats["total_distinct"] or 1.0
        ndistinct = ndistinct if ndistinct > 0 else 1.0
        # simplistic formula
        size_estimate = int(width * ndistinct * 2.0)
        return size_estimate

    async def _format_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], best_config: tuple[set[IndexConfig], float]
    ) -> list[IndexRecommendation]:
        """Format recommendations into a list of IndexRecommendation objects."""
        # build final recommendations from best_config
        recommendations: list[IndexRecommendation] = []
        total_size = 0
        budget_bytes = self.budget_mb * 1024 * 1024
        individual_base_cost = await self._evaluate_configuration_cost(query_weights, frozenset()) or 1.0
        progressive_base_cost = individual_base_cost
        indexes_so_far = []
        for index_config in best_config[0]:
            indexes_so_far.append(index_config)
            # Calculate the cost with only this index
            progressive_cost = await self._evaluate_configuration_cost(
                query_weights,
                frozenset(indexes_so_far),  # Indexes so far
            )
            individual_cost = await self._evaluate_configuration_cost(
                query_weights,
                frozenset([index_config]),  # Only this index
            )

            size = await self._estimate_index_size(index_config.table, list(index_config.columns))
            if budget_bytes < 0 or total_size + size <= budget_bytes:
                self.dta_trace(f"Adding index: {candidate_str([index_config])}")
                rec = IndexRecommendation(
                    table=index_config.table,
                    columns=index_config.columns,
                    using=index_config.using,
                    potential_problematic_reason=index_config.potential_problematic_reason,
                    estimated_size_bytes=size,
                    progressive_base_cost=progressive_base_cost,
                    progressive_recommendation_cost=progressive_cost,
                    individual_base_cost=individual_base_cost,
                    individual_recommendation_cost=individual_cost,
                    queries=[q for q, _, _ in query_weights],
                    definition=index_config.definition,
                )
                progressive_base_cost = progressive_cost
                recommendations.append(rec)
                total_size += size
            else:
                self.dta_trace(f"Skipping index: {candidate_str([index_config])} because it exceeds budget")

        return recommendations

    @staticmethod
    def extract_cost_from_json_plan(plan_data: dict[str, Any]) -> float:
        """Extract total cost from JSON EXPLAIN plan data."""
        try:
            if not plan_data:
                return float("inf")

            # Parse JSON plan
            top_plan = plan_data.get("Plan")
            if not top_plan:
                logger.error("No top plan found in plan data: %s", plan_data)
                return float("inf")

            # Extract total cost from top plan
            total_cost = top_plan.get("Total Cost")
            if total_cost is None:
                logger.error("Total Cost not found in top plan: %s", top_plan)
                return float("inf")

            return float(total_cost)
        except (IndexError, KeyError, ValueError, json.JSONDecodeError) as e:
            raise ValueError("Error extracting cost from plan") from e

    @abstractmethod
    async def _generate_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], existing_index_defs: set[str]
    ) -> tuple[set[IndexConfig], float]:
        """Generate index tuning queries."""
        pass
