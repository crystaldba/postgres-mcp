import logging
import math
from dataclasses import dataclass
from typing import Any
from typing import override

import instructor
from openai import OpenAI
from pglast.ast import SelectStmt
from pydantic import BaseModel

from postgres_mcp.artifacts import ErrorResult
from postgres_mcp.explain.explain_plan import ExplainPlanTool
from postgres_mcp.sql import TableAliasVisitor

from ..sql import IndexConfig
from ..sql import SqlDriver
from .index_opt_base import IndexTuningBase

logger = logging.getLogger(__name__)


class Index(BaseModel):
    table_name: str
    columns: tuple[str, ...]

    def __hash__(self):
        return hash((self.table_name, self.columns))

    def __eq__(self, other):
        if not isinstance(other, Index):
            return False
        return self.table_name == other.table_name and self.columns == other.columns

    def to_index_config(self) -> IndexConfig:
        return IndexConfig(table=self.table_name, columns=self.columns)


class IndexingAlternative(BaseModel):
    alternatives: list[set[Index]]

    # def to_list(self) -> list[set[IndexConfig]]:
    #     return [set(IndexConfig(table=index.table_name, columns=index.columns) for index in indexes) for indexes in self.alternatives]


@dataclass
class ScoredIndexes:
    indexes: set[Index]
    execution_cost: float
    index_size: float
    objective_score: float


