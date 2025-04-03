import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from postgres_mcp.server import (
    explain_query,
    explain_analyze_query,
    explain_with_hypothetical_indexes,
    parse_sql_query,
    format_text_response,
)
from postgres_mcp.explain.tools import ErrorResult


@pytest_asyncio.fixture
async def mock_safe_sql_driver():
    """Create a mock SafeSqlDriver for testing."""
    driver = MagicMock()
    return driver


@pytest.fixture
def mock_explain_plan_tool():
    """Create a mock ExplainPlanTool."""
    tool = MagicMock()
    tool.explain = AsyncMock()
    tool.explain_analyze = AsyncMock()
    tool.explain_with_hypothetical_indexes = AsyncMock()
    return tool


@pytest.fixture
def mock_sql_parser_tool():
    """Create a mock SqlParserTool."""
    tool = MagicMock()
    tool.parse_sql = MagicMock()
    return tool


@pytest.mark.asyncio
async def test_explain_query_success(mock_safe_sql_driver, mock_explain_plan_tool):
    """Test explain_query when successful."""
    # Set up mock
    mock_result = MagicMock()
    mock_result.value = json.dumps({"Plan": {"Node Type": "Seq Scan"}})
    mock_explain_plan_tool.explain.return_value = mock_result

    # Patch the get_safe_sql_driver and ExplainPlanTool constructor
    with (
        patch(
            "postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_sql_driver
        ),
        patch(
            "postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_plan_tool
        ),
    ):
        # Execute the function
        result = await explain_query("SELECT * FROM users")

        # Verify
        mock_explain_plan_tool.explain.assert_called_once_with("SELECT * FROM users")
        assert result == format_text_response(mock_result.value)


@pytest.mark.asyncio
async def test_explain_query_error(mock_safe_sql_driver, mock_explain_plan_tool):
    """Test explain_query when an error occurs."""
    # Set up mock to return an error
    error_message = "Error processing explain plan"
    mock_error = ErrorResult(error_message)
    mock_explain_plan_tool.explain.return_value = mock_error

    # Patch the get_safe_sql_driver and ExplainPlanTool constructor
    with (
        patch(
            "postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_sql_driver
        ),
        patch(
            "postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_plan_tool
        ),
    ):
        # Execute the function
        result = await explain_query("SELECT * FROM users")

        # Verify error format - check that the response contains the error text
        # The server might add prefixes or other formatting
        assert isinstance(result, list)
        assert len(result) == 1
        assert error_message in result[0].text


@pytest.mark.asyncio
async def test_explain_analyze_query_success(
    mock_safe_sql_driver, mock_explain_plan_tool
):
    """Test explain_analyze_query when successful."""
    # Set up mock
    mock_result = MagicMock()
    mock_result.value = json.dumps(
        {"Plan": {"Node Type": "Seq Scan"}, "Execution Time": 1.23}
    )
    mock_explain_plan_tool.explain_analyze.return_value = mock_result

    # Patch the get_safe_sql_driver and ExplainPlanTool constructor
    with (
        patch(
            "postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_sql_driver
        ),
        patch(
            "postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_plan_tool
        ),
    ):
        # Execute the function
        result = await explain_analyze_query("SELECT * FROM users")

        # Verify
        mock_explain_plan_tool.explain_analyze.assert_called_once_with(
            "SELECT * FROM users"
        )
        assert result == format_text_response(mock_result.value)


@pytest.mark.asyncio
async def test_explain_with_hypothetical_indexes_success(
    mock_safe_sql_driver, mock_explain_plan_tool
):
    """Test explain_with_hypothetical_indexes when successful."""
    # Set up mock
    mock_result = MagicMock()
    mock_result.value = json.dumps({"Plan": {"Node Type": "Index Scan"}})
    mock_explain_plan_tool.explain_with_hypothetical_indexes.return_value = mock_result

    # Test data
    test_sql = "SELECT * FROM users WHERE email = 'test@example.com'"
    test_indexes = [{"table": "users", "columns": ["email"], "using": "btree"}]

    # Patch the get_safe_sql_driver and ExplainPlanTool constructor
    with (
        patch(
            "postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_sql_driver
        ),
        patch(
            "postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_plan_tool
        ),
    ):
        # Execute the function
        result = await explain_with_hypothetical_indexes(test_sql, test_indexes)

        # Verify
        mock_explain_plan_tool.explain_with_hypothetical_indexes.assert_called_once_with(
            test_sql, test_indexes
        )
        assert result == format_text_response(mock_result.value)


@pytest.mark.asyncio
async def test_parse_sql_query_success(mock_sql_parser_tool):
    """Test parse_sql_query when successful."""
    # Set up mock
    mock_result = MagicMock()
    mock_result.value = json.dumps({"stmt": [{"SelectStmt": {}}]})
    mock_sql_parser_tool.parse_sql.return_value = mock_result

    # Patch the SqlParserTool constructor
    with patch("postgres_mcp.server.SqlParserTool", return_value=mock_sql_parser_tool):
        # Execute the function
        result = await parse_sql_query("SELECT * FROM users")

        # Verify
        mock_sql_parser_tool.parse_sql.assert_called_once_with("SELECT * FROM users")
        assert result == format_text_response(mock_result.value)


@pytest.mark.asyncio
async def test_parse_sql_query_error(mock_sql_parser_tool):
    """Test parse_sql_query when an error occurs."""
    # Set up mock to return an error
    error_message = "Syntax error"
    mock_error = ErrorResult(error_message)
    mock_sql_parser_tool.parse_sql.return_value = mock_error

    # Patch the SqlParserTool constructor
    with patch("postgres_mcp.server.SqlParserTool", return_value=mock_sql_parser_tool):
        # Execute the function
        result = await parse_sql_query("INVALID SQL")

        # Verify error format - check that the response contains the error text
        assert isinstance(result, list)
        assert len(result) == 1
        assert error_message in result[0].text


@pytest.mark.asyncio
async def test_exception_handling_in_explain_query():
    """Test exception handling in explain_query."""
    # Error message
    error_message = "Unexpected error"

    # Patch the get_safe_sql_driver to raise an exception
    with patch(
        "postgres_mcp.server.get_safe_sql_driver",
        side_effect=Exception(error_message),
    ):
        # Execute the function
        result = await explain_query("SELECT * FROM users")

        # Verify error response contains the error message
        assert isinstance(result, list)
        assert len(result) == 1
        assert error_message in result[0].text


@pytest.mark.asyncio
async def test_exception_handling_in_explain_with_hypothetical_indexes():
    """Test exception handling in explain_with_hypothetical_indexes."""
    # Error message
    error_message = "Database connection error"

    # Test data
    test_sql = "SELECT * FROM users WHERE email = 'test@example.com'"
    test_indexes = [{"table": "users", "columns": ["email"], "using": "btree"}]

    # Patch the get_safe_sql_driver to raise an exception
    with patch(
        "postgres_mcp.server.get_safe_sql_driver",
        side_effect=Exception(error_message),
    ):
        # Execute the function
        result = await explain_with_hypothetical_indexes(test_sql, test_indexes)

        # Verify error response contains the error message
        assert isinstance(result, list)
        assert len(result) == 1
        assert error_message in result[0].text
