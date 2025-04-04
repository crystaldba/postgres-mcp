import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from postgres_mcp.dta.artifacts import ExplainPlanArtifact
from postgres_mcp.explain.tools import ErrorResult
from postgres_mcp.explain.tools import ExplainPlanTool


class MockCell:
    def __init__(self, data):
        self.cells = data


@pytest_asyncio.fixture
async def mock_sql_driver():
    """Create a mock SQL driver for testing."""
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    return driver


@pytest.mark.asyncio
async def test_explain_plan_tool_initialization(mock_sql_driver):
    """Test initialization of ExplainPlanTool."""
    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    assert tool.sql_driver == mock_sql_driver


@pytest.mark.asyncio
async def test_has_bind_variables():
    """Test the _has_bind_variables method."""
    tool = ExplainPlanTool(sql_driver=MagicMock())

    # Test with bind variables
    assert tool._has_bind_variables("SELECT * FROM users WHERE id = $1") is True  # type: ignore
    assert tool._has_bind_variables("INSERT INTO users VALUES ($1, $2)") is True  # type: ignore

    # Test without bind variables
    assert tool._has_bind_variables("SELECT * FROM users WHERE id = 1") is False  # type: ignore
    assert tool._has_bind_variables("INSERT INTO users VALUES (1, 'test')") is False  # type: ignore


@pytest.mark.asyncio
async def test_explain_success(mock_sql_driver):
    """Test successful execution of explain."""
    # Prepare mock response
    plan_data = {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "users",
            "Startup Cost": 0.00,
            "Total Cost": 10.00,
            "Plan Rows": 100,
            "Plan Width": 20,
        }
    }

    mock_sql_driver.execute_query.return_value = [MockCell({"QUERY PLAN": [plan_data]})]

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain("SELECT * FROM users")

    # Verify query was called with expected parameters
    mock_sql_driver.execute_query.assert_called_once()
    call_args = mock_sql_driver.execute_query.call_args[0][0]
    assert "EXPLAIN (FORMAT JSON) SELECT * FROM users" in call_args

    # Verify result is as expected
    assert isinstance(result, ExplainPlanArtifact)
    assert json.loads(result.value) == plan_data


@pytest.mark.asyncio
async def test_explain_with_bind_variables(mock_sql_driver):
    """Test explain with bind variables."""
    # Prepare mock response
    plan_data = {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "users",
            "Startup Cost": 0.00,
            "Total Cost": 10.00,
            "Plan Rows": 100,
            "Plan Width": 20,
        }
    }

    mock_sql_driver.execute_query.return_value = [MockCell({"QUERY PLAN": [plan_data]})]

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    _result = await tool.explain("SELECT * FROM users WHERE id = $1")

    # Verify query includes GENERIC_PLAN option
    call_args = mock_sql_driver.execute_query.call_args[0][0]
    assert "EXPLAIN (FORMAT JSON, GENERIC_PLAN) SELECT * FROM users WHERE id = $1" in call_args


@pytest.mark.asyncio
async def test_explain_analyze_with_bind_variables(mock_sql_driver):
    """Test explain analyze with bind variables returns error."""
    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain_analyze("SELECT * FROM users WHERE id = $1")

    # Should return error result
    assert isinstance(result, ErrorResult)
    assert "bind variables" in result.value
    # Ensure no query was executed
    mock_sql_driver.execute_query.assert_not_called()


@pytest.mark.asyncio
async def test_explain_analyze_success(mock_sql_driver):
    """Test successful execution of explain analyze."""
    # Prepare mock response with execution statistics
    plan_data = {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "users",
            "Startup Cost": 0.00,
            "Total Cost": 10.00,
            "Plan Rows": 100,
            "Plan Width": 20,
            "Actual Startup Time": 0.01,
            "Actual Total Time": 1.23,
            "Actual Rows": 95,
            "Actual Loops": 1,
        },
        "Planning Time": 0.05,
        "Execution Time": 1.30,
    }

    mock_sql_driver.execute_query.return_value = [MockCell({"QUERY PLAN": [plan_data]})]

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain_analyze("SELECT * FROM users")

    # Verify query was called with expected parameters
    call_args = mock_sql_driver.execute_query.call_args[0][0]
    assert "EXPLAIN (FORMAT JSON, ANALYZE) SELECT * FROM users" in call_args

    # Verify result is as expected
    assert isinstance(result, ExplainPlanArtifact)
    assert json.loads(result.value) == plan_data


@pytest.mark.asyncio
async def test_explain_with_error(mock_sql_driver):
    """Test handling of error in explain."""
    # Configure mock to raise exception
    mock_sql_driver.execute_query.side_effect = Exception("Database error")

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain("SELECT * FROM users")

    # Verify error handling
    assert isinstance(result, ErrorResult)
    assert "Database error" in result.value


@pytest.mark.asyncio
async def test_explain_with_invalid_response(mock_sql_driver):
    """Test handling of invalid response format."""
    # Return invalid response format
    mock_sql_driver.execute_query.return_value = [
        MockCell({"QUERY PLAN": "invalid"})  # Not a list
    ]

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain("SELECT * FROM users")

    # Verify error handling
    assert isinstance(result, ErrorResult)
    assert "Expected list" in result.value


@pytest.mark.asyncio
async def test_explain_with_empty_result(mock_sql_driver):
    """Test handling of empty result set."""
    # Return empty result
    mock_sql_driver.execute_query.return_value = None

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain("SELECT * FROM users")

    # Verify error handling
    assert isinstance(result, ErrorResult)
    assert "No results" in result.value


@pytest.mark.asyncio
async def test_explain_with_empty_plan_data(mock_sql_driver):
    """Test handling of empty plan data."""
    # Return empty plan data list
    mock_sql_driver.execute_query.return_value = [MockCell({"QUERY PLAN": []})]

    tool = ExplainPlanTool(sql_driver=mock_sql_driver)
    result = await tool.explain("SELECT * FROM users")

    # Verify error handling
    assert isinstance(result, ErrorResult)
    assert "No results" in result.value
