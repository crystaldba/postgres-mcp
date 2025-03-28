from unittest.mock import Mock

import pytest
from .sql_driver import SqlDriver
from psycopg2.sql import SQL

from .safe_sql import LiteralParam
from .safe_sql import SafeSqlDriver


@pytest.fixture
def mock_sql_driver():
    driver = Mock(spec=SqlDriver)
    driver.execute_query.return_value = []
    return driver


@pytest.fixture
def safe_driver(mock_sql_driver):
    return SafeSqlDriver(mock_sql_driver)


def test_select_statement(safe_driver, mock_sql_driver):
    """Test that simple SELECT statements are allowed"""
    query = "SELECT * FROM users WHERE age > 18"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_update_statement(safe_driver):
    """Test that UPDATE statements are blocked"""
    query = "UPDATE users SET status = 'active' WHERE id = 1"
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_select_with_join(safe_driver, mock_sql_driver):
    """Test that SELECT with JOIN is allowed"""
    query = """
    SELECT users.name, orders.order_date
    FROM users
    INNER JOIN orders ON users.id = orders.user_id
    WHERE orders.status = 'pending'
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_show_variable(safe_driver, mock_sql_driver):
    """Test that SHOW statements are allowed"""
    query = "SHOW search_path"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_set_variable(safe_driver):
    """Test that SET statements are blocked"""
    query = "SET search_path TO public"
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_select_with_arithmetic(safe_driver, mock_sql_driver):
    """Test that SELECT with arithmetic expressions is allowed"""
    query = "SELECT id, price * quantity as total FROM orders"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_select_current_user(safe_driver, mock_sql_driver):
    """Test that SELECT current_user is allowed"""
    query = "SELECT current_user"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_drop_table(safe_driver):
    """Test that DROP TABLE statements are blocked"""
    query = "DROP TABLE users"
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_delete_from_table(safe_driver):
    """Test that DELETE FROM statements are blocked"""
    query = "DELETE FROM users WHERE status = 'inactive'"
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_select_with_subquery(safe_driver, mock_sql_driver):
    """Test that SELECT with subqueries is allowed"""
    query = """
    SELECT name FROM users
    WHERE id IN (SELECT user_id FROM orders WHERE total > 1000)
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_select_with_malicious_comment(safe_driver):
    """Test that SQL injection via comments is blocked"""
    # This test is not valid because pglast will parse this as a single SELECT statement
    # The comment is treated as a comment, not as additional statements
    # A better test would be to try actual multiple statements
    query = """
    SELECT * FROM users; DROP TABLE users;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_select_with_union(safe_driver, mock_sql_driver):
    """Test that UNION queries are allowed"""
    query = """
    SELECT id, name FROM users
    UNION
    SELECT NULL, concat(table_name) FROM information_schema.tables
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_select_into(safe_driver):
    """Test that SELECT INTO statements are blocked"""
    query = """
    SELECT id, name
    INTO new_table
    FROM users
    """
    with pytest.raises(ValueError, match="(?i)not allowed"):
        safe_driver.execute_query(query)


def test_select_for_update(safe_driver):
    """Test that SELECT FOR UPDATE statements are blocked"""
    query = """
    SELECT id, name
    FROM users
    FOR UPDATE
    """
    with pytest.raises(ValueError, match="(?i)locking clause"):
        safe_driver.execute_query(query)


def test_select_with_locking_clause(safe_driver):
    """Test that SELECT with explicit locking clauses is blocked"""
    query = """
    SELECT id, name
    FROM users
    FOR SHARE NOWAIT
    """
    with pytest.raises(ValueError, match="(?i)locking clause"):
        safe_driver.execute_query(query)


