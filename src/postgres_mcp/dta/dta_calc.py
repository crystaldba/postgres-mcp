import json
import logging
import re
import time
from dataclasses import dataclass
from dataclasses import field
from itertools import combinations
from typing import Any
from typing import Iterable

import humanize
from pglast import parse_sql
from pglast.ast import A_Expr
from pglast.ast import ColumnRef
from pglast.ast import JoinExpr
from pglast.ast import Node
from pglast.ast import RangeVar
from pglast.ast import SelectStmt
from pglast.ast import SortBy
from pglast.ast import SortGroupClause
from pglast.visitors import Visitor

from .artifacts import calculate_improvement_multiple
from .safe_sql import SafeSqlDriver
from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


# --- Data Classes ---


@dataclass(frozen=True)
class IndexConfig:
    """Immutable index configuration for hashing."""

    table: str
    columns: tuple[str, ...]
    using: str = "btree"
    potential_problematic_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": list(self.columns),
            "using": self.using,
            "definition": self.definition,
        }

    @property
    def definition(self) -> str:
        return f"CREATE INDEX {self.name} ON {self.table} USING {self.using} ({', '.join(self.columns)})"

    @property
    def name(self) -> str:
        return (
            f"crystaldba_idx_{self.table}_{'_'.join(self.columns)}_{len(self.columns)}"
            + ("" if self.using == "btree" else f"_{self.using}")
        )

    def __str__(self) -> str:
        return self.definition

    def __repr__(self) -> str:
        return self.definition


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
        self.index_config = IndexConfig(
            table, columns, using, potential_problematic_reason
        )
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
        return (
            self.index_config.__repr__() + f" (estimated_size: {self.estimated_size})"
        )


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
        return calculate_improvement_multiple(
            self.progressive_base_cost, self.progressive_recommendation_cost
        )

    @property
    def individual_improvement_multiple(self) -> float:
        """Calculate the individual percentage improvement from this recommendation."""
        return calculate_improvement_multiple(
            self.individual_base_cost, self.individual_recommendation_cost
        )


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


