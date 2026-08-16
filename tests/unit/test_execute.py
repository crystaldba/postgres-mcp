"""Tests for execute_sql function with JSON and CSV output formats."""

import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from postgres_mcp.server import execute_sql


class MockRow:
    def __init__(self, cells):
        self.cells = cells


@pytest.mark.asyncio
async def test_execute_sql_json_output():
    """Test execute_sql outputs valid JSON (not Python repr format)."""
    mock_driver = AsyncMock()
    mock_driver.execute_query.return_value = [
        MockRow({"id": 1, "salary": Decimal('50000.00'), "created_at": datetime(2023, 1, 1)})
    ]

    with patch('postgres_mcp.server.get_sql_driver', return_value=mock_driver):
        result = await execute_sql("SELECT * FROM users")

    # Should return valid JSON
    parsed = json.loads(result[0].text)
    assert parsed[0]["salary"] == 50000.0  # Decimal -> float, not repr
    assert parsed[0]["created_at"] == "2023-01-01T00:00:00"  # ISO format


@pytest.mark.asyncio
async def test_execute_sql_csv_output():
    """Test execute_sql outputs CSV format."""
    mock_driver = AsyncMock()
    mock_driver.execute_query.return_value = [
        MockRow({"id": 1, "name": "John", "salary": Decimal('50000.00')})
    ]

    with patch('postgres_mcp.server.get_sql_driver', return_value=mock_driver):
        result = await execute_sql("SELECT * FROM users", output_format="csv")

    lines = result[0].text.strip().split('\n')
    assert len(lines) == 2  # Header + data
    assert "id" in lines[0]
    assert "50000.00" in lines[1]  # Decimal precision preserved