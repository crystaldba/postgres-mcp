from typing import override

from pglast.ast import SelectStmt

from ..sql import SqlDriver
from .dta_calc import IndexRecommendation
from .dta_calc import IndexTuningBase


class LLMOptimizerTool(IndexTuningBase):
    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    @override
    async def _generate_recommendations(self, query_weights: list[tuple[str, SelectStmt, float]]) -> list[IndexRecommendation]:
        """Generate index tuning queries."""
        raise NotImplementedError("LLMOptimizerTool.analyze_queries is not implemented")