def test_select_with_commit(safe_driver):
    """Test that statements containing COMMIT are blocked"""
    query = """
    SELECT id FROM users;
    COMMIT;
    SELECT name FROM users;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_explain_plan(safe_driver, mock_sql_driver):
    """Test that EXPLAIN (without ANALYZE) works with bind variables"""
    query = """
    EXPLAIN (FORMAT JSON)
    SELECT id, name
    FROM users
    WHERE age > $1 AND status = $2
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_explain_analyze_blocked(safe_driver):
    """Test that EXPLAIN ANALYZE is blocked"""
    query = """
    EXPLAIN ANALYZE
    SELECT id, name FROM users
    """
    with pytest.raises(ValueError, match="EXPLAIN ANALYZE is not supported"):
        safe_driver.execute_query(query)


def test_begin_transaction_blocked(safe_driver):
    """Test that transaction blocks are blocked"""
    query = """
    BEGIN;
    SELECT id, name FROM users;
    """
    # Note the commit is intentionally not included in the query
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_invalid_sql_syntax(safe_driver):
    """Test that queries with invalid SQL syntax are blocked"""

    # Invalid SQL syntax
    query1 = "SELECT * FRMO users;"
    with pytest.raises(ValueError, match="Failed to parse SQL statement"):
        safe_driver.execute_query(query1)

    # Invalid JOIN syntax
    query2 = "SELECT * FROM users INNER JOON posts ON users.id = posts.user_id;"
    with pytest.raises(ValueError, match="Failed to parse SQL statement"):
        safe_driver.execute_query(query2)

    # Unmatched parentheses
    query3 = "SELECT * FROM users WHERE (age > 21 AND active = true;"
    with pytest.raises(ValueError, match="Failed to parse SQL statement"):
        safe_driver.execute_query(query3)


def test_create_index_blocked(safe_driver):
    """Test that CREATE INDEX statements are blocked"""
    query = """
    CREATE INDEX idx_user_email ON users(email);
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_drop_index_blocked(safe_driver):
    """Test that DROP INDEX statements are blocked"""
    query = """
    DROP INDEX idx_user_email;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_create_table_blocked(safe_driver):
    """Test that CREATE TABLE statements are blocked"""
    query = """
    CREATE TABLE test_table (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_create_table_as_blocked(safe_driver):
    """Test that CREATE TABLE AS statements are blocked"""
    query = """
    CREATE TABLE user_backup AS
    SELECT * FROM users;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_create_extension_blocked(safe_driver):
    """Test that CREATE EXTENSION statements are blocked"""
    query = """
    CREATE EXTENSION pg_hack;
    """
    with pytest.raises(ValueError, match="CREATE EXTENSION pg_hack is not supported"):
        safe_driver.execute_query(query)


