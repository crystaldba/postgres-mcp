"""Database Tuning Advisor (DTA) tool for Postgres MCP."""

import logging
import humanize
from typing import Any, Dict, List

from .artifacts import ExplainPlanArtifact
from .artifacts import calculate_improvement_multiple
from .dta_calc import DatabaseTuningAdvisor, DTASession, IndexConfig
from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


class DTATool:
    """Database Tuning Advisor tool for recommending indexes."""

    def __init__(self, sql_driver: SqlDriver):
        """
        Initialize the DTA tool.

        Args:
            conn: The PostgreSQL connection object
        """
        self.sql_driver = sql_driver
        self.dta = None

    async def do_init(self):
        """Initialize the DatabaseTuningAdvisor if not already initialized."""
        self.dta = DatabaseTuningAdvisor(self.sql_driver)

    async def _create_recommendations_response(
        self, session: DTASession
    ) -> Dict[str, Any]:
        """
        Create a structured JSON response from a DTASession.

        Args:
            session: DTASession containing recommendations

        Returns:
            Dictionary with summary and recommendations
        """
        if session.error:
            return {
                "error": session.error,
                "_langfuse_trace": session.dta_traces,
            }
        if not session.recommendations:
            return {
                "recommendations": "No index recommendations found.",
                "_langfuse_trace": session.dta_traces,
            }

        # Calculate overall statistics
        total_size_bytes = sum(
            rec.estimated_size_bytes for rec in session.recommendations
        )

        # Calculate overall performance improvement
        initial_cost = (
            session.recommendations[0].progressive_base_cost
            if session.recommendations
            else 0
        )
        final_cost = (
            session.recommendations[-1].progressive_recommendation_cost
            if session.recommendations
            else 1.0
        )
        overall_improvement = calculate_improvement_multiple(initial_cost, final_cost)

        # Build recommendations list
        recommendations = []
        for index_apply_order, rec in enumerate(session.recommendations):
            rec_dict = {
                "index_apply_order": index_apply_order + 1,
                "index_target_table": rec.table,
                "index_target_columns": rec.columns,
                "benefit_of_this_index_only": {
                    "improvement_multiple": f"{rec.individual_improvement_multiple:.1f}",
                    "base_cost": f"{rec.individual_base_cost:.1f}",
                    "new_cost": f"{rec.individual_recommendation_cost:.1f}",
                },
                "benefit_after_previous_indexes": {
                    "improvement_multiple": f"{rec.progressive_improvement_multiple:.1f}",
                    "base_cost": f"{rec.progressive_base_cost:.1f}",
                    "new_cost": f"{rec.progressive_recommendation_cost:.1f}",
                },
                "index_estimated_size": humanize.naturalsize(rec.estimated_size_bytes),
                "index_definition": rec.definition,
            }
            if rec.potential_problematic_reason == "long_text_column":
                rec_dict["warning"] = (
                    "This index is potentially problematic because it includes a long text column. "
                    "You might not be able to create this index if the index row size becomes too large "
                    "(i.e., more than 8191 bytes)."
                )
            elif rec.potential_problematic_reason:
                rec_dict["warning"] = (
                    f"This index is potentially problematic because it includes a {rec.potential_problematic_reason} column."
                )
            recommendations.append(rec_dict)

        # Create the result JSON object with summary
        summary = {
            "total_recommendations": len(session.recommendations),
            "base_cost": f"{initial_cost:.1f}",
            "new_cost": f"{final_cost:.1f}",
            "total_size_bytes": humanize.naturalsize(total_size_bytes),
            "improvement_multiple": f"{overall_improvement:.1f}",
        }

        # Generate query impact section using helper function
        query_impact = await self._generate_query_impact(session)

        return {
            "summary": summary,
            "recommendations": recommendations,
            "query_impact": query_impact,
            "_langfuse_trace": session.dta_traces,
        }

    async def _generate_query_impact(self, session: DTASession) -> List[Dict[str, Any]]:
        """
        Generate the query impact section showing before/after explain plans.

        Args:
            session: DTASession containing recommendations

        Returns:
            List of dictionaries with query and explain plans
        """
        query_impact = []

        # Get workload queries from the first recommendation
        # (All recommendations have the same queries)
        if not session.recommendations:
            return query_impact

        workload_queries = session.recommendations[0].queries

        # Remove duplicates while preserving order
        seen = set()
        unique_queries = []
        for q in workload_queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        # Get before and after plans for each query
        if unique_queries and self.dta:
            for query in unique_queries:
                # Get plan with no indexes
                before_plan = await self.dta.get_explain_plan_with_indexes(
                    query, frozenset()
                )

                # Get plan with all recommended indexes
                index_configs = frozenset(
                    IndexConfig(rec.table, rec.columns, rec.using)
                    for rec in session.recommendations
                )
                after_plan = await self.dta.get_explain_plan_with_indexes(
                    query, index_configs
                )

                # Extract costs from plans
                base_cost = self.dta.extract_cost_from_json_plan(before_plan)
                new_cost = self.dta.extract_cost_from_json_plan(after_plan)

                # Calculate improvement multiple
                improvement_multiple = "∞"  # Default for cases where new_cost is zero
                if new_cost > 0 and base_cost > 0:
                    improvement_multiple = (
                        f"{calculate_improvement_multiple(base_cost, new_cost):.1f}"
                    )

                before_plan_text = ExplainPlanArtifact.format_plan_summary(before_plan)
                after_plan_text = ExplainPlanArtifact.format_plan_summary(after_plan)
                diff_text = ExplainPlanArtifact.create_plan_diff(
                    before_plan, after_plan
                )

                # Add to query impact with costs and improvement
                query_impact.append(
                    {
                        "query": query,
                        "base_cost": f"{base_cost:.1f}",
                        "new_cost": f"{new_cost:.1f}",
                        "improvement_multiple": improvement_multiple,
                        "before_explain_plan": "```\n" + before_plan_text + "\n```",
                        "after_explain_plan": "```\n" + after_plan_text + "\n```",
                        "explain_plan_diff": "```\n" + diff_text + "\n```",
                    }
                )

        return query_impact

    async def _execute_analysis(
        self,
        query_list=None,
        min_calls=50,
        min_avg_time_ms=5.0,
        limit=100,
        max_index_size_mb=10000,
    ):
        """
        Execute analysis with proper error handling.

        Args:
            **kwargs: Arguments to pass to the analysis function

        Returns:
            Dict with recommendations or dict with error
        """
        try:
            await self.do_init()
            if self.dta is None:
                return {"error": "DatabaseTuningAdvisor not initialized"}

            session = await self.dta.analyze_workload(
                query_list=query_list,
                min_calls=min_calls,
                min_avg_time_ms=min_avg_time_ms,
                limit=limit,
                max_index_size_mb=max_index_size_mb,
            )
            result = await self._create_recommendations_response(session)

            return result
        except Exception as e:
            logger.error(f"Error analyzing queries: {e}", exc_info=True)
            return {"error": f"Error analyzing queries: {e}"}

    async def analyze_workload(self, max_index_size_mb=10000):
        """
        Analyze SQL workload and recommend indexes.

        This method analyzes queries from database query history, examining
        frequently executed and costly queries to recommend the most beneficial indexes.

        Args:
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        return await self._execute_analysis(
            min_calls=50,
            min_avg_time_ms=5.0,
            limit=100,
            max_index_size_mb=max_index_size_mb,
        )

    async def analyze_queries(self, queries, max_index_size_mb=10000):
        """
        Analyze a list of SQL queries and recommend indexes.

        This method examines the provided SQL queries and recommends
        indexes that would improve their performance.

        Args:
            queries: List of SQL queries to analyze
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        if not queries:
            return {"error": "No queries provided for analysis"}

        return await self._execute_analysis(
            query_list=queries,
            min_calls=0,  # Ignore min calls for explicit query list
            min_avg_time_ms=0,  # Ignore min time for explicit query list
            limit=0,  # Ignore limit for explicit query list
            max_index_size_mb=max_index_size_mb,
        )

    async def analyze_single_query(self, query, max_index_size_mb=10000):
        """
        Analyze a single SQL query and recommend indexes.

        This method examines the provided SQL query and recommends
        indexes that would improve its performance.

        Args:
            query: SQL query to analyze
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        return await self._execute_analysis(
            query_list=[query],
            min_calls=0,  # Ignore min calls for explicit query
            min_avg_time_ms=0,  # Ignore min time for explicit query
            limit=0,  # Ignore limit for explicit query
            max_index_size_mb=max_index_size_mb,
        )
