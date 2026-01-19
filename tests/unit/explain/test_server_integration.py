import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import pytest_asyncio

from postgres_mcp.artifacts import ExplainPlanArtifact
from postgres_mcp.server import explain_query


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


class MockCell:
    def __init__(self, data):
        self.cells = data


class MockExplainPlanArtifact(ExplainPlanArtifact):
    """Mock ExplainPlanArtifact that inherits from the real class."""

    def __init__(self, plan_data):
        self.plan_data = plan_data
        # Don't call super().__init__() to avoid validation

    def to_text(self):
        return json.dumps(self.plan_data)


@pytest.mark.asyncio
async def test_explain_query_integration():
    """Test the entire explain_query tool end-to-end."""
    # Mock response with format_text_response
    result_text = json.dumps({"Plan": {"Node Type": "Seq Scan"}})
    mock_text_result = MagicMock()
    mock_text_result.text = result_text

    # Create mock ExplainPlanArtifact
    mock_artifact = MockExplainPlanArtifact({"Plan": {"Node Type": "Seq Scan"}})

    # Create a mock sql_driver with execute_query method
    mock_sql_driver = MagicMock()
    mock_sql_driver.execute_query = AsyncMock(return_value=[MockCell({"server_version": "16.2"})])

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain = AsyncMock(return_value=mock_artifact)

    # Patch the format_text_response function
    with patch("postgres_mcp.server.format_text_response", return_value=[mock_text_result]):
        # Patch the sql_driver_module.get_sql_driver to return our mock sql_driver
        with patch("postgres_mcp.server.sql_driver_module.get_sql_driver", AsyncMock(return_value=mock_sql_driver)):
            # Patch the ExplainPlanTool constructor to return our mock tool
            with patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool):
                # Patch SafeSqlDriver.execute_param_query to avoid validation errors
                with patch("postgres_mcp.sql.safe_sql.SafeSqlDriver.execute_param_query", AsyncMock(return_value=[])):
                    # Pass empty list instead of None
                    result = await explain_query("SELECT * FROM users", analyze=False, hypothetical_indexes=[])

                    # Verify result matches our expected plan data
                    assert isinstance(result, list)
                    assert len(result) == 1
                    assert result[0].text == result_text


@pytest.mark.asyncio
async def test_explain_query_with_analyze_integration():
    """Test the explain_query tool with analyze=True."""
    # Mock response with format_text_response
    result_text = json.dumps({"Plan": {"Node Type": "Seq Scan"}, "Execution Time": 1.23})
    mock_text_result = MagicMock()
    mock_text_result.text = result_text

    # Create mock ExplainPlanArtifact
    mock_artifact = MockExplainPlanArtifact({"Plan": {"Node Type": "Seq Scan"}, "Execution Time": 1.23})

    # Create a mock sql_driver with execute_query method
    mock_sql_driver = MagicMock()
    mock_sql_driver.execute_query = AsyncMock(return_value=[MockCell({"server_version": "16.2"})])

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain_analyze = AsyncMock(return_value=mock_artifact)

    # Patch the format_text_response function
    with patch("postgres_mcp.server.format_text_response", return_value=[mock_text_result]):
        # Patch the sql_driver_module.get_sql_driver to return our mock sql_driver
        with patch("postgres_mcp.server.sql_driver_module.get_sql_driver", AsyncMock(return_value=mock_sql_driver)):
            # Patch the ExplainPlanTool constructor to return our mock tool
            with patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool):
                # Patch SafeSqlDriver.execute_param_query to avoid validation errors
                with patch("postgres_mcp.sql.safe_sql.SafeSqlDriver.execute_param_query", AsyncMock(return_value=[])):
                    # Pass empty list instead of None
                    result = await explain_query("SELECT * FROM users", analyze=True, hypothetical_indexes=[])

                    # Verify result matches our expected plan data
                    assert isinstance(result, list)
                    assert len(result) == 1
                    assert result[0].text == result_text