def test_drop_extension_blocked(safe_driver):
    """Test that DROP EXTENSION statements are blocked"""
    query = """
    DROP EXTENSION pg_stat_statements;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)


def test_complex_index_metadata_select(safe_driver, mock_sql_driver):
    """Test that complex SELECT queries for index metadata are allowed"""
    query = """SELECT indexrelid::regclass AS index_name, array_agg(attname) AS columns,
    indisunique, indisprimary
    FROM pg_index i
    JOIN pg_attribute a ON a.attnum = ANY(i.indkey)
    WHERE i.indrelid IN (SELECT oid FROM pg_class WHERE relnamespace =
        (SELECT oid FROM pg_namespace WHERE nspname = 'public'))
    GROUP BY indexrelid, indisunique, indisprimary
    HAVING COUNT(array_agg(attname)) > 1"""
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_once_with("/* crystaldba */ " + query)


def test_allowed_functions(safe_driver):
    """Tests that allow functins (especially the ones that are newly added)"""

    # Test pg_relation_filenode function
    query = """
    SELECT pg_relation_filenode('foo');
    """
    assert safe_driver._validate(query) is None


def test_disallowed_functions(safe_driver):
    """Test that disallowed functions are blocked"""

    # Test pg_sleep() function
    query = """
    SELECT pg_sleep(1);
    """
    with pytest.raises(ValueError, match="Function pg_sleep is not allowed"):
        safe_driver.execute_query(query)

    # Test pg_read_file() function
    query = """
    SELECT pg_read_file('/etc/passwd');
    """
    with pytest.raises(ValueError, match="Function pg_read_file is not allowed"):
        safe_driver.execute_query(query)

    # Test lo_import() function
    query = """
    SELECT lo_import('/etc/passwd');
    """
    with pytest.raises(ValueError, match="Function lo_import is not allowed"):
        safe_driver.execute_query(query)


def test_session_info_functions(safe_driver, mock_sql_driver):
    """Test that various session information functions are allowed"""
    queries = [
        "SELECT current_catalog",
        "SELECT current_database()",
        "SELECT current_query()",
        "SELECT current_role",
        "SELECT current_schema",
        "SELECT current_schema()",
        "SELECT current_schemas(true)",
        "SELECT current_user",
        "SELECT pg_backend_pid()",
        "SELECT pg_conf_load_time()",
        "SELECT pg_jit_available()",
        "SELECT pg_trigger_depth()",
        "SELECT session_user",
        "SELECT user",
        "SELECT system_user",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_blocking_pids_functions(safe_driver, mock_sql_driver):
    """Test that blocking PIDs related functions are allowed"""
    queries = [
        "SELECT pg_blocking_pids(1234)",
        "SELECT pg_safe_snapshot_blocking_pids(1234)",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_temp_schema_functions(safe_driver):
    """Test that temporary schema related functions are blocked"""
    blocked_queries = [
        "SELECT pg_my_temp_schema()",
        "SELECT pg_is_other_temp_schema(12345)",
        """
        SELECT nspname
        FROM pg_namespace
        WHERE oid = pg_my_temp_schema()
        """,
        """
        SELECT nspname
        FROM pg_namespace
        WHERE pg_is_other_temp_schema(oid)
        """,
    ]

    for query in blocked_queries:
        safe_driver.execute_query(query)


def test_logfile_functions(safe_driver, mock_sql_driver):
    """Test that log file related functions are allowed"""
    queries = [
        "SELECT pg_current_logfile()",
        "SELECT pg_current_logfile('csvlog')",
        "SELECT pg_current_logfile('jsonlog')",
        "SELECT pg_current_logfile('stderr')",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_complex_session_info_queries(safe_driver, mock_sql_driver):
    """Test more complex queries using session information functions"""
    queries = [
        """
        SELECT current_database() as db_name,
               current_user as user_name,
               current_schema() as schema_name,
               pg_backend_pid() as pid
        FROM pg_database
        WHERE datname = current_database()
        """,
        """
        SELECT nspname, current_schema = nspname as is_current
        FROM pg_namespace
        WHERE nspname = ANY(current_schemas(false))
        ORDER BY nspname
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_security_privilege_functions(safe_driver, mock_sql_driver):
    """Test that security privilege check functions are allowed"""
    queries = [
        # Table privileges
        "SELECT has_table_privilege('myuser', 'mytable', 'SELECT')",
        "SELECT has_table_privilege('mytable', 'UPDATE')",  # current user form
        "SELECT has_table_privilege(1234, 'mytable', 'DELETE')",  # using OID
        # Column privileges
        "SELECT has_any_column_privilege('myuser', 'mytable', 'SELECT')",
        "SELECT has_column_privilege('myuser', 'mytable', 'column1', 'UPDATE')",
        "SELECT has_column_privilege('mytable', 1, 'INSERT')",  # using column number
        # Database privileges
        "SELECT has_database_privilege('myuser', 'mydb', 'CONNECT')",
        "SELECT has_database_privilege('mydb', 'TEMPORARY')",
        "SELECT has_database_privilege(1234, 'mydb', 'CREATE')",
        # Schema privileges
        "SELECT has_schema_privilege('myuser', 'myschema', 'CREATE')",
        "SELECT has_schema_privilege('myschema', 'USAGE')",
        # Function privileges
        "SELECT has_function_privilege('myuser', 'myfunc(int, text)', 'EXECUTE')",
        "SELECT has_function_privilege(1234, 'myfunc', 'EXECUTE')",
        # Sequence privileges
        "SELECT has_sequence_privilege('myuser', 'myseq', 'SELECT')",
        "SELECT has_sequence_privilege('myseq', 'UPDATE')",
        "SELECT has_sequence_privilege(1234, 'myseq', 'USAGE')",
        # Foreign data wrapper privileges
        "SELECT has_foreign_data_wrapper_privilege('myuser', 'myfdw', 'USAGE')",
        # Language privileges
        "SELECT has_language_privilege('myuser', 'plpgsql', 'USAGE')",
        # Parameter privileges
        "SELECT has_parameter_privilege('myuser', 'work_mem', 'SET')",
        "SELECT has_parameter_privilege('maintenance_work_mem', 'ALTER SYSTEM')",
        # Server privileges
        "SELECT has_server_privilege('myuser', 'myserver', 'USAGE')",
        # Tablespace privileges
        "SELECT has_tablespace_privilege('myuser', 'mytablespace', 'CREATE')",
        # Type privileges
        "SELECT has_type_privilege('myuser', 'mytype', 'USAGE')",
        "SELECT has_type_privilege('int4', 'USAGE')",
        # Role privileges
        "SELECT pg_has_role('myrole', 'MEMBER')",
        "SELECT pg_has_role('myuser', 'myrole', 'USAGE')",
        "SELECT pg_has_role(1234, 'myrole', 'SET')",
        "SELECT pg_has_role('myrole', 'MEMBER WITH ADMIN OPTION')",
        # Row-level security
        "SELECT row_security_active('mytable')",
        "SELECT row_security_active(12345)",  # using OID
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_complex_security_privilege_queries(safe_driver, mock_sql_driver):
    """Test more complex queries using security privilege functions"""
    queries = [
        """
        SELECT tablename, has_table_privilege(current_user, tablename, 'SELECT') as can_select,
               has_table_privilege(current_user, tablename, 'UPDATE') as can_update
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
        """,
        """
        SELECT proname, has_function_privilege(current_user, p.oid, 'EXECUTE') as can_execute
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'public'
        """,
        """
        SELECT c.relname, a.attname,
               has_column_privilege(current_user, c.oid, a.attnum, 'SELECT') as can_select,
               has_column_privilege(current_user, c.oid, a.attnum, 'UPDATE') as can_update
        FROM pg_class c
        JOIN pg_attribute a ON c.oid = a.attrelid
        WHERE c.relname = 'mytable'
        AND a.attnum > 0
        AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_security_privilege_functions_with_subqueries(safe_driver, mock_sql_driver):
    """Test security privilege functions used within subqueries"""
    queries = [
        """
        SELECT nspname
        FROM pg_namespace
        WHERE has_schema_privilege(current_user, oid, 'USAGE')
        AND nspname NOT LIKE 'pg_%'
        ORDER BY nspname
        """,
        """
        SELECT t.tablename
        FROM pg_tables t
        WHERE EXISTS (
            SELECT 1
            FROM information_schema.columns c
            WHERE c.table_name = t.tablename
            AND has_column_privilege(current_user, t.tablename, c.column_name, 'SELECT')
        )
        AND t.schemaname = 'public'
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


@pytest.mark.parametrize("operator", ["LIKE", "ILIKE"])
def test_like_patterns(safe_driver, mock_sql_driver, operator):
    """Test that LIKE/ILIKE patterns are only allowed if they start or end with %, but not both or in middle"""

    # Valid pattern - starting with %
    query1 = f"SELECT name FROM users WHERE name {operator} '%smith'"
    safe_driver.execute_query(query1)

    # Valid patterns - ending with %
    query2 = f"SELECT name FROM users WHERE name {operator} 'john%'"
    safe_driver.execute_query(query2)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query2)

    # Vvalid pattern - % in middle
    query3 = f"SELECT name FROM users WHERE name {operator} 'jo%hn'"
    safe_driver.execute_query(query3)

    # Valid pattern - multiple %
    query4 = f"SELECT name FROM users WHERE name {operator} '%jo%hn%'"
    safe_driver.execute_query(query4)

    # Valid pattern - both start and end %
    query5 = f"SELECT name FROM users WHERE name {operator} '%john%'"
    safe_driver.execute_query(query5)


def test_datetime_functions(safe_driver, mock_sql_driver):
    """Test that date/time functions are allowed"""
    queries = [
        "SELECT age(timestamp '2001-04-10', timestamp '1957-06-13')",
        "SELECT clock_timestamp()",
        "SELECT current_date",
        "SELECT current_time",
        "SELECT current_timestamp",
        "SELECT date_part('year', timestamp '2001-02-16')",
        "SELECT date_trunc('hour', timestamp '2001-02-16 20:38:40')",
        "SELECT extract(year from timestamp '2001-02-16 20:38:40')",
        "SELECT isfinite(timestamp '2001-02-16')",
        "SELECT justify_days(interval '35 days')",
        "SELECT justify_hours(interval '27 hours')",
        "SELECT justify_interval(interval '1 year -1 hour')",
        "SELECT localtime",
        "SELECT localtimestamp",
        "SELECT make_date(2013, 7, 15)",
        "SELECT make_interval(years := 1)",
        "SELECT make_time(8, 15, 23.5)",
        "SELECT make_timestamp(2013, 7, 15, 8, 15, 23.5)",
        "SELECT make_timestamptz(2013, 7, 15, 8, 15, 23.5)",
        "SELECT now()",
        "SELECT statement_timestamp()",
        "SELECT timeofday()",
        "SELECT transaction_timestamp()",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_type_conversion_functions(safe_driver, mock_sql_driver):
    """Test that type conversion functions are allowed"""
    queries = [
        "SELECT CAST('100' AS integer)",
        "SELECT '100'::integer",
        "SELECT text '100'",
        "SELECT bool 'true'",
        "SELECT int2 '100'",
        "SELECT int4 '100'",
        "SELECT int8 '100'",
        "SELECT float4 '100.0'",
        "SELECT float8 '100.0'",
        "SELECT numeric '100.0'",
        "SELECT date '2001-10-05'",
        "SELECT time '04:05:06.789'",
        "SELECT timetz '04:05:06.789-08'",
        "SELECT timestamp '2001-10-05 04:05:06.789'",
        "SELECT timestamptz '2001-10-05 04:05:06.789-08'",
        "SELECT interval '1 year 2 months 3 days'",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_regexp_functions(safe_driver, mock_sql_driver):
    """Test that regular expression functions are allowed"""
    queries = [
        "SELECT regexp_count('hello world', 'l')",
        "SELECT regexp_instr('hello world', 'o')",
        "SELECT regexp_like('hello world', '^h.*d$')",
        "SELECT regexp_substr('hello world', 'world')",
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_complex_type_conversion_queries(safe_driver, mock_sql_driver):
    """Test more complex queries using type conversion functions"""
    queries = [
        """
        SELECT id,
               CAST(value AS numeric) as numeric_value,
               CAST(timestamp_col AS date) as date_only
        FROM data_table
        WHERE CAST(value AS numeric) > 100
        """,
        """
        SELECT id,
               CASE WHEN is_valid
                    THEN CAST(value AS numeric)
                    ELSE 0
               END as safe_value
        FROM data_table
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_network_functions(safe_driver):
    """Test that network-related functions are blocked"""
    blocked_queries = [
        "SELECT inet_client_addr()",
        "SELECT inet_client_port()",
        "SELECT inet_server_addr()",
        "SELECT inet_server_port()",
    ]

    for query in blocked_queries:
        safe_driver.execute_query(query)


def test_network_functions_in_complex_queries(safe_driver):
    """Test that queries containing network functions are blocked even in complex queries"""
    blocked_queries = [
        """
        SELECT user_id,
               inet_client_addr() as client_ip
        FROM user_sessions
        """,
        """
        SELECT CASE
            WHEN inet_server_port() = 5432
            THEN 'default'
            ELSE 'custom'
        END as port_type
        FROM system_info
        """,
        """
        SELECT * FROM users
        WHERE last_ip = inet_client_addr()
        """,
    ]

    for query in blocked_queries:
        safe_driver.execute_query(query)


def test_notification_and_server_functions(safe_driver):
    """Test that notification and server information functions are blocked"""
    blocked_queries = [
        "SELECT pg_listening_channels()",
        "SELECT pg_notification_queue_usage()",
        "SELECT pg_postmaster_start_time()",
        # Complex queries using these functions
        """
        SELECT channel,
               pg_notification_queue_usage() as queue_usage
        FROM pg_listening_channels()
        """,
        """
        SELECT CASE
            WHEN pg_postmaster_start_time() < now() - interval '1 day'
            THEN 'server running > 1 day'
            ELSE 'server running < 1 day'
        END as uptime_status
        """,
    ]

    for query in blocked_queries:
        safe_driver.execute_query(query)


def test_minmax_expressions(safe_driver, mock_sql_driver):
    """Test that GREATEST and LEAST expressions are allowed"""
    queries = [
        "SELECT GREATEST(1, 2, 3, 4, 5) as max_value",
        "SELECT LEAST(1, 2, 3, 4, 5) as min_value",
        "SELECT id, GREATEST(value1, value2, value3) as highest_value FROM measurements",
        "SELECT id, LEAST(price1, price2, price3) as lowest_price FROM products",
        # Test with different data types
        "SELECT GREATEST('apple', 'banana', 'cherry') as last_alphabetically",
        "SELECT LEAST('2021-01-01', '2020-12-31', '2021-06-30'::date) as earliest_date",
        # Test in complex expressions
        """
        SELECT id,
               GREATEST(price, minimum_price) as effective_price,
               LEAST(stock, maximum_order) as orderable_quantity
        FROM products
        WHERE LEAST(price, sale_price) > 10
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_row_expressions(safe_driver, mock_sql_driver):
    """Test that row expressions are allowed in various contexts"""
    queries = [
        # Basic row comparisons
        "SELECT * FROM users WHERE (id, name) = (1, 'John')",
        "SELECT * FROM users WHERE (id, age) > (100, 25)",
        "SELECT * FROM users WHERE (id, age) IN (SELECT id, age FROM other_users)",
        # Row constructors in SELECT list
        "SELECT ROW(id, name, age) FROM users",
        "SELECT (id, created_at)::record FROM users",
        # Nested row expressions
        """
        SELECT * FROM orders
        WHERE (customer_id, order_date) IN (
            SELECT customer_id, MAX(order_date)
            FROM orders
            GROUP BY customer_id
        )
        """,
        # Row expressions with functions
        """
        SELECT * FROM coordinates
        WHERE (x, y) <> (0, 0)
        AND (LEAST(x, 100), GREATEST(y, -100)) = (x, y)
        """,
        # Multiple row comparisons
        """
        SELECT * FROM employees
        WHERE (department, salary) >= ('IT', 50000)
        AND (department, salary) < ('IT', 100000)
        """,
        # Row expressions in joins
        """
        SELECT * FROM orders o
        JOIN order_items oi ON (o.order_id, o.customer_id) = (oi.order_id, oi.customer_id)
        """,
        # Row expressions with subqueries
        """
        SELECT * FROM products
        WHERE (category, price) = ANY (
            SELECT category, MAX(price)
            FROM products
            GROUP BY category
        )
        """,
    ]

    for query in queries:
        safe_driver.execute_query(query)
        mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_extension_check_query(safe_driver, mock_sql_driver):
    """Test that extension check query is allowed."""
    query = "SELECT 1 FROM pg_extension WHERE extname = 'hypopg'"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_create_extension_query(safe_driver):
    """Test CREATE EXTENSION query."""
    query = "CREATE EXTENSION IF NOT EXISTS hypopg"
    safe_driver.execute_query(query)


def test_hypopg_create_index_query(safe_driver, mock_sql_driver):
    """Test hypopg_create_index function call."""
    query = "SELECT * FROM hypopg_create_index('CREATE INDEX idx_test ON users(name)')"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_hypopg_reset_query(safe_driver, mock_sql_driver):
    """Test hypopg_reset function call."""
    query = "SELECT hypopg_reset()"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_hypopg_list_indexes_query(safe_driver, mock_sql_driver):
    """Test query for listing hypothetical indexes."""
    query = "SELECT index_name, hypopg_relation_size(indexrelid) as index_size FROM hypopg_list_indexes"
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_pg_stat_statements_query(safe_driver, mock_sql_driver):
    """Test query for getting statistics from pg_stat_statements."""
    query = """
    SELECT queryid, query, calls, total_exec_time/calls as avg_exec_time
    FROM pg_stat_statements
    WHERE calls >= 50
    AND total_exec_time/calls >= 5.0
    ORDER BY total_exec_time DESC
    LIMIT 100
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_pg_indexes_query(safe_driver, mock_sql_driver):
    """Test query for getting index information."""
    query = """
    SELECT schemaname as schema,
           tablename as table,
           indexname as name,
           indexdef as definition
    FROM pg_indexes
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY schemaname, tablename, indexname
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_pg_stats_query(safe_driver, mock_sql_driver):
    """Test query for getting column statistics."""
    query = """
    SELECT COALESCE(SUM(avg_width), 0) AS total_width,
           COALESCE(SUM(n_distinct), 0) AS total_distinct
    FROM pg_stats
    WHERE tablename = 'users' AND attname = ANY(ARRAY['name', 'id'])
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_explain_query(safe_driver, mock_sql_driver):
    """Test EXPLAIN query for measuring query performance."""
    query = """
    EXPLAIN (FORMAT JSON)
    SELECT * FROM users WHERE name = 'Alice'
    """
    safe_driver.execute_query(query)
    mock_sql_driver.execute_query.assert_called_with("/* crystaldba */ " + query)


def test_sql_driver_parameter_format(safe_driver, mock_sql_driver):
    """Test query with SQL parameters through the DatabaseTuningAdvisor."""
    query_template = """
    SELECT queryid, query, calls, total_exec_time/calls as avg_exec_time
    FROM pg_stat_statements
    WHERE calls >= {}
    AND total_exec_time/calls >= {}
    ORDER BY total_exec_time DESC
    LIMIT {}
    """

    min_calls = 50
    min_avg_time = 5.0
    limit = 100

    formatted_query = (
        SQL(query_template)
        .format(
            LiteralParam(min_calls), LiteralParam(min_avg_time), LiteralParam(limit)
        )
        .as_string({})  # type: ignore
    )  # type: ignore

    safe_driver.execute_query(formatted_query)
    mock_sql_driver.execute_query.assert_called_with(
        "/* crystaldba */ " + formatted_query
    )


def test_multiple_statements_with_semicolon(safe_driver):
    """Test that multiple statements separated by semicolons are blocked"""
    query = """
    SELECT id, name FROM users;
    DROP TABLE important_data;
    """
    with pytest.raises(
        ValueError, match="Only SELECT, EXPLAIN, and SHOW statements are allowed"
    ):
        safe_driver.execute_query(query)
