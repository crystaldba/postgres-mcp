"""End-to-end test for the opt-in GCF response encoding.

Runs the real ``execute_sql`` tool against a real PostgreSQL instance (through the same
global connection pool the server uses) and asserts that, with the GCF response format
selected, the tool emits a Graph Compact Format wire that decodes back to the queried
rows. DB-native column types (numeric, date, boolean) exercise the JSON-scalar
normalization path.
"""

import logging

import pytest
import pytest_asyncio

import postgres_mcp.server as server
from postgres_mcp.server import ResponseFormat
from postgres_mcp.server import execute_sql
from postgres_mcp.sql import SqlDriver

logger = logging.getLogger(__name__)

gcf = pytest.importorskip("gcf", reason="gcf-python optional dependency not installed")

_SEED = """
DROP TABLE IF EXISTS gcf_demo;
CREATE TABLE gcf_demo (
    id int, name text, amount numeric(10, 2), created date, active boolean
);
INSERT INTO gcf_demo
SELECT g, 'row ' || g, (g * 1.5)::numeric(10, 2), DATE '2024-01-01' + g, g % 2 = 0
FROM generate_series(1, 30) g;
"""


@pytest_asyncio.fixture
async def connected_pool(test_postgres_connection_string):
    """Connect the server's global pool to a real database and seed a demo table."""
    connection_string, _version = test_postgres_connection_string
    await server.db_connection.pool_connect(connection_string)
    driver = SqlDriver(conn=server.db_connection)
    for statement in filter(str.strip, _SEED.split(";")):
        await driver.execute_query(statement)
    try:
        yield
    finally:
        await server.db_connection.close()


@pytest.mark.asyncio
async def test_execute_sql_emits_gcf_end_to_end(connected_pool, monkeypatch):
    monkeypatch.setattr(server, "current_response_format", ResponseFormat.GCF)
    result = await execute_sql("SELECT * FROM gcf_demo ORDER BY id")

    assert len(result) == 1
    wire = result[0].text
    assert wire.startswith("GCF profile=generic")
    # Field names factored into exactly one header line.
    assert wire.count("{id,name,amount,created,active}") == 1

    decoded = gcf.decode_generic(wire)
    assert len(decoded) == 30
    assert decoded[0]["id"] == 1
    assert decoded[0]["name"] == "row 1"
    assert decoded[0]["active"] is False
    # numeric/date normalized to JSON scalars, losslessly.
    assert decoded[1]["amount"] == "3.00"
    assert decoded[0]["created"] == "2024-01-02"


@pytest.mark.asyncio
async def test_execute_sql_stays_json_by_default(connected_pool):
    # Default response format: the tool returns the normal text rendering, never GCF.
    result = await execute_sql("SELECT * FROM gcf_demo ORDER BY id")
    text = result[0].text
    assert not text.startswith("GCF profile=generic")
    # Positive check: it is the usual list-of-rows rendering with the queried data.
    assert text.startswith("[")
    assert "'name': 'row 1'" in text
