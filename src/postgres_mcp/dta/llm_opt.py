from typing import override

from pglast.ast import SelectStmt

from ..sql import IndexConfig
from ..sql import SqlDriver
from .index_opt_base import IndexRecommendation
from .index_opt_base import IndexTuningBase


class LLMOptimizerTool(IndexTuningBase):
    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    @override
    async def _generate_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], existing_index_defs: set[str]
    ) -> tuple[set[IndexConfig], float]:
        """Generate index tuning queries using optimization by LLM."""

        workload_queries = [q for q, _, _ in query_weights]
        best_recommendations: list[IndexRecommendation] = []

        # get the current cost
        original_cost = await self._evaluate_configuration_cost(query_weights, frozenset())
        best_cost = original_cost
        best_index_config: set[IndexConfig] = set()

        max_no_progress_attempts = 5
        attempts_remaining = max_no_progress_attempts
        while attempts_remaining > 0:
            attempts_remaining -= 1

            test_indexes = await self._get_candidate_indexes(workload_queries, existing_index_defs, best_recommendations)
            if not test_indexes:
                continue

            # Evaluate test indexes and track which configuration gives minimum cost
            test_index_costs = [
                (await self._evaluate_configuration_cost(query_weights, frozenset(indexes)), set(indexes)) for indexes in test_indexes
            ]

            # Find minimum cost and corresponding index configuration
            min_cost, min_cost_indexes = min(test_index_costs, key=lambda x: x[0], default=(best_cost, None))

            if min_cost_indexes and min_cost < best_cost:
                best_cost = min_cost
                best_index_config = min_cost_indexes
                attempts_remaining = max_no_progress_attempts

        return (best_index_config, best_cost)

    async def _evaluate_configuration_cost(self, weighted_workload: list[tuple[str, SelectStmt, float]], indexes: frozenset[IndexConfig]) -> float:
        """Evaluate cost of this index configuration."""
        total_cost = 0.0
        valid_queries = 0
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
            return float("inf")

        return total_cost / valid_queries

    async def _get_candidate_indexes(
        self, workload_queries: list[str], existing_index_defs: set[str], best_recommendations: list[IndexRecommendation]
    ) -> list[list[IndexConfig]]:
        """Get candidate indexes from the workload queries and existing index definitions."""
        return [[]]
