from __future__ import annotations

import logging
import re
import json
from typing import TYPE_CHECKING, Any, Dict, List, Union

from ..dta.artifacts import ExplainPlanArtifact

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..sql.sql_driver import SqlDriver


class ErrorResult:
    """Simple error result class."""

    def __init__(self, message: str):
        self.value = message


class JsonResult:
    """Simple JSON result class."""

    def __init__(self, data: Any):
        self.value = json.dumps(data, indent=2)
        self.data = data


class ExplainPlanTool:
    """Tool for generating and analyzing PostgreSQL explain plans."""

    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    async def explain(self, sql_query: str) -> Union[ExplainPlanArtifact, ErrorResult]:
        """
        Generate an EXPLAIN plan for a SQL query.

        Args:
            sql_query: The SQL query to explain

        Returns:
            ExplainPlanArtifact or ErrorResult
        """
        use_generic_plan = self._has_bind_variables(sql_query)
        return await self._run_explain_query(
            sql_query, analyze=False, generic_plan=use_generic_plan
        )

    async def explain_analyze(
        self, sql_query: str
    ) -> Union[ExplainPlanArtifact, ErrorResult]:
        """
        Generate an EXPLAIN ANALYZE plan for a SQL query.

        Args:
            sql_query: The SQL query to explain and analyze

        Returns:
            ExplainPlanArtifact or ErrorResult
        """
        if self._has_bind_variables(sql_query):
            return ErrorResult(
                "EXPLAIN ANALYZE cannot be used with bind variables - please replace them with explicit values"
            )
        return await self._run_explain_query(
            sql_query, analyze=True, generic_plan=False
        )

    async def explain_with_hypothetical_indexes(
        self, sql_query: str, hypothetical_indexes: List[Dict[str, Any]]
    ) -> Union[ExplainPlanArtifact, ErrorResult]:
        """
        Generate an explain plan for a query as if certain indexes existed.

        Args:
            sql_query: The SQL query to explain
            hypothetical_indexes: List of index definitions as dictionaries

        Returns:
            ExplainPlanArtifact or ErrorResult
        """
        try:
            # Validate index definitions format
            if not isinstance(hypothetical_indexes, list):
                return ErrorResult(
                    f"Expected list of index definitions, got {type(hypothetical_indexes)}"
                )

            for idx in hypothetical_indexes:
                if not isinstance(idx, dict):
                    return ErrorResult(
                        f"Expected dictionary for index definition, got {type(idx)}"
                    )
                if "table" not in idx:
                    return ErrorResult("Missing 'table' in index definition")
                if "columns" not in idx:
                    return ErrorResult("Missing 'columns' in index definition")
                if not isinstance(idx["columns"], list):
                    # Try to convert to list if it's not already
                    try:
                        idx["columns"] = (
                            list(idx["columns"])
                            if hasattr(idx["columns"], "__iter__")
                            else [idx["columns"]]
                        )
                    except Exception as e:
                        return ErrorResult(
                            f"Expected list for 'columns', got {type(idx['columns'])}: {e}"
                        )

            # Import inside method to avoid circular imports
            from postgres_mcp.dta.dta_calc import DatabaseTuningAdvisor, IndexConfig

            # Convert the index definitions to IndexConfig objects
            indexes = frozenset(
                IndexConfig(
                    table=idx["table"],
                    columns=tuple(idx["columns"]),
                    using=idx.get("using", "btree"),
                )
                for idx in hypothetical_indexes
            )

            # Generate the explain plan using the static method
            plan_data = await DatabaseTuningAdvisor.generate_explain_plan_with_hypothetical_indexes(
                self.sql_driver, sql_query, indexes
            )

            # Check if we got a valid plan
            if (
                not plan_data
                or not isinstance(plan_data, dict)
                or "Plan" not in plan_data
            ):
                return ErrorResult(
                    "Failed to generate a valid explain plan with the hypothetical indexes"
                )

            try:
                # Convert the plan data to an ExplainPlanArtifact
                return ExplainPlanArtifact.from_json_data(plan_data)
            except Exception as e:
                return ErrorResult(f"Error converting explain plan: {e}")

        except Exception as e:
            logger.error(
                f"Error in explain_with_hypothetical_indexes: {e}", exc_info=True
            )
            return ErrorResult(
                f"Error generating explain plan with hypothetical indexes: {e}"
            )

    def _has_bind_variables(self, query: str) -> bool:
        """Check if a query contains bind variables ($1, $2, etc)."""
        return bool(re.search(r"\$\d+", query))

    async def _run_explain_query(
        self, query: str, analyze: bool = False, generic_plan: bool = False
    ) -> Union[ExplainPlanArtifact, ErrorResult]:
        try:
            explain_options = ["FORMAT JSON"]
            if analyze:
                explain_options.append("ANALYZE")
            if generic_plan:
                explain_options.append("GENERIC_PLAN")

            explain_query = f"EXPLAIN ({', '.join(explain_options)}) {query}"
            logger.debug(f"RUNNING EXPLAIN QUERY: {explain_query}")
            rows = await self.sql_driver.execute_query(explain_query)  # type: ignore
            if rows is None:
                return ErrorResult("No results returned from EXPLAIN")

            query_plan_data = rows[0].cells["QUERY PLAN"]

            if not isinstance(query_plan_data, list):
                return ErrorResult(
                    f"Expected list from EXPLAIN, got {type(query_plan_data)}"
                )
            if len(query_plan_data) == 0:
                return ErrorResult("No results returned from EXPLAIN")

            plan_dict = query_plan_data[0]
            if not isinstance(plan_dict, dict):
                return ErrorResult(
                    f"Expected dict in EXPLAIN result list, got {type(plan_dict)} with value {plan_dict}"
                )

            try:
                return ExplainPlanArtifact.from_json_data(plan_dict)
            except Exception as e:
                return ErrorResult(
                    f"Internal error converting explain plan - do not retry: {e}"
                )
        except Exception as e:
            return ErrorResult(f"Error executing explain plan: {e}")