@pytest.mark.asyncio
async def test_explain_query_with_hypothetical_indexes_integration():
    """Test the explain_query tool with hypothetical indexes."""
    # Mock response with format_text_response
    result_text = json.dumps({"Plan": {"Node Type": "Index Scan"}})
    mock_text_result = MagicMock()
    mock_text_result.text = result_text

    # Test data
    test_sql = "SELECT * FROM users WHERE email = 'test@example.com'"
    test_indexes = [{"table": "users", "columns": ["email"]}]

    # Create mock ExplainPlanArtifact
    mock_artifact = MockExplainPlanArtifact({"Plan": {"Node Type": "Index Scan"}})

    # Create mock SafeSqlDriver that returns extension exists
    mock_safe_driver = MagicMock()
    mock_execute_query = AsyncMock(return_value=[MockCell({"exists": 1})])
    mock_safe_driver.execute_query = mock_execute_query
    # Also need to mock the execute_query for get_postgres_version
    mock_safe_driver.execute_query = AsyncMock(
        side_effect=[
            [MockCell({"server_version": "16.2"})],  # For get_postgres_version
            [MockCell({"exists": 1})],  # For check_extension
        ]
    )

    # Create a mock ExplainPlanTool
    mock_explain_tool = MagicMock()
    mock_explain_tool.explain_with_hypothetical_indexes = AsyncMock(return_value=mock_artifact)

    # Mock check_hypopg_installation_status to return True
    with patch("postgres_mcp.server.check_hypopg_installation_status", AsyncMock(return_value=(True, ""))):
        # Patch the format_text_response function
        with patch("postgres_mcp.server.format_text_response", return_value=[mock_text_result]):
            # Patch the sql_driver_module.get_sql_driver to return our mock sql_driver
            with patch("postgres_mcp.server.sql_driver_module.get_sql_driver", AsyncMock(return_value=mock_safe_driver)):
                # Patch the ExplainPlanTool constructor to return our mock tool
                with patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool):
                    # Patch SafeSqlDriver.execute_param_query to avoid validation errors
                    with patch("postgres_mcp.sql.safe_sql.SafeSqlDriver.execute_param_query", AsyncMock(return_value=[])):
                        # Explicitly pass analyze=False
                        result = await explain_query(test_sql, analyze=False, hypothetical_indexes=test_indexes)

                        # Verify result matches our expected plan data
                        assert isinstance(result, list)
                        assert len(result) == 1
                        assert result[0].text == result_text


@pytest.mark.asyncio
async def test_explain_query_missing_hypopg_integration():
    """Test the explain_query tool when hypopg extension is missing."""
    # Mock message about missing extension
    missing_ext_message = "hypopg extension is required"
    mock_text_result = MagicMock()
    mock_text_result.text = missing_ext_message

    # Test data
    test_sql = "SELECT * FROM users WHERE email = 'test@example.com'"
    test_indexes = [{"table": "users", "columns": ["email"]}]

    # Create mock SafeSqlDriver that returns empty result (extension not exists)
    mock_safe_driver = MagicMock()
    # We need to mock execute_query for both get_postgres_version and check_extension
    mock_safe_driver.execute_query = AsyncMock(
        side_effect=[
            [MockCell({"server_version": "16.2"})],  # For get_postgres_version
            [],  # For check_extension (pg_extension query)
            [],  # For check_extension (pg_available_extensions query)
        ]
    )

    # Create a mock ExplainPlanTool (it shouldn't be called in this case)
    mock_explain_tool = MagicMock()

    # Mock check_hypopg_installation_status to return False with message
    with patch("postgres_mcp.server.check_hypopg_installation_status", AsyncMock(return_value=(False, missing_ext_message))):
        # Patch the format_text_response function
        with patch("postgres_mcp.server.format_text_response", return_value=[mock_text_result]):
            # Patch the sql_driver_module.get_sql_driver to return our mock sql_driver
            with patch("postgres_mcp.server.sql_driver_module.get_sql_driver", AsyncMock(return_value=mock_safe_driver)):
                # Patch the ExplainPlanTool constructor to return our mock tool
                with patch("postgres_mcp.server.ExplainPlanTool", return_value=mock_explain_tool):
                    # Patch SafeSqlDriver.execute_param_query to avoid validation errors
                    with patch("postgres_mcp.sql.safe_sql.SafeSqlDriver.execute_param_query", AsyncMock(return_value=[])):
                        # Explicitly pass analyze=False
                        result = await explain_query(test_sql, analyze=False, hypothetical_indexes=test_indexes)

                        # Verify result
                        assert isinstance(result, list)
                        assert len(result) == 1
                        assert "hypopg" in result[0].text.lower() or "extension" in result[0].text.lower()


@pytest.mark.asyncio
async def test_explain_query_error_handling_integration():
    """Test the explain_query tool's error handling."""
    # Mock error response
    error_message = "Error executing query"
    mock_text_result = MagicMock()
    mock_text_result.text = f"Error: {error_message}"

    # Patch the format_error_response function
    with patch("postgres_mcp.server.format_error_response", return_value=[mock_text_result]):
        # Patch the sql_driver_module.get_sql_driver to throw an exception
        with patch(
            "postgres_mcp.server.sql_driver_module.get_sql_driver",
            side_effect=Exception(error_message),
        ):
            result = await explain_query("INVALID SQL", analyze=False, hypothetical_indexes=[])

            # Verify error is correctly formatted
            assert isinstance(result, list)
            assert len(result) == 1
            assert error_message in result[0].text
