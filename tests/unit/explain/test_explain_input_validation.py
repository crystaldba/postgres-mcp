"""Tests for SQL injection prevention in ExplainPlanTool._validate_explain_input.

These tests verify that the EXPLAIN tool validates its input SQL to prevent
SQL injection via the f-string concatenation used to build EXPLAIN queries.
This ensures the readOnlyHint=True annotation on explain_query is honored
regardless of the server access mode.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from postgres_mcp.artifacts import ErrorResult
from postgres_mcp.explain import ExplainPlanTool


@pytest_asyncio.fixture
async def mock_sql_driver():
    """Create a mock SQL driver for testing."""
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    return driver


# ============================================================================
# _validate_explain_input unit tests
# ============================================================================


class TestValidateExplainInput:
    """Tests for the _validate_explain_input static method."""

    def test_valid_select(self):
        """Valid SELECT queries should pass validation."""
        assert ExplainPlanTool._validate_explain_input("SELECT * FROM users") is None
        assert ExplainPlanTool._validate_explain_input("SELECT id, name FROM users WHERE age > 18") is None
        assert ExplainPlanTool._validate_explain_input("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id") is None

    def test_valid_select_with_cte(self):
        """SELECT with CTE (WITH clause) should pass."""
        assert ExplainPlanTool._validate_explain_input("WITH active AS (SELECT * FROM users WHERE active) SELECT * FROM active") is None

    def test_valid_select_with_subquery(self):
        """SELECT with subquery should pass."""
        assert ExplainPlanTool._validate_explain_input("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)") is None

    def test_valid_insert_without_analyze(self):
        """INSERT should pass validation when analyze=False (EXPLAIN without ANALYZE is safe)."""
        assert ExplainPlanTool._validate_explain_input("INSERT INTO users (name) VALUES ('test')", analyze=False) is None

    def test_valid_update_without_analyze(self):
        """UPDATE should pass validation when analyze=False."""
        assert ExplainPlanTool._validate_explain_input("UPDATE users SET name = 'test' WHERE id = 1", analyze=False) is None

    def test_valid_delete_without_analyze(self):
        """DELETE should pass validation when analyze=False."""
        assert ExplainPlanTool._validate_explain_input("DELETE FROM users WHERE id = 1", analyze=False) is None

    def test_reject_multi_statement_injection(self):
        """Multi-statement input should be rejected (prevents SQL injection)."""
        result = ExplainPlanTool._validate_explain_input("SELECT 1; DROP TABLE users")
        assert result is not None
        assert "single SQL statement" in result

    def test_reject_multi_statement_with_comment(self):
        """Multi-statement with comment-based injection should be rejected."""
        result = ExplainPlanTool._validate_explain_input("SELECT 1; DROP TABLE users; --")
        assert result is not None
        assert "single SQL statement" in result

    def test_reject_empty_query(self):
        """Empty queries should be rejected."""
        assert ExplainPlanTool._validate_explain_input("") is not None
        assert ExplainPlanTool._validate_explain_input("   ") is not None

    def test_reject_invalid_sql(self):
        """Invalid SQL syntax should be rejected."""
        result = ExplainPlanTool._validate_explain_input("NOT VALID SQL AT ALL")
        assert result is not None
        assert "Invalid SQL syntax" in result

    def test_analyze_rejects_delete(self):
        """EXPLAIN ANALYZE should reject DELETE to prevent data modification."""
        result = ExplainPlanTool._validate_explain_input("DELETE FROM users WHERE 1=1", analyze=True)
        assert result is not None
        assert "SELECT" in result

    def test_analyze_rejects_update(self):
        """EXPLAIN ANALYZE should reject UPDATE to prevent data modification."""
        result = ExplainPlanTool._validate_explain_input("UPDATE users SET name = 'hacked'", analyze=True)
        assert result is not None
        assert "SELECT" in result

    def test_analyze_rejects_insert(self):
        """EXPLAIN ANALYZE should reject INSERT to prevent data modification."""
        result = ExplainPlanTool._validate_explain_input("INSERT INTO users (name) VALUES ('test')", analyze=True)
        assert result is not None
        assert "SELECT" in result

    def test_analyze_allows_select(self):
        """EXPLAIN ANALYZE should allow SELECT queries."""
        assert ExplainPlanTool._validate_explain_input("SELECT * FROM users", analyze=True) is None

    def test_reject_drop_table(self):
        """DDL statements should be rejected as multi-statement or invalid."""
        # DROP TABLE alone is a single statement but not a valid EXPLAIN target in pglast
        result = ExplainPlanTool._validate_explain_input("SELECT 1; DROP TABLE users")
        assert result is not None

    def test_reject_copy_injection(self):
        """COPY-based data exfiltration should be rejected."""
        result = ExplainPlanTool._validate_explain_input("SELECT 1; COPY (SELECT * FROM pg_shadow) TO '/tmp/pwned.csv'")
        assert result is not None

    def test_valid_select_with_bind_params(self):
        """Queries with bind parameters ($1, $2) should pass validation."""
        assert ExplainPlanTool._validate_explain_input("SELECT * FROM users WHERE id = $1") is None
        assert ExplainPlanTool._validate_explain_input("SELECT * FROM users WHERE id = $1 AND name = $2") is None


# ============================================================================
# Integration tests: explain method blocks injection
# ============================================================================


class TestExplainBlocksInjection:
    """Tests that the explain() method blocks SQL injection attempts."""

    @pytest.mark.asyncio
    async def test_explain_blocks_multi_statement(self, mock_sql_driver):
        """explain() should return ErrorResult for multi-statement injection."""
        tool = ExplainPlanTool(sql_driver=mock_sql_driver)
        result = await tool.explain("SELECT 1; DROP TABLE users")
        assert isinstance(result, ErrorResult)
        # Verify the query was never sent to the database
        mock_sql_driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explain_analyze_blocks_delete(self, mock_sql_driver):
        """explain_analyze() should block DELETE statements."""
        tool = ExplainPlanTool(sql_driver=mock_sql_driver)
        result = await tool.explain_analyze("DELETE FROM users WHERE 1=1")
        assert isinstance(result, ErrorResult)
        mock_sql_driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explain_analyze_blocks_update(self, mock_sql_driver):
        """explain_analyze() should block UPDATE statements."""
        tool = ExplainPlanTool(sql_driver=mock_sql_driver)
        result = await tool.explain_analyze("UPDATE users SET admin = true")
        assert isinstance(result, ErrorResult)
        mock_sql_driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explain_blocks_empty_input(self, mock_sql_driver):
        """explain() should return ErrorResult for empty input."""
        tool = ExplainPlanTool(sql_driver=mock_sql_driver)
        result = await tool.explain("")
        assert isinstance(result, ErrorResult)
        mock_sql_driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explain_with_hypothetical_indexes_blocks_injection(self, mock_sql_driver):
        """explain_with_hypothetical_indexes() should block SQL injection."""
        tool = ExplainPlanTool(sql_driver=mock_sql_driver)
        result = await tool.explain_with_hypothetical_indexes(
            "SELECT 1; DROP TABLE users",
            [{"table": "users", "columns": ["email"]}],
        )
        assert isinstance(result, ErrorResult)
        mock_sql_driver.execute_query.assert_not_awaited()
