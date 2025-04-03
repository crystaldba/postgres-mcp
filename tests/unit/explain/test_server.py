import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import postgres_mcp.server as server
from postgres_mcp.dta.artifacts import ExplainPlanArtifact


class MockCell:
    def __init__(self, data):
        self.cells = data


@pytest_asyncio.fixture
async def mock_db_connection():
    """Create a mock DB connection."""
    conn = MagicMock()
    conn.pool_connect = AsyncMock()
    conn.close = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_server_tools_registered():
    """Test that the explain tools are properly registered in the server."""
    # Check that the explain tools are registered
    assert hasattr(server, "explain_query")
    assert hasattr(server, "explain_analyze_query")
    assert hasattr(server, "explain_with_hypothetical_indexes")
    assert hasattr(server, "parse_sql_query")

    # Simply check that the tools are callable functions
    assert callable(server.explain_query)
    assert callable(server.explain_analyze_query)
    assert callable(server.explain_with_hypothetical_indexes)
    assert callable(server.parse_sql_query)


@pytest.mark.asyncio
async def test_explain_query_integration(mock_db_connection):
    """Test the integration of explain_query with real components."""
    # Mock data for the test
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

    # Create a mock ExplainPlanArtifact
    mock_artifact = MagicMock(spec=ExplainPlanArtifact)
    mock_artifact.value = json.dumps(plan_data, indent=2)

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain = AsyncMock(return_value=mock_artifact)

    # Mock the SafeSqlDriver to return our mock driver
    mock_safe_driver = MagicMock()

    # Patch the required components
    with (
        patch("postgres_mcp.server.db_connection", mock_db_connection),
        patch("postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_driver),
        patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool),
    ):
        # Execute the explain_query function
        result = await server.explain_query("SELECT * FROM users")

        # Verify the result
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].text == json.dumps(plan_data, indent=2)


@pytest.mark.asyncio
async def test_explain_analyze_query_integration(mock_db_connection):
    """Test the integration of explain_analyze_query with real components."""
    # Mock data for the test with execution statistics
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

    # Create a mock ExplainPlanArtifact
    mock_artifact = MagicMock(spec=ExplainPlanArtifact)
    mock_artifact.value = json.dumps(plan_data, indent=2)

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain_analyze = AsyncMock(return_value=mock_artifact)

    # Mock the SafeSqlDriver
    mock_safe_driver = MagicMock()

    # Patch the required components
    with (
        patch("postgres_mcp.server.db_connection", mock_db_connection),
        patch("postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_driver),
        patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool),
    ):
        # Execute the explain_analyze_query function
        result = await server.explain_analyze_query("SELECT * FROM users")

        # Verify the result
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].text == json.dumps(plan_data, indent=2)


@pytest.mark.asyncio
async def test_explain_with_hypothetical_indexes_integration(mock_db_connection):
    """Test the integration of explain_with_hypothetical_indexes with real components."""
    # Mock data for the test
    plan_data = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "users",
            "Index Name": "hypothetical_idx_users_email",
            "Startup Cost": 0.00,
            "Total Cost": 5.00,
            "Plan Rows": 1,
            "Plan Width": 20,
        }
    }

    # Create a mock ExplainPlanArtifact
    mock_artifact = MagicMock(spec=ExplainPlanArtifact)
    mock_artifact.value = json.dumps(plan_data, indent=2)

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain_with_hypothetical_indexes = AsyncMock(
        return_value=mock_artifact
    )

    # Mock the SafeSqlDriver
    mock_safe_driver = MagicMock()

    # Test data
    test_sql = "SELECT * FROM users WHERE email = 'test@example.com'"
    test_indexes = [{"table": "users", "columns": ["email"]}]

    # Patch the required components
    with (
        patch("postgres_mcp.server.db_connection", mock_db_connection),
        patch("postgres_mcp.server.get_safe_sql_driver", return_value=mock_safe_driver),
        patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool),
    ):
        # Execute the explain_with_hypothetical_indexes function
        result = await server.explain_with_hypothetical_indexes(test_sql, test_indexes)

        # Verify the result
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].text == json.dumps(plan_data, indent=2)


@pytest.mark.asyncio
async def test_parse_sql_query_integration():
    """Test the integration of parse_sql_query with real components."""
    # Mock AST data
    mock_ast = {"stmt": [{"SelectStmt": {"targetList": []}}]}

    # Create a mock JsonResult
    mock_result = MagicMock()
    mock_result.value = json.dumps(mock_ast)

    # Create a mock SqlParserTool
    mock_parser = MagicMock()
    mock_parser.parse_sql = MagicMock(return_value=mock_result)

    # Patch the required components
    with patch("postgres_mcp.server.SqlParserTool", return_value=mock_parser):
        # Execute the parse_sql_query function
        result = await server.parse_sql_query("SELECT * FROM users")

        # Verify the result
        assert isinstance(result, list)
        assert len(result) == 1
        assert json.loads(result[0].text) == mock_ast