class LLMOptimizerTool(IndexTuningBase):
    def __init__(
        self,
        sql_driver: SqlDriver,
        max_no_progress_attempts: int = 5,
        pareto_alpha: float = 2.0,
    ):
        super().__init__(sql_driver)
        self.sql_driver = sql_driver
        self.max_no_progress_attempts = max_no_progress_attempts
        self.pareto_alpha = pareto_alpha
        logger.info("Initialized LLMOptimizerTool with max_no_progress_attempts=%d", max_no_progress_attempts)

    def score(self, execution_cost: float, index_size: float) -> float:
        return math.log(execution_cost) + self.pareto_alpha * math.log(index_size)

    @override
    async def _generate_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], existing_index_defs: set[str]
    ) -> tuple[set[IndexConfig], float]:
        """Generate index tuning queries using optimization by LLM."""
        # For now we support only one table at a time
        if len(query_weights) > 1:
            logger.error("LLM optimization currently supports only one query at a time")
            raise ValueError("Optimization by LLM supports only one query at a time.")

        query = query_weights[0][0]
        parsed_query = query_weights[0][1]
        logger.info("Generating index recommendations for query: %s", query)

        # Extract tables from the parsed query
        table_visitor = TableAliasVisitor()
        table_visitor(parsed_query)
        tables = table_visitor.tables
        logger.info("Extracted tables from query: %s", tables)

        # Get the size of the tables
        table_sizes = {}
        for table in tables:
            table_sizes[table] = await self._get_table_size(table)
        total_table_size = sum(table_sizes.values())
        logger.info("Total table size: %s", total_table_size)

        # Generate explain plan for the query
        explain_tool = ExplainPlanTool(self.sql_driver)
        explain_result = await explain_tool.explain(query)
        if isinstance(explain_result, ErrorResult):
            logger.error("Failed to generate explain plan: %s", explain_result.to_text())
            raise ValueError(f"Failed to generate explain plan: {explain_result.to_text()}")

        # Get the explain plan JSON
        explain_plan_json = explain_result.value
        logger.debug("Generated explain plan: %s", explain_plan_json)

        # Extract indexes used in the explain plan
        indexes_used = await self._extract_indexes_from_explain_plan_with_columns(explain_plan_json)

        # Get the current cost
        original_cost = await self._evaluate_configuration_cost(query_weights, frozenset())
        logger.info("Original query cost: %f", original_cost)

        original_config = ScoredIndexes(
            indexes=indexes_used,
            execution_cost=original_cost,
            index_size=total_table_size,
            objective_score=self.score(original_cost, total_table_size),
        )

        best_config = original_config

        # Initialize attempt history for this run
        attempt_history: list[ScoredIndexes] = [original_config]

        no_progress_count = 0
        client = instructor.from_openai(OpenAI())

        # Starting cost
        # TODO should include the size of the starting indexes
        score = self.score(original_cost, total_table_size)
        logger.info("Starting score: %f", score)

        while no_progress_count < self.max_no_progress_attempts:
            logger.info("Requesting index recommendations from LLM")

            # Build history of past attempts
            history_prompt = ""
            if attempt_history:
                history_prompt = "\nPrevious attempts and their costs:\n"
                for attempt in attempt_history:
                    indexes_str = ", ".join(f"{idx.table_name}.{','.join(idx.columns)}" for idx in attempt.indexes)
                    history_prompt += f"- Indexes: {indexes_str}, Cost: {attempt.execution_cost}, Index Size: {attempt.index_size}, "
                    history_prompt += f"Objective Score: {attempt.objective_score}\n"

            if no_progress_count > 0:
                remaining_attempts_prompt = f"You have made {no_progress_count} attempts without progress. "
                if self.max_no_progress_attempts - no_progress_count < self.max_no_progress_attempts / 2:
                    remaining_attempts_prompt += "Get creative and suggest indexes that are not obvious."
            else:
                remaining_attempts_prompt = ""

            response = client.chat.completions.create(
                model="gpt-4o",
                response_model=IndexingAlternative,
                temperature=1.2,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates index recommendations for a given workload."},
                    {
                        "role": "user",
                        "content": f"Here is the query we are optimizing: {query}\n"
                        f"Here is the explain plan: {explain_plan_json}\n"
                        f"Here are the existing indexes: {existing_index_defs}\n"
                        f"{history_prompt}\n"
                        "Each indexing suggestion that you provide is a combination of indexes. You can provide multiple alternative suggestions. "
                        "We will evaluate each alternative using hypopg to see how the optimizer will be behave with those indexes in place. "
                        "The overall score is based on a combination of execution cost and index size. In all cases, lower is better. "
                        f"{remaining_attempts_prompt}",
                    },
                ],
            )

            # Convert the response to IndexConfig objects
            index_alternatives: list[set[Index]] = response.alternatives
            logger.info("Received %d alternative index configurations from LLM", len(index_alternatives))

            # If no alternatives were generated, break the loop
            if not index_alternatives:
                logger.warning("No index alternatives were generated by the LLM")
                break

            # Try each alternative
            found_improvement = False
            for i, index_set in enumerate(index_alternatives):
                try:
                    logger.info("Evaluating alternative %d/%d with %d indexes", i + 1, len(index_alternatives), len(index_set))
                    # Evaluate this index configuration
                    execution_cost_estimate = await self._evaluate_configuration_cost(
                        query_weights, frozenset({index.to_index_config() for index in index_set})
                    )
                    logger.info(
                        "Alternative %d cost: %f (improvement: %.2f%%)",
                        i + 1,
                        execution_cost_estimate,
                        ((original_cost - execution_cost_estimate) / original_cost) * 100,
                    )

                    # Estimate the size of the indexes
                    index_size_estimate = await self._estimate_index_size_2({index.to_index_config() for index in index_set})
                    logger.info("Estimated index size: %f", index_size_estimate)

                    # Score based on a balance of size and performance
                    score = math.log(execution_cost_estimate) + self.pareto_alpha * math.log(total_table_size + index_size_estimate)

                    # Record this attempt in history
                    latest_config = ScoredIndexes(
                        indexes={Index(table_name=index.table_name, columns=index.columns) for index in index_set},
                        execution_cost=execution_cost_estimate,
                        index_size=index_size_estimate,
                        objective_score=score,
                    )
                    attempt_history.append(latest_config)
                    logger.info("Latest config: %s", latest_config)

                    # If this is better than what we've seen so far, update our best
                    # Minimum 2% improvement required
                    if latest_config.objective_score < best_config.objective_score:
                        best_config = latest_config
                        found_improvement = True
                except Exception as e:
                    # We discard the alternative. We are seeing this happen due to invalid index definitions.
                    logger.error("Error evaluating alternative %d/%d: %s", i + 1, len(index_alternatives), str(e))

            # Keep only the 5 best results in the attempt history
            attempt_history.sort(key=lambda x: x.objective_score)
            attempt_history = attempt_history[:5]

            if found_improvement:
                no_progress_count = 0
            else:
                no_progress_count += 1
                logger.info(
                    "No improvement found in this iteration. Attempts without progress: %d/%d", no_progress_count, self.max_no_progress_attempts
                )

        if best_config != original_config:
            logger.info(
                "Selected best index configuration with %d indexes, cost improvement: %.2f%%",
                len(best_config.indexes),
                ((original_cost - best_config.execution_cost) / original_cost) * 100,
            )
        else:
            logger.info("No better index configuration found")

        # Convert Index objects to IndexConfig objects for return
        best_index_config_set = {index.to_index_config() for index in best_config.indexes}
        return (best_index_config_set, best_config.execution_cost)

    async def _estimate_index_size_2(self, index_set: set[IndexConfig]) -> float:
        """
        Estimate the size of a set of indexes using hypopg.

        Args:
            index_set: Set of IndexConfig objects representing the indexes to estimate

        Returns:
            Total estimated size of all indexes in bytes
        """
        if not index_set:
            return 0.0

        total_size = 0.0

        for index_config in index_set:
            try:
                # Create a hypothetical index using hypopg
                # Using a tuple to avoid LiteralString type error
                create_index_query = (
                    "WITH hypo_index AS (SELECT indexrelid FROM hypopg_create_index(%s)) "
                    "SELECT hypopg_relation_size(indexrelid) as size, hypopg_drop_index(indexrelid) FROM hypo_index;"
                )

                # Execute the query to get the index size
                result = await self.sql_driver.execute_query(create_index_query, params=[index_config.definition])

                if result and len(result) > 0:
                    # Extract the size from the result
                    size = result[0].cells.get("size", 0)
                    total_size += float(size)
                    logger.debug(f"Estimated size for index {index_config.name}: {size} bytes")
                else:
                    logger.warning(f"Failed to estimate size for index {index_config.name}")

            except Exception as e:
                logger.error(f"Error estimating size for index {index_config.name}: {e!s}")

        return total_size

    def _extract_indexes_from_explain_plan(self, explain_plan_json: Any) -> set[tuple[str, str]]:
        """
        Extract indexes used in the explain plan JSON.

        Args:
            explain_plan_json: The explain plan JSON from PostgreSQL

        Returns:
            A set of tuples (table_name, index_name) representing the indexes used in the plan
        """
        indexes_used = set()
        if isinstance(explain_plan_json, dict):
            plan_data = explain_plan_json.get("Plan")
            if plan_data is not None:

                def extract_indexes_from_node(node):
                    # Check if this is an index scan node
                    if node.get("Node Type") in ["Index Scan", "Index Only Scan", "Bitmap Index Scan"]:
                        if "Index Name" in node and "Relation Name" in node:
                            # Add the table name and index name
                            indexes_used.add((node["Relation Name"], node["Index Name"]))

                    # Recursively process child plans
                    if "Plans" in node:
                        for child in node["Plans"]:
                            extract_indexes_from_node(child)

                # Start extraction from the root plan
                extract_indexes_from_node(plan_data)
                logger.info("Extracted %d indexes from explain plan", len(indexes_used))

        return indexes_used

    async def _extract_indexes_from_explain_plan_with_columns(self, explain_plan_json: Any) -> set[Index]:
        """
        Extract indexes used in the explain plan JSON and populate their columns.

        Args:
            explain_plan_json: The explain plan JSON from PostgreSQL

        Returns:
            A set of Index objects representing the indexes used in the plan with their columns
        """
        # First extract the indexes without columns
        index_tuples = self._extract_indexes_from_explain_plan(explain_plan_json)

        # Now populate the columns for each index
        indexes_with_columns = set()
        for table_name, index_name in index_tuples:
            # Get the columns for this index
            columns = await self._get_index_columns(index_name)

            # Create a new Index object with the columns
            index_with_columns = Index(table_name=table_name, columns=columns)
            indexes_with_columns.add(index_with_columns)

        return indexes_with_columns

    async def _get_index_columns(self, index_name: str) -> tuple[str, ...]:
        """
        Get the columns for a specific index by querying the database.

        Args:
            index_name: The name of the index

        Returns:
            A tuple of column names in the index
        """
        try:
            # Query to get index columns
            query = """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE c.relname = %s
            ORDER BY array_position(i.indkey, a.attnum)
            """

            result = await self.sql_driver.execute_query(query, [index_name])

            if result and len(result) > 0:
                # Extract column names from the result
                columns = [row.cells.get("attname", "") for row in result if row.cells.get("attname")]
                return tuple(columns)
            else:
                logger.warning(f"No columns found for index {index_name}")
                return tuple()

        except Exception as e:
            logger.error(f"Error getting columns for index {index_name}: {e!s}")
            return tuple()

    # def _get_tables(self, workload_queries: list[SelectStmt]) -> set[tuple[str, str]]:
    #     """Extract table information from the workload queries."""
    #     tables: set[tuple[str, str]] = set()

    #     def walk_node(node: Node) -> None:
    #         if isinstance(node, RangeVar):
    #             # Handle the case where schemaname or relname might be None
    #             schema = node.schemaname if node.schemaname is not None else "public"
    #             relname = node.relname if node.relname is not None else ""
    #             if relname:  # Only add if we have a valid table name
    #                 tables.add((schema, relname))

    #         # Recursively validate all attributes that might be nodes
    #         for attr_name in node.__slots__:
    #             # Skip private attributes and methods
    #             if attr_name.startswith("_"):
    #                 continue

    #             try:
    #                 attr = getattr(node, attr_name)
    #             except AttributeError:
    #                 # Skip attributes that don't exist (this is normal in pglast)
    #                 continue

    #             # Handle lists of nodes
    #             if isinstance(attr, list):
    #                 for item in attr:
    #                     if isinstance(item, Node):
    #                         walk_node(item)

    #             # Handle tuples of nodes
    #             elif isinstance(attr, tuple):
    #                 for item in attr:
    #                     if isinstance(item, Node):
    #                         walk_node(item)

    #             # Handle single nodes
    #             elif isinstance(attr, Node):
    #                 walk_node(attr)

    #     for stmt in workload_queries:
    #         walk_node(stmt)

    #     return tables

    # async def _get_candidate_indexes(
    #     self, workload_queries: list[str], existing_index_defs: set[str], best_recommendations: list[IndexRecommendation]
    # ) -> list[set[IndexConfig]]:
    #     """Get candidate indexes from the workload queries and existing index definitions."""
    #     # Use Instructor to get index recommendations from the LLM
    #     client = instructor.from_openai(OpenAI())

    #     workload_queries_str = "\n".join(workload_queries)
    #     best_recommendations_str = "\n".join(str(rec) for rec in best_recommendations)

    #     response = client.chat.completions.create(
    #         model="gpt-4o",
    #         response_model=IndexingAlternative,
    #         messages=[
    #             {"role": "system", "content": "You are a helpful assistant that generates index recommendations for a given workload."},
    #             {
    #                 "role": "user",
    #                 "content": f"Here are the queries we are optimizing: {workload_queries_str}\n"
    #                 f"Here are the existing indexes: {existing_index_defs}\n"
    #                 f"Here are the best recommendations so far: {best_recommendations_str}\n\n"
    #                 "Generate a list of possible indexes that would best improve the performance of the queries.",
    #             },
    #         ],
    #     )

    #     # Convert the response to IndexConfig objects
    #     return response.to_list()