class DatabaseTuningAdvisor:
    def __init__(
        self,
        sql_driver: SqlDriver,
        budget_mb: int = -1,  # no limit by default
        max_runtime_seconds: int = 30,  # 30 seconds
        max_index_width: int = 3,
        min_column_usage: int = 1,  # skip columns used in fewer than this many queries
        seed_columns_count: int = 3,  # how many single-col seeds to pick
        pareto_alpha: float = 2.0,
        min_time_improvement: float = 0.1,
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
        self.budget_mb = budget_mb
        self.max_runtime_seconds = max_runtime_seconds
        self.max_index_width = max_index_width
        self.min_column_usage = min_column_usage
        self.seed_columns_count = seed_columns_count
        self._analysis_start_time = 0.0
        self.pareto_alpha = pareto_alpha
        self.min_time_improvement = min_time_improvement

        # Add memoization caches
        self.cost_cache: dict[frozenset[IndexConfig], float] = {}
        self._size_estimate_cache: dict[tuple[str, frozenset[str]], int] = {}
        self._table_size_cache = {}
        self._estimate_table_size_cache = {}
        self._explain_plans_cache = {}
        self._column_stats_cache = {}

        # Add trace accumulator
        self._dta_traces: list[str] = []

    def _run_prechecks(self, session: DTASession) -> DTASession | None:
        """
        Run pre-checks before analysis and return a session with error if any check fails.

        Args:
            session: The current DTASession object

        Returns:
            The DTASession with error information if any check fails, None if all checks pass
        """
        # Pre-check 1: Check HypoPG with more granular feedback
        # First check if HypoPG is installed
        installed_result = self.sql_driver.execute_query(
            "SELECT 1 FROM pg_extension WHERE extname = 'hypopg'"
        )
        if installed_result:
            # Case 1: HypoPG is installed and ready to use
            pass
        else:
            # Check if HypoPG is available but not installed
            available_result = self.sql_driver.execute_query(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'hypopg'"
            )
            if available_result:
                # Case 2: Available but not installed
                error_message = (
                    "The HypoPG extension is available but not installed. "
                    "Please connect to your database and run: CREATE EXTENSION hypopg;"
                )
                session.error = error_message
                logger.error(error_message)
                return session
            else:
                # Case 3: Not available at all
                error_message = (
                    "The HypoPG extension is not available on this PostgreSQL server. "
                    "To install HypoPG:\n"
                    "1. For Debian/Ubuntu: sudo apt-get install postgresql-hypopg\n"
                    "2. For RHEL/CentOS: sudo yum install postgresql-hypopg\n"
                    "3. For MacOS with Homebrew: brew install hypopg\n"
                    "4. For other systems, build from source: git clone https://github.com/HypoPG/hypopg\n"
                    "After installing the extension packages, connect to your database and run: CREATE EXTENSION hypopg;"
                )
                session.error = error_message
                logger.error(error_message)
                return session

        # Pre-check 2: Check if ANALYZE has been run at least once
        result = self.sql_driver.execute_query(
            "SELECT s.last_analyze FROM pg_stat_user_tables s LIMIT 1"
        )
        if not result or all(row.cells.get("last_analyze") is None for row in result):
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

    def analyze_workload(
        self,
        workload: list[dict[str, Any]] | None = None,
        sql_file: str | None = None,
        query_list: list[str] | None = None,
        min_calls: int = 50,
        min_avg_time_ms: float = 5.0,
        limit: int = 100,
        max_index_size_mb: int = -1,
        max_runtime_seconds: int = 30,  # 30 seconds
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
            precheck_result = self._run_prechecks(session)
            if precheck_result:
                return precheck_result

            # First try to use explicit workload if provided
            if workload:
                logger.debug(f"Using explicit workload with {len(workload)} queries")
                session.workload_source = "args"
                session.workload = workload
            # Then try direct query list if provided
            elif query_list:
                logger.debug(
                    f"Using provided query list with {len(query_list)} queries"
                )
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
                session.workload = self._get_query_stats(
                    min_calls, min_avg_time_ms, limit
                )

            if not session.workload:
                logger.warning("No workload to analyze")
                return session

            session.workload = self._validate_and_parse_workload(session.workload)

            query_weights = self._covert_workload_to_query_weights(session.workload)

            # Generate and evaluate index recommendations
            session.recommendations = self._generate_recommendations(query_weights)
            # Reset HypoPG only once at the end
            self.sql_driver.execute_query("SELECT hypopg_reset();")

        except Exception as e:
            logger.error(f"Error in workload analysis: {e}", exc_info=True)
            session.error = str(e)

        session.dta_traces = self._dta_traces
        return session

    def _validate_and_parse_workload(
        self, workload: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate the workload to ensure it is analyzable."""
        validated_workload = []
        for q in workload:
            query_text = q["query"]
            if not query_text:
                logger.debug("Skipping empty query")
                continue
            query_text = query_text.strip().lower()

            # Replace parameter placeholders with dummy values
            query_text = self._replace_parameters(query_text)

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

    def _covert_workload_to_query_weights(
        self, workload: list[dict[str, Any]]
    ) -> list[tuple[str, SelectStmt, float]]:
        """Convert workload to query weights based on query frequency."""
        return [
            (q["query"], q["stmt"], self.convert_query_info_to_weight(q))
            for q in workload
        ]

    def convert_query_info_to_weight(self, query_info: dict[str, Any]) -> float:
        """Convert query info to weight based on query frequency."""
        return query_info.get("calls", 1.0) * query_info.get("avg_exec_time", 1.0)

    def _generate_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]]
    ) -> list[IndexRecommendation]:
        """Generate index recommendations using a hybrid 'seed + greedy' approach with a time cutoff."""
        # Gather queries as strings
        workload_queries = [q for q, _, _ in query_weights]

        self.dta_trace(
            f"Workload queries ({len(workload_queries)}): {self._pp_list(workload_queries)}"
        )

        # get existing indexes
        existing_defs = {idx["definition"] for idx in self._get_existing_indexes()}

        logger.debug(
            f"Existing indexes ({len(existing_defs)}): {self._pp_list(existing_defs)}"
        )

        # generate initial candidates
        all_candidates = self.generate_candidates(query_weights, existing_defs)

        self.dta_trace(
            f"All candidates ({len(all_candidates)}): {self.candidate_str(all_candidates)}"
        )

        # TODO: Remove this once we have a better way to generate seeds
        # # produce seeds if desired
        # seeds = set()
        # if self.seed_columns_count > 0 and not self._check_time():
        #     seeds = self._quick_pass_seeds(query_weights, all_candidates)

        # unify seeds with an empty set
        #   we treat seeds as "starting points"
        #   in the real DTA approach, they'd enumerate many seeds,
        #   but let's just do: [seeds, empty]
        # Because we do a small scale approach, we only do these 2 seeds
        seeds_list = [
            # seeds,
            set(),
        ]

        best_config: tuple[set[IndexConfig], float] = (set(), float("inf"))
        recommendations: list[IndexRecommendation] = []

        # Evaluate each seed
        for seed in seeds_list:
            if self._check_time():
                break

            self.dta_trace("Evaluating seed:")
            current_cost = self._evaluate_configuration_cost(
                query_weights, frozenset(seed)
            )
            candidate_indexes = set(
                {
                    IndexConfig(
                        c.table,
                        tuple(c.columns),
                        c.using,
                        c.potential_problematic_reason,
                    )
                    for c in all_candidates
                }
            )
            final_indexes, final_cost = self._enumerate_greedy(
                query_weights, seed.copy(), current_cost, candidate_indexes - seed
            )

            if final_cost < best_config[1]:
                best_config = (final_indexes, final_cost)

        # build final recommendations from best_config
        total_size = 0
        budget_bytes = self.budget_mb * 1024 * 1024
        individual_base_cost = (
            self._evaluate_configuration_cost(query_weights, frozenset()) or 1.0
        )
        progressive_base_cost = individual_base_cost
        indexes_so_far = []
        for index_config in best_config[0]:
            indexes_so_far.append(index_config)
            # Calculate the cost with only this index
            progressive_cost = self._evaluate_configuration_cost(
                query_weights,
                frozenset(indexes_so_far),  # Indexes so far
            )
            individual_cost = self._evaluate_configuration_cost(
                query_weights,
                frozenset([index_config]),  # Only this index
            )

            size = self._estimate_index_size(
                index_config.table, list(index_config.columns)
            )
            if budget_bytes < 0 or total_size + size <= budget_bytes:
                self.dta_trace(f"Adding index: {self.candidate_str([index_config])}")
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
                    queries=workload_queries,
                    definition=index_config.definition,
                )
                progressive_base_cost = progressive_cost
                recommendations.append(rec)
                total_size += size
            else:
                self.dta_trace(
                    f"Skipping index: {self.candidate_str([index_config])} because it exceeds budget"
                )

        # Sort recs by benefit desc
        return recommendations

    def generate_candidates(
        self, workload: list[tuple[str, SelectStmt, float]], existing_defs: set[str]
    ) -> list[Index]:
        """Generates index candidates from queries, with batch creation."""
        table_columns_usage = {}  # table -> {col -> usage_count}
        # Extract columns from all queries
        for q, stmt, _ in workload:
            columns_per_table = self.extract_stmt_columns(q, stmt)
            for tbl, cols in columns_per_table.items():
                if tbl not in table_columns_usage:
                    table_columns_usage[tbl] = {}
                for c in cols:
                    table_columns_usage[tbl][c] = table_columns_usage[tbl].get(c, 0) + 1

        # Filter out rarely used columns
        # e.g. skip columns that appear in fewer than self.min_column_usage queries
        table_columns: dict[str, set[str]] = {}
        for tbl, usage_map in table_columns_usage.items():
            kept_cols = {
                c for c, usage in usage_map.items() if usage >= self.min_column_usage
            }
            if kept_cols:
                table_columns[tbl] = kept_cols

        candidates = []
        for table, cols in table_columns.items():
            # TODO: Optimize by prioritizing columns from filters/joins; current approach generates all combinations
            col_list = list(cols)
            for width in range(1, min(self.max_index_width, len(cols)) + 1):
                for combo in combinations(col_list, width):
                    candidates.append(Index(table=table, columns=tuple(combo)))

        # filter out duplicates with existing indexes
        filtered_candidates = [
            c for c in candidates if not self._index_exists(c, existing_defs)
        ]

        # filter out candidates with columns not used in query conditions
        condition_filtered1 = self._filter_candidates_by_query_conditions(
            workload, filtered_candidates
        )

        # filter out long text columns
        condition_filtered = self._filter_long_text_columns(condition_filtered1)

        self.dta_trace(f"Generated {len(candidates)} total candidates")
        self.dta_trace(
            f"Filtered to {len(filtered_candidates)} after removing existing indexes."
        )
        self.dta_trace(
            f"Filtered to {len(condition_filtered1)} after removing unused columns."
        )
        self.dta_trace(
            f"Filtered to {len(condition_filtered)} after removing long text columns."
        )
        # Batch create all hypothetical indexes and store their size estimates
        if len(condition_filtered) > 0:
            query = "SELECT hypopg_create_index({});" * len(condition_filtered)
            SafeSqlDriver.execute_param_query(
                self.sql_driver,
                query,
                [idx.definition for idx in condition_filtered],
            )

            # Get estimated sizes without resetting indexes yet
            result = self.sql_driver.execute_query(
                "SELECT index_name, hypopg_relation_size(indexrelid) as index_size FROM hypopg_list_indexes;"
            )
            if result is not None:
                index_map = {
                    r.cells["index_name"]: r.cells["index_size"] for r in result
                }
                for idx in condition_filtered:
                    if idx.name in index_map:
                        idx.estimated_size = index_map[idx.name]

            self.sql_driver.execute_query("SELECT hypopg_reset();")
        return condition_filtered

    def candidate_str(self, indexes: Iterable[Index] | Iterable[IndexConfig]) -> str:
        return (
            ", ".join(f"{idx.table}({','.join(idx.columns)})" for idx in indexes)
            if indexes
            else "(no indexes)"
        )

    def _evaluate_configuration_cost(
        self,
        weighted_workload: list[tuple[str, SelectStmt, float]],
        indexes: frozenset[IndexConfig],
    ) -> float:
        """Evaluate total cost with selective enabling and caching."""
        # Use indexes as cache key
        if indexes in self.cost_cache:
            self.dta_trace(
                f"  - Using cached cost for configuration: {self.candidate_str(indexes)}"
            )
            return self.cost_cache[indexes]

        self.dta_trace(
            f"  - Evaluating cost for configuration: {self.candidate_str(indexes)}"
        )

        total_cost = 0.0
        valid_queries = 0

        try:
            # Calculate cost for all queries with this configuration
            for query_text, _stmt, weight in weighted_workload:
                try:
                    # Get the explain plan using our memoized helper
                    plan_data = self.get_explain_plan_with_indexes(query_text, indexes)

                    # Extract cost from the plan data
                    cost = self.extract_cost_from_json_plan(plan_data)
                    total_cost += cost * weight
                    valid_queries += 1
                except Exception as e:
                    logger.error(
                        f"Error executing explain for query: {query_text} with error: {e}",
                        exc_info=True,
                    )

            if valid_queries == 0:
                self.dta_trace("    + no valid queries found for cost evaluation")
                return float("inf")

            avg_cost = total_cost / valid_queries
            self.cost_cache[indexes] = avg_cost
            self.dta_trace(
                f"    + config cost: {avg_cost:.2f} (from {valid_queries} queries)"
            )
            return avg_cost

        except Exception as e:
            logger.error(f"Error evaluating configuration: {e}", exc_info=True)
            self.dta_trace(f"    + error evaluating configuration: {e}")
            return float("inf")

    def _enumerate_greedy(
        self,
        queries: list[tuple[str, SelectStmt, float]],
        current_indexes: set[IndexConfig],
        current_cost: float,
        candidate_indexes: set[IndexConfig],
    ) -> tuple[set[IndexConfig], float]:
        """
        Pareto optimal greedy approach using cost/benefit analysis:
        - Cost: Size of base relation plus size of indexes (in bytes)
        - Benefit: Inverse of query execution time (1/time)
        - Objective function: log(time) + alpha * log(space)
        - We want to minimize this function, with alpha=2 for 2x emphasis on performance
        - Primary stopping criterion: minimum relative time improvement threshold
        """
        import math

        # Parameters
        alpha = self.pareto_alpha
        min_time_improvement = self.min_time_improvement  # 5% default

        self.dta_trace("\n[GREEDY SEARCH] Starting enumeration")
        self.dta_trace(
            f"  - Parameters: alpha={alpha}, min_time_improvement={min_time_improvement}"
        )
        self.dta_trace(
            f"  - Initial indexes: {len(current_indexes)}, Candidates: {len(candidate_indexes)}"
        )

        # Get the tables involved in this analysis
        tables = set()
        for idx in candidate_indexes:
            tables.add(idx.table)

        # Estimate base relation size for each table
        base_relation_size = sum(self._get_table_size(table) for table in tables)

        self.dta_trace(
            f"  - Base relation size: {humanize.naturalsize(base_relation_size)}"
        )

        # Calculate current indexes size
        indexes_size = sum(
            self._estimate_index_size(idx.table, list(idx.columns))
            for idx in current_indexes
        )

        # Total space is base relation plus indexes
        current_space = base_relation_size + indexes_size
        current_time = current_cost
        current_objective = math.log(current_time) + alpha * math.log(current_space)

        self.dta_trace(
            f"  - Initial configuration: Time={current_time:.2f}, "
            f"Space={humanize.naturalsize(current_space)} (Base: {humanize.naturalsize(base_relation_size)}, "
            f"Indexes: {humanize.naturalsize(indexes_size)}), "
            f"Objective={current_objective:.4f}"
        )

        added_indexes = []  # Keep track of added indexes in order
        iteration = 1

        while True:
            self.dta_trace(f"\n[ITERATION {iteration}] Evaluating candidates")
            best_index = None
            best_time = current_time
            best_space = current_space
            best_objective = current_objective

            for candidate in candidate_indexes:
                self.dta_trace(
                    f"Evaluating candidate: {self.candidate_str([candidate])}"
                )
                # Calculate additional size from this index
                index_size = self._estimate_index_size(
                    candidate.table, list(candidate.columns)
                )
                self.dta_trace(f"    + Index size: {humanize.naturalsize(index_size)}")
                # Total space with this index = current space + new index size
                test_space = current_space + index_size
                self.dta_trace(f"    + Total space: {humanize.naturalsize(test_space)}")

                # Check budget constraint
                if (
                    self.budget_mb > 0
                    and (test_space - base_relation_size) > self.budget_mb * 1024 * 1024
                ):
                    self.dta_trace(
                        f"  - Skipping candidate: {self.candidate_str([candidate])} because total "
                        f"index size ({humanize.naturalsize(test_space - base_relation_size)}) exceeds "
                        f"budget ({humanize.naturalsize(self.budget_mb * 1024 * 1024)})"
                    )
                    continue

                # Calculate new time (cost) with this index
                test_time = self._evaluate_configuration_cost(
                    queries, frozenset(current_indexes | {candidate})
                )
                self.dta_trace(f"    + Eval cost (time): {test_time}")
                # Calculate relative time improvement
                time_improvement = (current_time - test_time) / current_time

                # Calculate objective for this configuration
                test_objective = math.log(test_time) + alpha * math.log(test_space)
                # Skip if time improvement is below threshold
                if time_improvement < min_time_improvement:
                    self.dta_trace(
                        f"  - Skipping candidate: {self.candidate_str([candidate])} because time improvement is below threshold"
                    )
                    continue

                # Select the index with the best time improvement that meets our threshold
                if test_objective < best_objective:
                    self.dta_trace(
                        f"  - Updating best candidate: {self.candidate_str([candidate])}"
                    )
                    best_index = candidate
                    best_time = test_time
                    best_space = test_space
                    best_objective = test_objective
                else:
                    self.dta_trace(
                        f"  - Skipping candidate: {self.candidate_str([candidate])} because it doesn't have the best objective improvement"
                    )

            # If no improvement or no valid candidates, stop
            if best_index is None:
                self.dta_trace(
                    f"STOPPED SEARCH: No indexes found with time improvement >= {min_time_improvement:.2%}"
                )
                break

            # Calculate improvements/changes
            time_improvement = (current_time - best_time) / current_time
            space_increase = (best_space - current_space) / current_space
            objective_improvement = current_objective - best_objective

            # Log this step
            self.dta_trace(
                f"  - Selected index: {self.candidate_str([best_index])}"
                f"\n    + Time improvement: {time_improvement:.2%}"
                f"\n    + Space increase: {space_increase:.2%}"
                f"\n    + New objective: {best_objective:.4f} (improvement: {objective_improvement:.4f})"
            )

            # Add the best index and update metrics
            current_indexes.add(best_index)
            candidate_indexes.remove(best_index)
            added_indexes.append(best_index)

            # Update current metrics
            current_time = best_time
            current_space = best_space
            current_objective = best_objective

            iteration += 1

            # Check if we've exceeded the time limit after doing at least one iteration
            if self._check_time():
                self.dta_trace("STOPPED SEARCH: Time limit reached")
                break

        # Log final configuration
        self.dta_trace("\n[SEARCH COMPLETE]")
        if added_indexes:
            indexes_size = sum(
                self._estimate_index_size(idx.table, list(idx.columns))
                for idx in current_indexes
            )
            self.dta_trace(
                f"  - Final configuration: {len(added_indexes)} indexes added"
                f"\n    + Final time: {current_time:.2f}"
                f"\n    + Final space: {humanize.naturalsize(current_space)} (Base: {humanize.naturalsize(base_relation_size)}, "
                f"Indexes: {humanize.naturalsize(indexes_size)})"
                f"\n    + Final objective: {current_objective:.4f}"
            )
        else:
            self.dta_trace("No indexes added - baseline configuration is optimal")

        return current_indexes, current_time

    def _get_table_size(self, table: str) -> int:
        """
        Get the total size of a table including indexes and toast tables.
        Uses memoization to avoid repeated database queries.

        Args:
            table: The name of the table

        Returns:
            Size of the table in bytes
        """
        # Check if we have a cached result
        if table in self._table_size_cache:
            return self._table_size_cache[table]

        # Try to get table size from the database using proper quoting
        try:
            # Use the proper way to calculate table size with quoted identifiers
            query = f"SELECT pg_total_relation_size(quote_ident('{table}')) as rel_size"
            result = self.sql_driver.execute_query(query)
            if result and len(result) > 0 and len(result[0].cells) > 0:
                size = int(result[0].cells["rel_size"])
                # Cache the result
                self._table_size_cache[table] = size
                return size
            else:
                # If query fails, use our estimation method
                size = self._estimate_table_size(table)
                self._table_size_cache[table] = size
                return size
        except Exception as e:
            logger.warning(f"Error getting table size for {table}: {e}")
            # Use estimation method
            size = self._estimate_table_size(table)
            self._table_size_cache[table] = size
            return size

    def _estimate_table_size(self, table: str) -> int:
        """Estimate the size of a table if we can't get it from the database."""
        try:
            # Try a simple query to get row count and then estimate size
            result = self.sql_driver.execute_query(
                f"SELECT count(*) as row_count FROM {table}"
            )
            if result and len(result) > 0 and len(result[0].cells) > 0:
                row_count = int(result[0].cells["row_count"])
                # Rough estimate: assume 1KB per row
                return row_count * 1024
        except Exception as e:
            logger.warning(f"Error estimating table size for {table}: {e}")

        # Default size if we can't estimate
        return 10 * 1024 * 1024  # 10MB default

    # def _quick_pass_seeds(self, weighted_workload: list[tuple[str, SelectStmt, float]], all_candidates: list[Index]) -> set[IndexConfig]:
    #     """Identify top single-column seed indexes efficiently."""
    #     single_col = [c for c in all_candidates if len(c.columns) == 1]
    #     # For each candidate, let's check the cost improvement
    #     # We'll do a naive approach: cost(workload) w/o indexes vs. with just that index.
    #     base_cost = self._evaluate_configuration_cost(weighted_workload, frozenset())
    #     if base_cost <= 0:
    #         return set()

    #     # measure improvement
    #     improvements = []
    #     for c in single_col:
    #         if self._check_time():
    #             break
    #         cfg_set = frozenset({IndexConfig(c.table, tuple(c.columns), c.using, c.potential_problematic_reason)})
    #         new_cost = self._evaluate_configuration_cost(weighted_workload, cfg_set)
    #         improvement = base_cost - new_cost if new_cost < base_cost else 0
    #         improvements.append((c, improvement))
    #     # pick top seed_columns_count
    #     improvements.sort(key=lambda x: x[1], reverse=True)
    #     chosen = [x[0] for x in improvements[: self.seed_columns_count] if x[1] > 0]
    #     return {IndexConfig(idx.table, tuple(idx.columns), idx.using, idx.potential_problematic_reason) for idx in chosen}

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
            logger.error(
                f"Error loading queries from file {file_path}: {e}", exc_info=True
            )
            return []

    def _get_query_stats(
        self, min_calls: int, min_avg_time_ms: float, limit: int
    ) -> list[dict[str, Any]]:
        """Get query statistics from pg_stat_statements"""

        # Reference to original implementation
        return self._get_query_stats_direct(min_calls, min_avg_time_ms, limit)

    def _get_query_stats_direct(
        self, min_calls: int = 50, min_avg_time_ms: float = 5.0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Direct implementation of query stats collection."""
        query = """
        SELECT queryid, query, calls, total_exec_time/calls as avg_exec_time
        FROM pg_stat_statements
        WHERE calls >= {}
        AND total_exec_time/calls >= {}
        ORDER BY total_exec_time DESC
        LIMIT {}
        """
        result = SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
            [min_calls, min_avg_time_ms, limit],
        )
        return [dict(row.cells) for row in result] if result else []

    def _check_time(self) -> bool:
        """Return True if we have exceeded max_runtime_seconds."""
        if self.max_runtime_seconds <= 0:
            return False
        elapsed = time.time() - self._analysis_start_time
        return elapsed > self.max_runtime_seconds

    def _is_analyzable_stmt(self, stmt: Any) -> bool:
        """Check if a statement can be analyzed for index recommendations."""
        # It should be a SelectStmt
        if not isinstance(stmt, SelectStmt):
            return False

        visitor = TableAliasVisitor()
        visitor(stmt)

        # Skip queries that only access system tables
        if all(
            table.startswith("pg_") or table.startswith("aurora_")
            for table in visitor.tables
        ):
            return False
        return True

    def extract_columns(self, query: str) -> dict[str, set[str]]:
        """Extract columns from a query using improved visitors."""
        try:
            parsed = parse_sql(query)
            if not parsed:
                return {}
            stmt = parsed[0].stmt
            if not isinstance(stmt, SelectStmt):
                self.dta_trace(f"Skipping non-SELECT query: {query[:50]}...")
                return {}

            return self.extract_stmt_columns(query, stmt)

        except Exception as e:
            self.dta_trace(f"Error parsing query '{query[:50]}...': {e}", exc_info=True)
            return {}

    def extract_stmt_columns(self, query: str, stmt: SelectStmt) -> dict[str, set[str]]:
        """Extract columns from a query using improved visitors."""
        try:
            # Second pass: collect columns with table context
            collector = ColumnCollector()
            collector(stmt)  # type: ignore

            return collector.columns

        except Exception as e:
            self.dta_trace(
                f"Error collecting columns from '{query[:50]}...': {e}", exc_info=True
            )
            return {}

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
                    if not (
                        "CREATE INDEX" in existing_def.upper()
                        or "CREATE UNIQUE INDEX" in existing_def.upper()
                    ):
                        continue

                    # Parse the existing index
                    existing_stmt = parser.parse_sql(existing_def)[0]
                    existing_node = existing_stmt.stmt

                    # Extract key information
                    existing_info = self._extract_index_info(existing_node)

                    # Compare the key components
                    if existing_info and self._is_same_index(
                        candidate_info, existing_info
                    ):
                        return True
                except Exception as e:
                    self.dta_trace(f"Error parsing existing index: {e}")
                    # If parsing fails, try substring matching as fallback
                    if index.table.lower() in existing_def.lower() and all(
                        col.lower() in existing_def.lower() for col in index.columns
                    ):
                        return True

            return False
        except Exception as e:
            logger.warning(f"Error in robust index comparison: {e}")
            # Fallback to simpler heuristic if parsing fails
            return any(
                index.table.lower() in existing_def.lower()
                and all(col.lower() in existing_def.lower() for col in index.columns)
                for existing_def in existing_defs
            )

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
                if hasattr(idx_elem, "name"):
                    columns.append(idx_elem.name)
                elif hasattr(idx_elem, "IndexElem"):
                    columns.append(idx_elem.IndexElem.name)

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
            return None

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
            if index1["type"] == "hash" and set(index1["columns"]) == set(
                index2["columns"]
            ):
                return True
            return False

        # If one is unique and the other is not, they're different
        # Except when a primary key (which is unique) exists and we're considering a non-unique index on same column
        if index1["unique"] and not index2["unique"]:
            return False

        # Same core definition
        return True

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
            logger.error(f"Error extracting cost from plan: {e}", exc_info=True)
            return float("inf")

    def _get_existing_indexes(self) -> list[dict[str, Any]]:
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
        result = self.sql_driver.execute_query(query)
        if result is not None:
            return [dict(row.cells) for row in result]
        return []

    def _estimate_index_size(self, table: str, columns: list[str]) -> int:
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
            result = SafeSqlDriver.execute_param_query(
                self.sql_driver,
                stats_query,
                [table, columns],
            )
            if result and result[0].cells:
                size_estimate = self._estimate_index_size_internal(
                    dict(result[0].cells)
                )

                # Cache the result
                self._size_estimate_cache[cache_key] = size_estimate
                return size_estimate
            return 0
        except Exception as e:
            logger.error(f"Error estimating index size: {e}", exc_info=True)
            return 0

    def _estimate_index_size_internal(self, stats: dict[str, Any]) -> int:
        width = (stats["total_width"] or 0) + 8  # 8 bytes for the heap TID
        ndistinct = stats["total_distinct"] or 1.0
        ndistinct = ndistinct if ndistinct > 0 else 1.0
        # simplistic formula
        size_estimate = int(width * ndistinct * 2.0)
        return size_estimate

    def _filter_candidates_by_query_conditions(
        self, workload: list[tuple[str, SelectStmt, float]], candidates: list[Index]
    ) -> list[Index]:
        """Filter out index candidates that contain columns not used in query conditions."""
        if not workload or not candidates:
            return candidates

        # Extract all columns used in conditions across all queries
        condition_columns = {}  # Dictionary of table -> set of columns

        for _, stmt, _ in workload:
            try:
                # Use our enhanced collector to extract condition columns
                collector = ConditionColumnCollector()
                collector(stmt)
                query_condition_columns = collector.condition_columns

                # Merge with overall condition columns
                for table, cols in query_condition_columns.items():
                    if table not in condition_columns:
                        condition_columns[table] = set()
                    condition_columns[table].update(cols)

            except Exception as e:
                logger.warning(f"Error extracting condition columns from query: {e}")

        # Filter candidates - keep only those where all columns are in condition_columns
        filtered_candidates = []
        for candidate in candidates:
            table = candidate.table
            if table not in condition_columns:
                continue

            # Check if all columns in the index are used in conditions
            all_columns_used = all(
                col in condition_columns[table] for col in candidate.columns
            )
            if all_columns_used:
                filtered_candidates.append(candidate)

        return filtered_candidates

    def _filter_long_text_columns(
        self, candidates: list[Index], max_text_length: int = 100
    ) -> list[Index]:
        """Filter out indexes that contain long text columns based on catalog information.

        Args:
            candidates: List of candidate indexes
            max_text_length: Maximum allowed text length (default: 100)

        Returns:
            Filtered list of indexes
        """
        if not candidates:
            return []

        # First, get all unique table.column combinations
        table_columns = set()
        for candidate in candidates:
            for column in candidate.columns:
                table_columns.add((candidate.table, column))

        # Create a list of table names for the query
        tables_array = ",".join(f"'{table}'" for table, _ in table_columns)
        columns_array = ",".join(f"'{col}'" for _, col in table_columns)

        # Query to get column types and their length limits from catalog
        type_query = f"""
            SELECT
                c.table_name,
                c.column_name,
                c.data_type,
                c.character_maximum_length,
                pg_stats.avg_width,
                CASE
                    WHEN c.data_type = 'text' THEN true
                    WHEN (c.data_type = 'character varying' OR c.data_type = 'varchar' OR
                         c.data_type = 'character' OR c.data_type = 'char') AND
                         (c.character_maximum_length IS NULL OR c.character_maximum_length > {max_text_length})
                    THEN true
                    ELSE false
                END as potential_long_text
            FROM information_schema.columns c
            LEFT JOIN pg_stats ON
                pg_stats.tablename = c.table_name AND
                pg_stats.attname = c.column_name
            WHERE c.table_name IN ({tables_array})
            AND c.column_name IN ({columns_array})
        """

        result = self.sql_driver.execute_query(type_query)

        logger.debug(f"Column types and length limits: {result}")

        if not result:
            logger.debug("No column types and length limits found")
            return []

        # Process results and identify problematic columns
        problematic_columns = set()
        potential_problematic_columns = set()

        for row in result:
            table = row.cells["table_name"]
            column = row.cells["column_name"]
            potential_long = row.cells["potential_long_text"]
            avg_width = row.cells.get("avg_width")

            # Use avg_width from pg_stats as a heuristic - if it's high, likely contains long text
            if potential_long and (
                avg_width is None or avg_width > max_text_length * 0.4
            ):
                problematic_columns.add((table, column))
                logger.debug(
                    f"Identified potentially long text column: {table}.{column} (avg_width: {avg_width})"
                )
            elif potential_long:
                potential_problematic_columns.add((table, column))

        # Filter candidates based on column information
        filtered_candidates = []
        for candidate in candidates:
            valid = True
            for column in candidate.columns:
                if (candidate.table, column) in problematic_columns:
                    valid = False
                    logger.debug(
                        f"Skipping index candidate with long text column: {candidate.table}.{column}"
                    )
                    break
                elif (candidate.table, column) in potential_problematic_columns:
                    candidate.potential_problematic_reason = "long_text_column"

            if valid:
                filtered_candidates.append(candidate)

        return filtered_candidates

    # --- Parameter replacement ---

    def _replace_parameters(self, query: str) -> str:
        """Replace parameter placeholders with appropriate values based on column statistics.

        This handles queries from pg_stat_statements where literals
        have been replaced with $1, $2, etc.
        """
        try:
            modified_query = query
            # Find all parameter placeholders
            param_matches = list(re.finditer(r"\$\d+", query))
            if not param_matches:
                logger.debug(f"No parameters found for query: {query[:50]}...")
                return query

            # Handle common special cases in a specific order to prevent incorrect replacements

            # 1. Handle LIMIT clauses - these should always be replaced with integers
            limit_pattern = re.compile(r"limit\s+\$(\d+)", re.IGNORECASE)
            modified_query = limit_pattern.sub(r"limit 100", modified_query)

            # 2. Handle static INTERVAL expressions
            interval_pattern = re.compile(
                r"interval\s+'(\d+)\s+([a-z]+)'", re.IGNORECASE
            )
            modified_query = interval_pattern.sub(
                lambda m: f"interval '2 {m.group(2)}'", modified_query
            )

            # 3. Handle parameterized INTERVAL expressions (INTERVAL $1)
            param_interval_pattern = re.compile(r"interval\s+\$(\d+)", re.IGNORECASE)
            modified_query = param_interval_pattern.sub(
                "interval '2 days'", modified_query
            )

            # 4. Handle OFFSET clauses - similar to LIMIT
            offset_pattern = re.compile(r"offset\s+\$(\d+)", re.IGNORECASE)
            modified_query = offset_pattern.sub(r"offset 0", modified_query)

            # Find all remaining parameter placeholders
            param_matches = list(re.finditer(r"\$\d+", modified_query))
            if not param_matches:
                self.dta_trace(
                    f"No further parameters found for query: {query[:50]}..."
                )
                return modified_query

            # Then, handle BETWEEN clauses as special cases
            between_pattern = re.compile(
                r"(\w+(?:\.\w+)?)\s+between\s+\$(\d+)\s+and\s+\$(\d+)", re.IGNORECASE
            )
            for match in between_pattern.finditer(query):
                column_ref, param1, param2 = match.groups()
                # Extract table and column name from the reference
                if "." in column_ref:
                    parts = column_ref.split(".")
                    alias = parts[0]
                    col_name = parts[1]
                    # Resolve the table alias
                    table_columns = self.extract_columns(query)
                    table_name = None
                    for tbl, _cols in table_columns.items():
                        if any(alias == a for a in self._get_table_aliases(query, tbl)):
                            table_name = tbl
                            break
                else:
                    # No alias, try to find the column in any table
                    col_name = column_ref
                    table_columns = self.extract_columns(query)
                    table_name = None
                    for tbl, cols in table_columns.items():
                        if col_name in cols:
                            table_name = tbl
                            break

                # Default numeric bounds if statistics not available
                lower_bound = 10
                upper_bound = 100
                if table_name and col_name:
                    stats = self._get_column_statistics(table_name, col_name)
                    if stats:
                        # Get appropriate values for both bounds
                        lower_bound = self._get_bound_values(stats, is_lower=True)
                        upper_bound = self._get_bound_values(stats, is_lower=False)

                # Replace both parameters in the BETWEEN clause
                param1_pattern = r"\$" + param1
                param2_pattern = r"\$" + param2
                self.dta_trace(
                    f"Replacing {param1_pattern} with {lower_bound} and {param2_pattern} with {upper_bound}"
                )
                modified_query = re.sub(
                    param1_pattern, str(lower_bound), modified_query
                )
                modified_query = re.sub(
                    param2_pattern, str(upper_bound), modified_query
                )

            # Now handle remaining parameters normally
            # Recompute matches after BETWEEN replacements
            param_matches = list(re.finditer(r"\$\d+", modified_query))
            if not param_matches:
                self.dta_trace(
                    f"No additional parameters found for query: {query[:50]}..."
                )
                return modified_query

            table_columns = self.extract_columns(query)
            if not table_columns:
                self.dta_trace(f"No table columns found for query: {query[:50]}...")
                return self._replace_parameters_generic(modified_query)

            # Process each remaining parameter
            for match in reversed(param_matches):
                param_position = match.start()

                # Extract a narrower context
                clause_start = max(
                    modified_query.rfind(" where ", 0, param_position),
                    modified_query.rfind(" and ", 0, param_position),
                    modified_query.rfind(" or ", 0, param_position),
                    modified_query.rfind(",", 0, param_position),
                    modified_query.rfind("(", 0, param_position),
                    -1,
                )

                if clause_start == -1:
                    clause_start = max(0, param_position - 100)

                preceding_text = modified_query[clause_start : param_position + 2]

                # Try to identify which column this parameter belongs to
                column_info = self._identify_parameter_column(
                    preceding_text, table_columns
                )
                if column_info:
                    table_name, column_name = column_info
                    stats = self._get_column_statistics(table_name, column_name)
                    if stats:
                        replacement = self._get_replacement_value(stats, preceding_text)
                    else:
                        replacement = self._get_generic_replacement(preceding_text)
                else:
                    replacement = self._get_generic_replacement(preceding_text)

                modified_query = (
                    modified_query[: match.start()]
                    + replacement
                    + modified_query[match.end() :]
                )

            return modified_query
        except Exception as e:
            logger.error(f"Error replacing parameters: {e}", exc_info=True)
            return query  # Return original if replacement fails

    def _get_bound_values(self, stats: dict[str, Any], is_lower: bool = True) -> Any:
        """Get appropriate bound values for range queries based on column statistics.

        Args:
            stats: Column statistics from pg_stats
            is_lower: True if we want the lower bound, False for upper bound

        Returns:
            Appropriate tight bound value based on available statistics
        """
        data_type = stats.get("data_type", "").lower()

        # First check for most common values - these are statistically most relevant
        common_vals = stats.get("common_vals")
        common_freqs = stats.get("common_freqs")

        if common_vals and common_freqs and len(common_vals) == len(common_freqs):
            # Use the most common value if available
            if len(common_vals) > 0:
                common_vals_list = list(common_vals)  # make sure it's a list
                common_freqs_list = list(common_freqs)  # make sure it's a list
                # Find the most frequent value
                max_freq_idx = common_freqs_list.index(max(common_freqs_list))
                most_common = common_vals_list[max_freq_idx]

                # For tight bounds, use the most common value with small adjustment
                try:
                    if isinstance(most_common, float):
                        # Small +/- adjustment around most common value
                        adjustment = abs(most_common) * 0.05 if most_common != 0 else 1
                        return (
                            most_common - adjustment
                            if is_lower
                            else most_common + adjustment
                        )
                    if isinstance(most_common, int):
                        # Small +/- adjustment around most common value
                        adjustment = abs(most_common) * 0.05 if most_common != 0 else 1
                        return (
                            int(most_common - adjustment)
                            if is_lower
                            else int(most_common + adjustment)
                        )
                    elif isinstance(most_common, str) and most_common.isdigit():
                        # For string digits, convert and adjust
                        num_val = float(most_common)
                        adjustment = abs(num_val) * 0.05 if num_val != 0 else 1
                        return (
                            str(int(num_val - adjustment))
                            if is_lower
                            else str(int(num_val + adjustment))
                        )
                    else:
                        # For non-numeric, just use most common
                        return most_common
                except (TypeError, ValueError):
                    # If adaptation fails, just use the value
                    return most_common

        # Next, try histogram bounds focusing on central values
        histogram_bounds = stats.get("histogram_bounds")
        if histogram_bounds and len(histogram_bounds) >= 3:
            # For tight bounds, use values very close to median
            median_idx = len(histogram_bounds) // 2
            # Only move 10% away from median in either direction for tight bounds
            idx_offset = max(1, len(histogram_bounds) // 10)

            if is_lower:
                bound_idx = max(0, median_idx - idx_offset)
            else:
                bound_idx = min(len(histogram_bounds) - 1, median_idx + idx_offset)

            return histogram_bounds[bound_idx]

        # Fall back to standard statistics if available
        most_common = (
            stats.get("most_common_vals", [None])[0]
            if stats.get("most_common_vals")
            else None
        )
        if most_common is not None:
            return most_common

        # Use very conservative defaults as last resort
        if "int" in data_type or data_type in ["smallint", "integer", "bigint"]:
            return 10 if is_lower else 20  # Very tight range
        elif data_type in ["numeric", "decimal", "real", "double precision", "float"]:
            return 10.0 if is_lower else 20.0  # Very tight range
        elif "date" in data_type or "time" in data_type:
            return "'2023-01-01'" if is_lower else "'2023-01-31'"  # Just one month
        elif data_type == "boolean":
            return "true"  # Same value for both bounds for boolean
        else:
            # Default string-like behavior - narrow range
            return "'m'" if is_lower else "'n'"  # Just two adjacent letters

    def _get_table_aliases(self, query: str, table_name: str) -> list[str]:
        """Extract table aliases for a given table using SQL parser.

        Args:
            query: The SQL query to parse
            table_name: The name of the table to find aliases for

        Returns:
            List of aliases (including the table name itself)
        """
        try:
            # Parse the query
            parsed = parse_sql(query)
            if not parsed:
                return [table_name]  # Return just the table name if parsing fails

            # Get the statement tree
            stmt = parsed[0].stmt

            # Use TableAliasVisitor to extract aliases
            alias_visitor = TableAliasVisitor()
            alias_visitor(stmt)

            # Find all aliases for this table
            aliases = [table_name]  # Always include the table name itself

            for alias, table in alias_visitor.aliases.items():
                if table.lower() == table_name.lower():
                    aliases.append(alias)

            self.dta_trace(f"Found aliases for {table_name}: {aliases}")
            return aliases
        except Exception as e:
            logger.error(f"Error extracting table aliases: {e}", exc_info=True)
            return [table_name]  # Fallback to just the table name

    def _get_table_columns(self, table: str) -> set[str]:
        """Get column names for a table from the catalog."""
        try:
            query = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = {}
            """
            result = SafeSqlDriver.execute_param_query(
                self.sql_driver,
                query,
                [table],
            )
            return (
                set([row.cells["column_name"] for row in result]) if result else set()
            )
        except Exception as e:
            logger.error(f"Error getting columns for table {table}: {e}", exc_info=True)
            return set()

    def _identify_parameter_column(
        self, context: str, table_columns: dict[str, set[str]]
    ) -> tuple[str, str] | None:
        """Identify which column a parameter likely belongs to based on context."""
        # Look for patterns like "column_name = $1" or "column_name IN ($1)"
        for table, columns in table_columns.items():
            for column in columns:
                # Various patterns to match column references
                patterns = [
                    rf"{column}\s*=\s*\$\d+",  # column = $1
                    rf"{column}\s+in\s+\([^)]*\$\d+[^)]*\)",  # column in (...$1...)
                    rf"{column}\s+like\s+\$\d+",  # column like $1
                    rf"{column}\s*>\s*\$\d+",  # column > $1
                    rf"{column}\s*<\s*\$\d+",  # column < $1
                    rf"{column}\s*>=\s*\$\d+",  # column >= $1
                    rf"{column}\s*<=\s*\$\d+",  # column <= $1
                    rf"{column}\s+between\s+\$\d+\s+and\s+\$\d+",  # column between $1 and $2
                ]

                for pattern in patterns:
                    if re.search(pattern, context, re.IGNORECASE):
                        return (table, column)

        return None

    def _get_column_statistics(
        self, table_name: str, column_name: str
    ) -> dict[str, Any] | None:
        """Get statistics for a column from pg_stats."""
        # Initialize cache if it doesn't exist
        if not hasattr(self, "_column_stats_cache"):
            self._column_stats_cache = {}

        # Create a cache key from table and column name
        cache_key = f"{table_name}.{column_name}"

        # Check if we already have this in cache
        if cache_key in self._column_stats_cache:
            return self._column_stats_cache[cache_key]

        # Not in cache, query the database
        try:
            query = """
            SELECT
                data_type,
                most_common_vals as common_vals,
                most_common_freqs as common_freqs,
                histogram_bounds,
                null_frac,
                n_distinct,
                correlation
            FROM pg_stats
            JOIN information_schema.columns
                ON pg_stats.tablename = information_schema.columns.table_name
                AND pg_stats.attname = information_schema.columns.column_name
            WHERE pg_stats.tablename = {}
            AND pg_stats.attname = {}
            """

            self.dta_trace(f"Getting statistics for {table_name}.{column_name}")

            result = SafeSqlDriver.execute_param_query(
                self.sql_driver,
                query,
                [table_name, column_name],
            )
            if not result or not result[0]:
                self.dta_trace(f"No statistics found for {table_name}.{column_name}")
                self._column_stats_cache[cache_key] = None
                return None

            stats = dict(result[0].cells)

            # Convert PostgreSQL arrays to Python lists for easier handling
            for key in ["common_vals", "common_freqs", "histogram_bounds"]:
                if key in stats and stats[key] is not None:
                    if isinstance(stats[key], str):
                        # Parse array literals like '{val1,val2}' into Python lists
                        array_str = stats[key].strip("{}")
                        if array_str:
                            stats[key] = [
                                self._parse_pg_array_value(val)
                                for val in array_str.split(",")
                            ]
                        else:
                            stats[key] = []

            # Cache the processed results
            self._column_stats_cache[cache_key] = stats
            return stats
        except Exception as e:
            logger.warning(
                f"Error getting column statistics for {table_name}.{column_name}: {e}"
            )
            self._column_stats_cache[cache_key] = None
            return None

    def _parse_pg_array_value(self, value: str) -> Any:
        """Parse a single value from a PostgreSQL array representation."""
        value = value.strip()

        # Try to convert to appropriate type
        if value == "null":
            return None
        elif value.startswith('"') and value.endswith('"'):
            return value[1:-1]  # Strip quotes for string values

        # Try numeric conversion
        try:
            if "." in value:
                return float(value)
            else:
                return int(value)
        except ValueError:
            # Return as string if not a number
            return value

    def _get_replacement_value(self, stats: dict[str, Any], context: str) -> str:
        """Generate an appropriate replacement value based on column statistics."""
        data_type = stats.get("data_type", "").lower()
        common_vals = stats.get("common_vals")
        histogram_bounds = stats.get("histogram_bounds")

        # If we have common values, use the most common one for equality,
        # or a value in the middle of the range for range queries

        # Detect query operator context
        is_equality = "=" in context and "!=" not in context and "<>" not in context
        is_range = any(op in context for op in [">", "<", ">=", "<=", "between"])
        is_like = "like" in context

        # For string types
        if "char" in data_type or data_type == "text":
            if is_like:
                return "'%test%'"
            elif common_vals and is_equality:
                # Use the most common value for equality
                sample = common_vals[0]
                return f"'{sample}'"
            elif common_vals:
                # Use any sample value
                sample = common_vals[0]
                return f"'{sample}'"
            else:
                # Default string
                return "'sample_value'"

        # For numeric types
        elif "int" in data_type or data_type in [
            "numeric",
            "decimal",
            "real",
            "double",
        ]:
            if histogram_bounds and is_range:
                # For range queries, use a value in the middle
                bounds = histogram_bounds
                if isinstance(bounds, list) and len(bounds) > 1:
                    middle_idx = len(bounds) // 2
                    value = bounds[middle_idx]
                    return str(value)
            elif common_vals and is_equality:
                # Use most common value for equality
                return str(common_vals[0])
            elif histogram_bounds:
                # Use a reasonable value from histogram
                bounds = histogram_bounds
                if isinstance(bounds, list) and len(bounds) > 0:
                    return str(bounds[0])

            # Default numeric values by type
            if "int" in data_type:
                return "41"
            else:
                return "41.5"

        # For date/time types
        elif "date" in data_type or "time" in data_type:
            if is_range:
                return "'2023-01-15'"  # Middle of the month
            return "'2023-01-01'"

        # For boolean
        elif data_type == "boolean":
            return "true"

        # Default fallback
        return "'sample_value'"

    def _get_generic_replacement(self, context: str) -> str:
        """Provide a generic replacement when we can't determine the specific column type."""
        context = context.lower()

        # Try to guess based on context
        if any(
            date_word in context.split() for date_word in ["date", "timestamp", "time"]
        ):
            return "'2023-01-01'"

        if any(word in context for word in ["id", "key", "code", "num"]):
            return "43"

        if "like" in context:
            return "'%sample%'"

        if any(word in context for word in ["amount", "price", "cost", "fee"]):
            return "99.99"

        # If in comparison context, use a number
        if any(op in context for op in ["=", ">", "<", ">=", "<="]):
            return "44"

        # Default to string
        return "'sample_value'"

    def _replace_parameters_generic(self, query: str) -> str:
        """Fallback generic parameter replacement when catalog lookup fails."""
        try:
            modified_query = query

            # Replace string parameters
            modified_query = re.sub(r"like \$\d+", "like '%'", modified_query)

            # Context-aware replacements
            modified_query = re.sub(
                r"(\w+)\s*=\s*\$\d+",
                lambda m: self._context_replace(m, "="),
                modified_query,
            )
            modified_query = re.sub(
                r"(\w+)\s*<\s*\$\d+",
                lambda m: self._context_replace(m, "<"),
                modified_query,
            )
            modified_query = re.sub(
                r"(\w+)\s*>\s*\$\d+",
                lambda m: self._context_replace(m, ">"),
                modified_query,
            )

            # Replace numeric parameters in inequalities
            modified_query = re.sub(r"(\d+) and \$\d+", r"\1 and 100", modified_query)
            modified_query = re.sub(r"\$\d+ and (\d+)", r"1 and \1", modified_query)
            modified_query = re.sub(r">\s*\$\d+", "> 1", modified_query)
            modified_query = re.sub(r"<\s*\$\d+", "< 100", modified_query)
            modified_query = re.sub(r"=\s*\$\d+\b", "= 45", modified_query)

            # For any remaining parameters, use a generic replacement
            modified_query = re.sub(r"\$\d+", "'sample_value'", modified_query)

            return modified_query
        except Exception as e:
            logger.error(f"Error in generic parameter replacement: {e}", exc_info=True)
            return query

    def _context_replace(self, match, op: str):
        """Replace parameters based on column name context."""
        col_name = match.group(1).lower()

        # ID-like columns (numeric)
        if col_name.endswith("id") or col_name.endswith("_id") or col_name == "id":
            return f"{col_name} {op} 46"

        # Date/time columns
        if any(word in col_name for word in ["date", "time", "created", "updated"]):
            return f"{col_name} {op} '2023-01-01'"

        # Numeric-looking columns
        if any(
            word in col_name
            for word in ["amount", "price", "cost", "count", "num", "qty"]
        ):
            return f"{col_name} {op} 46.5"

        # Status-like columns (likely string enums)
        if "status" in col_name or "type" in col_name or "state" in col_name:
            return f"{col_name} {op} 'active'"

        # Default to string for other columns
        return f"{col_name} {op} 'sample_value'"

    # --- Debugging ---

    @staticmethod
    def _pp_list(lst) -> str:
        """Pretty print a list."""
        return ("\n  - " if len(lst) > 0 else "") + "\n  - ".join(
            [str(item) for item in lst]
        )

    def get_explain_plan_with_indexes(
        self, query_text: str, indexes: frozenset[IndexConfig]
    ) -> dict[str, Any]:
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
        plan = self.generate_explain_plan_with_hypothetical_indexes(
            self.sql_driver, query_text, indexes, self
        )

        # Cache the result
        self._explain_plans_cache[cache_key] = plan
        return plan

    @staticmethod
    def generate_explain_plan_with_hypothetical_indexes(
        sql_driver: SqlDriver,
        query_text: str,
        indexes: frozenset[IndexConfig],
        dta=None,
    ) -> dict[str, Any]:
        """
        Generate an explain plan for a query with specified indexes.

        Args:
            sql_driver: SQL driver to execute the query
            query_text: The SQL query to explain
            indexes: A frozenset of IndexConfig objects representing the indexes to enable

        Returns:
            The explain plan as a dictionary
        """
        try:
            # Create the indexes query
            create_indexes_query = "SELECT hypopg_reset();"
            if len(indexes) > 0:
                create_indexes_query += SafeSqlDriver.param_sql_to_query(
                    "SELECT hypopg_create_index({});" * len(indexes),
                    [idx.definition for idx in indexes],
                )

            # Execute explain with the indexes
            explain_plan_query = f"{create_indexes_query} EXPLAIN ({'COSTS TRUE, ' if indexes else ''}FORMAT JSON) {query_text}"
            plan_result = sql_driver.execute_query(explain_plan_query)

            # Extract the plan
            if plan_result and plan_result[0].cells.get("QUERY PLAN"):
                plan_data = plan_result[0].cells.get("QUERY PLAN")
                if isinstance(plan_data, list) and len(plan_data) > 0:
                    return plan_data[0]
                else:
                    dta.dta_trace(  # type: ignore
                        f"      - plan_data is an empty list with plan_data type: {type(plan_data)}"
                    )  # type: ignore

            dta.dta_trace("      - returning empty plan")  # type: ignore
            # Return empty plan if no result
            return {"Plan": {"Total Cost": float("inf")}}

        except Exception as e:
            logger.error(
                f"Error getting explain plan for query: {query_text} with error: {e}",
                exc_info=True,
            )
            return {"Plan": {"Total Cost": float("inf")}}

    def dta_trace(self, message: Any, exc_info: bool = False):
        """Convenience function to log DTA thinking process."""

        # Always log to debug
        if exc_info:
            logger.debug(message, exc_info=True)
        else:
            logger.debug(message)

        self._dta_traces.append(message)


# --- Visitor Classes ---


class TableAliasVisitor(Visitor):
    """Extracts table aliases and names from the SQL AST."""

    def __init__(self) -> None:
        super().__init__()  # Initialize the base Visitor class
        self.aliases: dict[str, str] = {}
        self.tables: set[str] = set()

    def __call__(self, node):
        super().__call__(node)
        return self.aliases, self.tables

    def visit_RangeVar(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit table references, including those in FROM clause."""
        if isinstance(node, RangeVar):  # Type narrowing for RangeVar
            if node.relname is not None:
                self.tables.add(node.relname)
            if node.alias and node.alias.aliasname is not None:
                self.aliases[node.alias.aliasname] = str(node.relname)

    def visit_JoinExpr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit both sides of JOIN expressions."""
        if isinstance(node, JoinExpr):  # Type narrowing for JoinExpr
            if node.larg is not None:
                self(node.larg)  # type: ignore
            if node.rarg is not None:
                self(node.rarg)  # type: ignore


class ColumnCollector(Visitor):
    """
    Collects columns used in WHERE, JOIN, ORDER BY, GROUP BY, HAVING, and SELECT clauses.
    With improved handling of column aliases.
    """

    def __init__(self) -> None:
        super().__init__()
        self.context_stack = []  # Stack of (tables, aliases) for each scope
        self.columns = {}  # Collected columns, keyed by table
        self.target_list = None
        self.inside_select = False
        self.column_aliases = {}  # Track column aliases and their definitions
        self.current_query_level = 0  # Track nesting level for subqueries

    def __call__(self, node):
        super().__call__(node)
        return self.columns

    def visit_SelectStmt(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit a SelectStmt node and process its targetList for column aliases."""
        if isinstance(node, SelectStmt):
            self.inside_select = True
            self.current_query_level += 1
            query_level = self.current_query_level

            # Collect tables and aliases
            alias_visitor = TableAliasVisitor()
            if hasattr(node, "fromClause") and node.fromClause:
                for from_item in node.fromClause:
                    alias_visitor(from_item)
            scope_tables = alias_visitor.tables
            scope_aliases = alias_visitor.aliases

            # Push new context for this scope
            self.context_stack.append((scope_tables, scope_aliases))

            # First pass: collect column aliases from targetList
            if hasattr(node, "targetList") and node.targetList:
                self.target_list = node.targetList
                for target_entry in self.target_list:
                    if hasattr(target_entry, "name") and target_entry.name:
                        # This is a column alias
                        col_alias = target_entry.name
                        # Store the expression node for this alias
                        if hasattr(target_entry, "val"):
                            self.column_aliases[col_alias] = {
                                "node": target_entry.val,
                                "level": query_level,
                            }

            # Second pass: process the rest of the query
            self._process_query_clauses(node)

            # Pop context after processing
            self.context_stack.pop()
            self.inside_select = False
            self.current_query_level -= 1

    def _process_query_clauses(self, node):
        """Process various query clauses for column collection."""
        # Process targetList expressions
        if hasattr(node, "targetList") and node.targetList:
            self.target_list = node.targetList
            for target_entry in self.target_list:
                if hasattr(target_entry, "val"):
                    self(target_entry.val)

        # Handle GROUP BY clause
        if hasattr(node, "groupClause") and node.groupClause:
            for group_item in node.groupClause:
                if isinstance(group_item, SortGroupClause) and isinstance(
                    group_item.tleSortGroupRef, int
                ):
                    ref_index = group_item.tleSortGroupRef
                    if self.target_list and ref_index <= len(self.target_list):
                        target_entry = self.target_list[ref_index - 1]  # 1-based index
                        if hasattr(target_entry, "val"):
                            self(target_entry.val)
                        if hasattr(target_entry, "expr"):
                            self(target_entry.expr)  # Visit the expression

        # Process WHERE clause (including subqueries)
        if hasattr(node, "whereClause") and node.whereClause:
            self(node.whereClause)

        # Process FROM clause (may contain subqueries)
        if hasattr(node, "fromClause") and node.fromClause:
            for from_item in node.fromClause:
                self(from_item)

        # Process HAVING clause
        if hasattr(node, "havingClause") and node.havingClause:
            self(node.havingClause)

        # Handle ORDER BY clause
        if hasattr(node, "sortClause") and node.sortClause:
            for sort_item in node.sortClause:
                self._process_sort_item(sort_item)

    def _process_sort_item(self, sort_item):
        """Process a sort item, resolving column aliases if needed."""
        if not hasattr(sort_item, "node"):
            return

        # If it's a simple column reference, it might be an alias
        if (
            isinstance(sort_item.node, ColumnRef)
            and hasattr(sort_item.node, "fields")
            and sort_item.node.fields
        ):
            fields = [f.sval for f in sort_item.node.fields if hasattr(f, "sval")]
            if len(fields) == 1:
                col_name = fields[0]
                # Check if this is a known alias
                if col_name in self.column_aliases:
                    # Process the original expression instead
                    alias_info = self.column_aliases[col_name]
                    if alias_info["level"] == self.current_query_level:
                        self(alias_info["node"])
                        return

        # Regular processing for non-alias sort items
        self(sort_item.node)

    def visit_ColumnRef(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit a ColumnRef node and collect column names, skipping aliases."""
        if isinstance(node, ColumnRef) and self.inside_select:
            if not hasattr(node, "fields") or not node.fields:
                return

            fields = [f.sval if hasattr(f, "sval") else "*" for f in node.fields]

            # Skip collecting if this is a reference to a column alias
            if len(fields) == 1 and (
                fields[0] == "*" or fields[0] in self.column_aliases
            ):
                return

            if len(fields) == 2 and fields[1] == "*":
                return

            # Use current scope's tables and aliases
            current_tables, current_aliases = (
                self.context_stack[-1] if self.context_stack else ({}, {})
            )

            if len(fields) == 2:  # Qualified column (e.g., u.name)
                table_or_alias, column = fields
                table = current_aliases.get(table_or_alias, table_or_alias)
                if table not in self.columns:
                    self.columns[table] = set()
                self.columns[table].add(column)
            elif len(fields) == 1:  # Unqualified column
                column = fields[0]
                if len(current_tables) == 1:  # Only one table in scope
                    table = next(iter(current_tables))
                    if table not in self.columns:
                        self.columns[table] = set()
                    self.columns[table].add(column)
                else:
                    # Try to find which table this column belongs to
                    for table in current_tables:
                        if self._column_exists(table, column):
                            if table not in self.columns:
                                self.columns[table] = set()
                            self.columns[table].add(column)
                            break

    def _column_exists(self, table: str, column: str) -> bool:
        """Check if column exists in table."""
        # This is a placeholder. We'd query the schema
        # Ideally this would be cached to avoid repeated queries
        return True  # Default to True for the sake of collecting all possibilities

    def visit_A_Expr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit an A_Expr node (arithmetic or comparison expression).
        """
        if isinstance(node, A_Expr) and self.inside_select:
            if hasattr(node, "lexpr") and node.lexpr:
                self(node.lexpr)
                if isinstance(node.lexpr, SelectStmt):
                    alias_visitor = TableAliasVisitor()
                    alias_visitor(node.lexpr)
                    self.context_stack.append(
                        (alias_visitor.tables, alias_visitor.aliases)
                    )
                    self(node.lexpr)
                    self.context_stack.pop()
            if hasattr(node, "rexpr") and node.rexpr:
                if isinstance(node.rexpr, SelectStmt):
                    alias_visitor = TableAliasVisitor()
                    alias_visitor(node.rexpr)
                    self.context_stack.append(
                        (alias_visitor.tables, alias_visitor.aliases)
                    )
                    self(node.rexpr)
                    self.context_stack.pop()
                else:
                    self(node.rexpr)

    def visit_JoinExpr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit a JoinExpr node to handle JOIN conditions.
        """
        if (
            isinstance(node, JoinExpr) and self.inside_select
        ):  # Type narrowing for JoinExpr
            if hasattr(node, "larg") and node.larg:
                self(node.larg)
            if hasattr(node, "rarg") and node.rarg:
                self(node.rarg)
            if hasattr(node, "quals") and node.quals:
                self(node.quals)

    def visit_SortBy(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit a SortBy node (ORDER BY expression).
        """
        if isinstance(node, SortBy) and self.inside_select:  # Type narrowing for SortBy
            if hasattr(node, "node") and node.node:
                self(node.node)


class ConditionColumnCollector(ColumnCollector):
    """
    A specialized version of ColumnCollector that only collects columns used in
    WHERE, JOIN, HAVING conditions, and properly resolves column aliases.
    """

    def __init__(self) -> None:
        super().__init__()
        self.condition_columns = {}  # Specifically for columns in conditions
        self.in_condition = False  # Flag to track if we're inside a condition

    def __call__(self, node):
        super().__call__(node)
        return self.condition_columns

    def visit_SelectStmt(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit a SelectStmt node but focus on condition-related clauses,
        while still collecting column aliases.
        """
        if isinstance(node, SelectStmt):
            self.inside_select = True
            self.current_query_level += 1
            query_level = self.current_query_level

            # Get table aliases first
            alias_visitor = TableAliasVisitor()
            if hasattr(node, "fromClause") and node.fromClause:
                for from_item in node.fromClause:
                    alias_visitor(from_item)
            tables = alias_visitor.tables
            aliases = alias_visitor.aliases

            # Store the context for this query
            self.context_stack.append((tables, aliases))

            # First pass: collect column aliases from targetList
            if hasattr(node, "targetList") and node.targetList:
                self.target_list = node.targetList
                for target_entry in self.target_list:
                    if hasattr(target_entry, "name") and target_entry.name:
                        # This is a column alias
                        col_alias = target_entry.name
                        # Store the expression node for this alias
                        if hasattr(target_entry, "val"):
                            self.column_aliases[col_alias] = {
                                "node": target_entry.val,
                                "level": query_level,
                            }

            # Process WHERE clause
            if node.whereClause:
                in_condition_cache = self.in_condition
                self.in_condition = True
                self(node.whereClause)
                self.in_condition = in_condition_cache

            # Process JOIN conditions in fromClause
            if node.fromClause:
                for item in node.fromClause:
                    if isinstance(item, JoinExpr) and item.quals:
                        in_condition_cache = self.in_condition
                        self.in_condition = True
                        self(item.quals)
                        self.in_condition = in_condition_cache

            # Process HAVING clause - may reference aliases
            if node.havingClause:
                in_condition_cache = self.in_condition
                self.in_condition = True
                self._process_having_with_aliases(node.havingClause)
                self.in_condition = in_condition_cache

            # Process ORDER BY clause - also important for indexes
            if hasattr(node, "sortClause") and node.sortClause:
                in_condition_cache = self.in_condition
                self.in_condition = True
                for sort_item in node.sortClause:
                    self._process_node_with_aliases(sort_item.node)
                self.in_condition = in_condition_cache

            # # Process GROUP BY clause - can also benefit from indexes
            # if hasattr(node, "groupClause") and node.groupClause:
            #     in_condition_cache = self.in_condition
            #     self.in_condition = True
            #     for group_item in node.groupClause:
            #         self._process_node_with_aliases(group_item)
            #     self.in_condition = in_condition_cache

            # Clean up the context stack
            self.context_stack.pop()
            self.inside_select = False
            self.current_query_level -= 1

    def _process_having_with_aliases(self, having_clause):
        """Process HAVING clause with special handling for column aliases."""
        self._process_node_with_aliases(having_clause)

    def _process_node_with_aliases(self, node):
        """Process a node, resolving any column aliases it contains."""
        if node is None:
            return

        # If node is a column reference, it might be an alias
        if isinstance(node, ColumnRef) and hasattr(node, "fields") and node.fields:
            fields = (
                [f.sval for f in node.fields if hasattr(f, "sval")]
                if node.fields
                else []
            )
            if len(fields) == 1:
                col_name = fields[0]
                # Check if this is a known alias
                if col_name in self.column_aliases:
                    # Process the original expression instead
                    alias_info = self.column_aliases[col_name]
                    if alias_info["level"] == self.current_query_level:
                        self(alias_info["node"])
                        return

        # For non-alias nodes, process normally
        self(node)

    def visit_ColumnRef(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Process column references, but only if we're in a condition context.
        Skip known column aliases but process their underlying expressions.
        """
        if not self.in_condition:
            return  # Skip if not in a condition context

        if not isinstance(node, ColumnRef) or not self.context_stack:
            return

        # Get the current query context
        tables, aliases = self.context_stack[-1]

        # Extract table and column names
        fields = (
            [f.sval for f in node.fields if hasattr(f, "sval")] if node.fields else []
        )

        # Check if this is a reference to a column alias
        if len(fields) == 1 and fields[0] in self.column_aliases:
            # Process the original expression node instead
            alias_info = self.column_aliases[fields[0]]
            if alias_info["level"] == self.current_query_level:
                self.in_condition = (
                    True  # Ensure we collect from the aliased expression
                )
                self(alias_info["node"])
            return

        if len(fields) == 2:  # Table.column format
            table_or_alias, column = fields
            # Resolve alias to actual table
            table = aliases.get(table_or_alias, table_or_alias)

            # Add to condition columns
            if table not in self.condition_columns:
                self.condition_columns[table] = set()
            self.condition_columns[table].add(column)

        elif len(fields) == 1:  # Unqualified column
            column = fields[0]

            # For unqualified columns, check all tables in context
            found_match = False
            for table in tables:
                # Skip schema qualification if present
                if "." in table:
                    _, table = table.split(".", 1)

                # Add column to all tables that have it
                if self._column_exists(table, column):
                    if table not in self.condition_columns:
                        self.condition_columns[table] = set()
                    self.condition_columns[table].add(column)
                    found_match = True

            if not found_match:
                logger.debug(
                    f"Could not resolve unqualified column '{column}' to any table"
                )

    def _column_exists(self, table: str, column: str) -> bool:
        """Check if column exists in table."""
        # This would normally query the database
        # For now, we'll return True to collect all possible matches
        # The actual filtering will happen later
        return True
