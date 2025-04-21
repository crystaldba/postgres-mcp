from typing import override

from pydantic import BaseModel

import instructor
from openai import OpenAI

from pglast.ast import SelectStmt, Node

from postgres_mcp.artifacts import ErrorResult, PlanNode
from postgres_mcp.explain.explain_plan import ExplainPlanTool

from ..sql import IndexConfig
from ..sql import SqlDriver
from .index_opt_base import IndexRecommendation
from .index_opt_base import IndexTuningBase

class Index(BaseModel):
    table_name: str
    columns: tuple[str, ...]

class IndexingAlternative(BaseModel):
    alternatives: list[set[Index]]

    def to_list(self) -> list[set[IndexConfig]]:
        return [set(IndexConfig(table=index.table_name, columns=index.columns) for index in indexes) for indexes in self.alternatives]

class LLMOptimizerTool(IndexTuningBase):
    def __init__(self,
                 sql_driver: SqlDriver,
                 include_plans: bool = False,
                 max_no_progress_attempts: int = 5,
                 ):
        self.sql_driver = sql_driver
        self.include_plans = include_plans
        self.max_no_progress_attempts = max_no_progress_attempts

    @override
    async def _generate_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], existing_index_defs: set[str]
    ) -> tuple[set[IndexConfig], float]:
        """Generate index tuning queries using optimization by LLM."""

        raise NotImplementedError("Optimization by LLM is not implemented yet.")

        # # For now we support only one table at a time
        # if len(set(table for _, _, table in query_weights)) > 1:
        #     raise ValueError("Optimization by LLM supports only one query at a time.")

        # query = query_weights[0][0]

        # # Generate explain plan for the query
        # explain_tool = ExplainPlanTool(self.sql_driver)
        # explain_result = await explain_tool.explain(query)
        # if isinstance(explain_result, ErrorResult):
        #     raise ValueError(f"Failed to generate explain plan: {explain_result.to_text()}")
        # plan = explain_result.



        # def walk_plan_node(node: PlanNode) -> None:
        #     # Check if this is an index scan
        #     if "Index Scan" in node.node_type and node.relation_name:
        #         index_names.add(node.relation_name)

        #     # Recursively walk through child nodes
        #     for child in node.children:
        #         walk_plan_node(child)

        # Walk through the plan tree starting from the root
        # walk_plan_node(plan)

        # workload_queries = [q for q, _, _ in query_weights]

        # # get all indexes that are used in the


        # # get the current cost
        # original_cost = await self._evaluate_configuration_cost(query_weights, frozenset())
        # best_cost = original_cost
        # best_index_config: set[IndexConfig] = existing_index_defs

        # best_recommendations: list[IndexRecommendation] = []

        # attempts_remaining = self.max_no_progress_attempts
        # while attempts_remaining > 0:
        #     attempts_remaining -= 1

        #     test_indexes = await self._get_candidate_indexes(workload_queries, existing_index_defs, best_recommendations)
        #     if not test_indexes:
        #         continue

        #     # Evaluate test indexes and track which configuration gives minimum cost
        #     test_index_costs = [
        #         (await self._evaluate_configuration_cost(query_weights, frozenset(indexes)), indexes) for indexes in test_indexes
        #     ]

        #     # Find minimum cost and corresponding index configuration
        #     min_cost, min_cost_indexes = min(test_index_costs, key=lambda x: x[0], default=(best_cost, None))

        #     if min_cost_indexes and min_cost < best_cost:
        #         best_cost = min_cost
        #         best_index_config = min_cost_indexes
        #         attempts_remaining = max_no_progress_attempts

        # return (best_index_config, best_cost)


    def _get_tables(self, workload_queries: list[SelectStmt]) -> set[tuple[str, str]]:

        raise NotImplementedError("Getting tables from the workload queries is not implemented yet.")
        # tables: set[tuple[str, str]] = set()

        # def walk_node(node: Node) -> None:
        #     if isinstance(node, TableRef):
        #         tables.add((node.schemaname, node.relname))

        #     # Recursively validate all attributes that might be nodes
        #     for attr_name in node.__slots__:
        #         # Skip private attributes and methods
        #         if attr_name.startswith("_"):
        #             continue

        #         try:
        #             attr = getattr(node, attr_name)
        #         except AttributeError:
        #             # Skip attributes that don't exist (this is normal in pglast)
        #             continue

        #         # Handle lists of nodes
        #         if isinstance(attr, list):
        #             for item in attr:
        #                 if isinstance(item, Node):
        #                     walk_node(item)

        #         # Handle tuples of nodes
        #         elif isinstance(attr, tuple):
        #             for item in attr:
        #                 if isinstance(item, Node):
        #                     walk_node(item)

        #         # Handle single nodes
        #         elif isinstance(attr, Node):
        #             walk_node(attr)

        # for stmt in workload_queries:
        #     walk_node(stmt)

        # return tables

    async def _get_candidate_indexes(
        self, workload_queries: list[str], existing_index_defs: set[str], best_recommendations: list[IndexRecommendation]
    ) -> list[set[IndexConfig]]:
        """Get candidate indexes from the workload queries and existing index definitions."""

        raise NotImplementedError("Getting candidate indexes is not implemented yet.")

        client = instructor.from_openai(OpenAI())

        workload_queries_str = "\n".join(workload_queries)
        # existing_index_defs_str = "\n".join(existing_index_defs)
        best_recommendations_str = "\n".join(best_recommendations)

        response = client.chat.completions.create(
            model="gpt-4o",
            response_model=IndexingAlternative,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that generates index recommendations for a given workload."},
                {"role": "user", "content":
                    f"Here are the queries we are optimizing: {workload_queries}\n"
                    # "Here are the existing indexes on the tables: {existing_index_defs}\n"
                    "Here are the best recommendations so far: {best_recommendations}\n\n"
                    "Generate a list of possible indexes that would best improve the performance of the queries."},
            ],
        )

        return response.to_list()

