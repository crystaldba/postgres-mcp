from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
import pytest_asyncio

from postgres_mcp.sql import DmlOnlySqlDriver
from postgres_mcp.sql import SqlDriver


@pytest_asyncio.fixture
async def mock_sql_driver():
    driver = Mock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    return driver


@pytest_asyncio.fixture
async def dml_only_driver(mock_sql_driver):
    return DmlOnlySqlDriver(mock_sql_driver)


# ========================================
# Test Allowed DML Operations
# ========================================


@pytest.mark.asyncio
async def test_insert_statement(dml_only_driver, mock_sql_driver):
    """Test that INSERT statements are allowed"""
    query = "INSERT INTO users (name, email) VALUES ('John Doe', 'john@example.com')"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_update_statement(dml_only_driver, mock_sql_driver):
    """Test that UPDATE statements are allowed"""
    query = "UPDATE users SET status = 'active' WHERE id = 1"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_delete_statement(dml_only_driver, mock_sql_driver):
    """Test that DELETE statements are allowed"""
    query = "DELETE FROM users WHERE status = 'inactive'"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_insert_on_conflict(dml_only_driver, mock_sql_driver):
    """Test that INSERT ... ON CONFLICT (UPSERT) statements are allowed"""
    query = """
    INSERT INTO users (id, name, email)
    VALUES (1, 'John Doe', 'john@example.com')
    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_select_statement(dml_only_driver, mock_sql_driver):
    """Test that SELECT statements are allowed"""
    query = "SELECT * FROM users WHERE age > 18"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_insert_with_select(dml_only_driver, mock_sql_driver):
    """Test that INSERT ... SELECT statements are allowed"""
    query = """
    INSERT INTO user_backup (id, name, email)
    SELECT id, name, email FROM users WHERE status = 'active'
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_update_with_join(dml_only_driver, mock_sql_driver):
    """Test that UPDATE with JOIN is allowed"""
    query = """
    UPDATE users u
    SET status = 'premium'
    FROM orders o
    WHERE u.id = o.user_id AND o.total > 1000
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_delete_with_subquery(dml_only_driver, mock_sql_driver):
    """Test that DELETE with subquery is allowed"""
    query = """
    DELETE FROM users
    WHERE id IN (SELECT user_id FROM orders WHERE status = 'cancelled')
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_insert_multiple_rows(dml_only_driver, mock_sql_driver):
    """Test that INSERT with multiple rows is allowed"""
    query = """
    INSERT INTO users (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob', 'bob@example.com'),
    ('Charlie', 'charlie@example.com')
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_update_with_returning(dml_only_driver, mock_sql_driver):
    """Test that UPDATE with RETURNING clause is allowed"""
    query = """
    UPDATE users SET status = 'active' WHERE id = 1 RETURNING id, name, status
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_delete_with_returning(dml_only_driver, mock_sql_driver):
    """Test that DELETE with RETURNING clause is allowed"""
    query = """
    DELETE FROM users WHERE status = 'inactive' RETURNING id, name
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_show_variable(dml_only_driver, mock_sql_driver):
    """Test that SHOW statements are allowed"""
    query = "SHOW search_path"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_explain_query(dml_only_driver, mock_sql_driver):
    """Test that EXPLAIN statements are allowed"""
    query = "EXPLAIN SELECT * FROM users WHERE age > 18"
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


# ========================================
# Test Blocked DDL Operations
# ========================================


@pytest.mark.asyncio
async def test_create_table_blocked(dml_only_driver):
    """Test that CREATE TABLE statements are blocked"""
    query = """
    CREATE TABLE test_table (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_alter_table_blocked(dml_only_driver):
    """Test that ALTER TABLE statements are blocked"""
    query = "ALTER TABLE users ADD COLUMN age INTEGER"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_table_blocked(dml_only_driver):
    """Test that DROP TABLE statements are blocked"""
    query = "DROP TABLE users"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_index_blocked(dml_only_driver):
    """Test that CREATE INDEX statements are blocked"""
    query = "CREATE INDEX idx_user_email ON users(email)"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_index_blocked(dml_only_driver):
    """Test that DROP INDEX statements are blocked"""
    query = "DROP INDEX idx_user_email"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_truncate_blocked(dml_only_driver):
    """Test that TRUNCATE statements are blocked"""
    query = "TRUNCATE TABLE users"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_extension_blocked(dml_only_driver):
    """Test that CREATE EXTENSION statements are blocked"""
    query = "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_extension_blocked(dml_only_driver):
    """Test that DROP EXTENSION statements are blocked"""
    query = "DROP EXTENSION pg_trgm"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_vacuum_blocked(dml_only_driver):
    """Test that VACUUM statements are blocked"""
    query = "VACUUM users"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_schema_blocked(dml_only_driver):
    """Test that CREATE SCHEMA statements are blocked"""
    query = "CREATE SCHEMA test_schema"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_schema_blocked(dml_only_driver):
    """Test that DROP SCHEMA statements are blocked"""
    query = "DROP SCHEMA test_schema"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_database_blocked(dml_only_driver):
    """Test that CREATE DATABASE statements are blocked"""
    query = "CREATE DATABASE test_db"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_database_blocked(dml_only_driver):
    """Test that DROP DATABASE statements are blocked"""
    query = "DROP DATABASE test_db"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_view_blocked(dml_only_driver):
    """Test that CREATE VIEW statements are blocked"""
    query = "CREATE VIEW active_users AS SELECT * FROM users WHERE status = 'active'"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_view_blocked(dml_only_driver):
    """Test that DROP VIEW statements are blocked"""
    query = "DROP VIEW active_users"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_create_function_blocked(dml_only_driver):
    """Test that CREATE FUNCTION statements are blocked"""
    query = """
    CREATE FUNCTION get_user_count() RETURNS INTEGER AS $$
    BEGIN
        RETURN (SELECT COUNT(*) FROM users);
    END;
    $$ LANGUAGE plpgsql
    """
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_drop_function_blocked(dml_only_driver):
    """Test that DROP FUNCTION statements are blocked"""
    query = "DROP FUNCTION get_user_count()"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


# ========================================
# Test Complex DML Queries
# ========================================


@pytest.mark.asyncio
async def test_cte_with_insert(dml_only_driver, mock_sql_driver):
    """Test that CTEs with INSERT are allowed"""
    query = """
    WITH new_users AS (
        SELECT 'Alice' as name, 'alice@example.com' as email
        UNION ALL
        SELECT 'Bob' as name, 'bob@example.com' as email
    )
    INSERT INTO users (name, email)
    SELECT name, email FROM new_users
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_cte_with_update(dml_only_driver, mock_sql_driver):
    """Test that CTEs with UPDATE are allowed"""
    query = """
    WITH premium_users AS (
        SELECT user_id FROM orders
        GROUP BY user_id
        HAVING SUM(total) > 10000
    )
    UPDATE users SET status = 'premium'
    WHERE id IN (SELECT user_id FROM premium_users)
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_cte_with_delete(dml_only_driver, mock_sql_driver):
    """Test that CTEs with DELETE are allowed"""
    query = """
    WITH inactive_users AS (
        SELECT id FROM users
        WHERE last_login < NOW() - INTERVAL '1 year'
    )
    DELETE FROM users WHERE id IN (SELECT id FROM inactive_users)
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_insert_with_complex_subquery(dml_only_driver, mock_sql_driver):
    """Test that INSERT with complex subquery is allowed"""
    query = """
    INSERT INTO user_stats (user_id, order_count, total_spent)
    SELECT
        u.id,
        COUNT(o.id),
        COALESCE(SUM(o.total), 0)
    FROM users u
    LEFT JOIN orders o ON u.id = o.user_id
    GROUP BY u.id
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_update_with_case(dml_only_driver, mock_sql_driver):
    """Test that UPDATE with CASE expression is allowed"""
    query = """
    UPDATE users
    SET tier = CASE
        WHEN total_orders > 100 THEN 'platinum'
        WHEN total_orders > 50 THEN 'gold'
        WHEN total_orders > 10 THEN 'silver'
        ELSE 'bronze'
    END
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_delete_with_exists(dml_only_driver, mock_sql_driver):
    """Test that DELETE with EXISTS clause is allowed"""
    query = """
    DELETE FROM users u
    WHERE NOT EXISTS (
        SELECT 1 FROM orders o
        WHERE o.user_id = u.id
        AND o.created_at > NOW() - INTERVAL '1 year'
    )
    """
    await dml_only_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


# ========================================
# Test Error Handling
# ========================================


@pytest.mark.asyncio
async def test_invalid_sql_syntax(dml_only_driver):
    """Test that queries with invalid SQL syntax are blocked"""
    query = "INSERT INTO users (name email) VALUES ('John', 'john@example.com')"
    with pytest.raises(ValueError, match="Failed to parse SQL statement"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_sql_injection_attempt(dml_only_driver):
    """Test that SQL injection attempts are blocked"""
    query = """
    SELECT * FROM users; DROP TABLE users;
    """
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_explain_analyze_blocked(dml_only_driver):
    """Test that EXPLAIN ANALYZE is blocked"""
    query = "EXPLAIN ANALYZE SELECT * FROM users"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_set_statement_blocked(dml_only_driver):
    """Test that SET statements are blocked"""
    query = "SET search_path TO public"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_begin_transaction_blocked(dml_only_driver):
    """Test that BEGIN TRANSACTION is blocked"""
    query = "BEGIN"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_commit_blocked(dml_only_driver):
    """Test that COMMIT is blocked"""
    query = "COMMIT"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


@pytest.mark.asyncio
async def test_rollback_blocked(dml_only_driver):
    """Test that ROLLBACK is blocked"""
    query = "ROLLBACK"
    with pytest.raises(ValueError, match="Error validating query"):
        await dml_only_driver.execute_query(query)


# ========================================
# Test Timeout Handling
# ========================================


@pytest.mark.asyncio
async def test_timeout_configuration(mock_sql_driver):
    """Test that timeout is properly configured"""
    driver_with_timeout = DmlOnlySqlDriver(mock_sql_driver, timeout=30)
    assert driver_with_timeout.timeout == 30

    driver_without_timeout = DmlOnlySqlDriver(mock_sql_driver)
    assert driver_without_timeout.timeout is None


# ========================================
# Test Force Readonly Parameter
# ========================================


@pytest.mark.asyncio
async def test_force_readonly_false_by_default(dml_only_driver, mock_sql_driver):
    """Test that force_readonly is False by default for DML operations"""
    query = "INSERT INTO users (name) VALUES ('Test')"
    await dml_only_driver.execute_query(query)
    # Verify that force_readonly=False is passed to the underlying driver
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=False)


@pytest.mark.asyncio
async def test_force_readonly_can_be_set(dml_only_driver, mock_sql_driver):
    """Test that force_readonly can be explicitly set"""
    query = "SELECT * FROM users"
    await dml_only_driver.execute_query(query, force_readonly=True)
    # Verify that force_readonly=True is passed to the underlying driver
    mock_sql_driver.execute_query.assert_awaited_once_with("/* crystaldba */ " + query, params=None, force_readonly=True)
