import pytest
import sqlalchemy

from ..dta.sql_tool import LocalSqlDriver
from .database_health import DatabaseHealthTool


@pytest.fixture
def local_sql_driver(test_postgres_connection_string):
    connection_string, version = test_postgres_connection_string
    # Ensure we use psycopg (v3) not psycopg2 by explicitly setting the dialect
    if connection_string.startswith("postgresql:"):
        connection_string = connection_string.replace(
            "postgresql:", "postgresql+psycopg:"
        )
    return LocalSqlDriver(engine_url=connection_string)


def setup_test_tables(sql_driver):
    with sql_driver.engine.begin() as conn:
        # Drop existing tables if they exist
        conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS test_orders"))
        conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS test_customers"))
        conn.execute(sqlalchemy.text("DROP SEQUENCE IF EXISTS test_seq"))

        # Create test sequence
        conn.execute(sqlalchemy.text("CREATE SEQUENCE test_seq"))

        # Create tables with various features to test health checks
        conn.execute(
            sqlalchemy.text("""
            CREATE TABLE test_customers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        )

        conn.execute(
            sqlalchemy.text("""
            CREATE TABLE test_orders (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES test_customers(id),
                total DECIMAL NOT NULL CHECK (total >= 0),
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        )

        # Create some indexes to test index health
        conn.execute(
            sqlalchemy.text(
                "CREATE INDEX idx_orders_customer ON test_orders(customer_id)"
            )
        )
        conn.execute(
            sqlalchemy.text("CREATE INDEX idx_orders_status ON test_orders(status)")
        )
        conn.execute(
            sqlalchemy.text(
                "CREATE INDEX idx_orders_created ON test_orders(created_at)"
            )
        )
        # Create a duplicate index to test duplicate index detection
        conn.execute(
            sqlalchemy.text(
                "CREATE INDEX idx_orders_customer_dup ON test_orders(customer_id)"
            )
        )

        # Insert some test data
        conn.execute(
            sqlalchemy.text("""
            INSERT INTO test_customers (name, email)
            SELECT
                'Customer ' || i,
                'customer' || i || '@example.com'
            FROM generate_series(1, 100) i
        """)
        )

        conn.execute(
            sqlalchemy.text("""
            INSERT INTO test_orders (customer_id, total, status)
            SELECT
                (random() * 99)::int + 1,  -- Changed to ensure IDs are between 1 and 100
                (random() * 1000)::decimal,
                CASE (random() * 2)::int
                    WHEN 0 THEN 'pending'
                    WHEN 1 THEN 'completed'
                    ELSE 'cancelled'
                END
            FROM generate_series(1, 1000) i
        """)
        )

        # Run ANALYZE to update statistics
        conn.execute(sqlalchemy.text("ANALYZE test_customers"))
        conn.execute(sqlalchemy.text("ANALYZE test_orders"))


def cleanup_test_tables(sql_driver):
    with sql_driver.engine.begin() as conn:
        conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS test_orders"))
        conn.execute(sqlalchemy.text("DROP TABLE IF EXISTS test_customers"))
        conn.execute(sqlalchemy.text("DROP SEQUENCE IF EXISTS test_seq"))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_health_all(local_sql_driver):
    """Test that the database health tool runs without errors when performing all health checks.
    This test only verifies that the tool executes successfully and returns results in the expected format.
    It does not validate whether the health check results are correct."""
    setup_test_tables(local_sql_driver)
    try:
        await local_sql_driver.connect()
        health_tool = DatabaseHealthTool(sql_driver=local_sql_driver)

        # Run health check with type "all"
        result = await health_tool.health(health_type="all")

        # Verify the result
        assert isinstance(result, str)
        health_report = result

        # Check that all health components are present
        assert "Invalid index check:" in health_report
        assert "Duplicate index check:" in health_report
        assert "Index bloat:" in health_report
        assert "Unused index check:" in health_report
        assert "Connection health:" in health_report
        assert "Vacuum health:" in health_report
        assert "Sequence health:" in health_report
        assert "Replication health:" in health_report
        assert "Buffer health for indexes:" in health_report
        assert "Buffer health for tables:" in health_report
        assert "Constraint health:" in health_report

        # Verify specific health issues we know should be detected
        assert (
            "idx_orders_customer_dup" in health_report
        )  # Should detect duplicate index

    finally:
        cleanup_test_tables(local_sql_driver)
