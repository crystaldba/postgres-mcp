import json
import unittest
from logging import getLogger
from typing import Any
from typing import Dict
from typing import Set
from unittest.mock import MagicMock
from unittest.mock import patch

from .artifacts import ExplainPlanArtifact
from .dta_calc import ColumnCollector
from .dta_calc import ConditionColumnCollector
from .dta_calc import DatabaseTuningAdvisor
from .dta_calc import Index
from .dta_calc import IndexConfig
from .dta_calc import parse_sql

logger = getLogger(__name__)


class MockCell:
    def __init__(self, data: Dict[str, Any]):
        self.cells = data


class TestDatabaseTuningAdvisor(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.sql_driver = MagicMock()
        self.dta = DatabaseTuningAdvisor(
            sql_driver=self.sql_driver, budget_mb=10, max_runtime_seconds=60
        )

    def test_extract_columns_empty_query(self):
        query = "SELECT 1"
        columns = self.dta.extract_columns(query)
        self.assertEqual(columns, {})

    def test_extract_columns_invalid_sql(self):
        query = "INVALID SQL"
        columns = self.dta.extract_columns(query)
        self.assertEqual(columns, {})

    def test_extract_columns_subquery(self):
        query = "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE status = 'pending')"
        columns = self.dta.extract_columns(query)
        self.assertEqual(columns, {"users": {"id"}, "orders": {"user_id", "status"}})

    def test_index_initialization(self):
        """Test Index class initialization and properties."""
        idx = Index(
            table="users",
            columns=(
                "name",
                "email",
            ),
        )
        self.assertEqual(idx.table, "users")
        self.assertEqual(idx.columns, ("name", "email"))
        self.assertEqual(
            idx.definition,
            "CREATE INDEX crystaldba_idx_users_name_email_2 ON users USING btree (name, email)",
        )

    def test_index_equality(self):
        """Test Index equality comparison."""
        idx1 = Index(table="users", columns=("name",))
        idx2 = Index(table="users", columns=("name",))
        idx3 = Index(table="users", columns=("email",))

        self.assertEqual(idx1, idx2)
        self.assertNotEqual(idx1, idx3)
        self.assertNotEqual(idx2, idx3)

    def test_extract_columns_from_simple_query(self):
        """Test column extraction from a simple SELECT query."""
        query = "SELECT * FROM users WHERE name = 'Alice' ORDER BY age"
        columns = self.dta.extract_columns(query)

        self.assertEqual(columns, {"users": {"name", "age"}})

    def test_extract_columns_from_join_query(self):
        """Test column extraction from a query with JOINs."""
        query = """
        SELECT u.name, o.order_date
        FROM users u
        JOIN orders o ON u.id = o.user_id
        WHERE o.status = 'pending'
        """
        columns = self.dta.extract_columns(query)
        self.assertEqual(
            columns,
            {"users": {"id", "name"}, "orders": {"user_id", "status", "order_date"}},
        )

    def test_generate_candidates(self):
        """Test index candidate generation."""
        global responses
        responses = [
            # information_schema.columns
            [
                MockCell(
                    {
                        "table_name": "users",
                        "column_name": "name",
                        "data_type": "character varying",
                        "character_maximum_length": 150,
                        "avg_width": 30,
                        "potential_long_text": True,
                    }
                )
            ],
            # create index users.name
            [MockCell({"indexrelid": 123})],
            # pg_stat_statements
            [
                MockCell(
                    {"index_name": "crystaldba_idx_users_name_1", "index_size": 81920}
                )
            ],
            # hypopg_reset
            [],
        ]
        global responses_index
        responses_index = 0

        def mock_execute_query(query):
            global responses_index
            responses_index += 1
            logger.info(
                f"Query: {query}\n    Response: {
                    list(json.dumps(x.cells) for x in responses[responses_index - 1])
                    if responses_index <= len(responses)
                    else None
                }\n--------------------------------------------------------"
            )
            return (
                responses[responses_index - 1]
                if responses_index <= len(responses)
                else None
            )

        self.sql_driver.execute_query.side_effect = mock_execute_query
        q1 = "SELECT * FROM users WHERE name = 'Alice'"
        queries = [(q1, parse_sql(q1)[0].stmt, 1.0)]
        candidates = self.dta.generate_candidates(queries, set())
        self.assertTrue(
            any(c.table == "users" and c.columns == ("name",) for c in candidates)
        )
        self.assertEqual(candidates[0].estimated_size, 10 * 8192)

    def test_analyze_workload(self):
        def mock_execute_query(query):
            logger.info(f"Query: {query}")
            if "pg_stat_statements" in query:
                return [
                    MockCell(
                        {
                            "queryid": 1,
                            "query": "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)",
                            "calls": 100,
                            "avg_exec_time": 10.0,
                        }
                    )
                ]
            elif "EXPLAIN" in query:
                if "COSTS TRUE" in query:
                    return [
                        MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 80.0}}]})
                    ]  # Cost with hypothetical index
                else:
                    return [
                        MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 100.0}}]})
                    ]  # Current cost
            elif "hypopg_reset" in query:
                return None
            elif "FROM information_schema.columns c" in query:
                return [
                    MockCell(
                        {
                            "table_name": "users",
                            "column_name": "id",
                            "data_type": "integer",
                            "character_maximum_length": None,
                            "avg_width": 4,
                            "potential_long_text": False,
                        }
                    ),
                ]
            elif "pg_stats" in query:
                return [
                    MockCell({"total_width": 10, "total_distinct": 100})
                ]  # For index size estimation
            elif "pg_extension" in query:
                return [MockCell({"exists": 1})]
            elif "hypopg_disable_index" in query:
                return None
            elif "hypopg_enable_index" in query:
                return None
            elif "pg_total_relation_size" in query:
                return [MockCell({"rel_size": 100000})]
            return None  # Default response for unrecognized queries

        self.sql_driver.execute_query.side_effect = mock_execute_query
        session = self.dta.analyze_workload(min_calls=50, min_avg_time_ms=5.0)
        logger.debug(f"Recommendations: {session.recommendations}")
        self.assertTrue(
            any(r.table in {"users", "orders"} for r in session.recommendations)
        )

    def test_error_handling(self):
        """Test error handling in critical methods."""
        # Test HypoPG setup failure
        self.sql_driver.execute_query.side_effect = RuntimeError("HypoPG not available")
        with self.assertRaises(RuntimeError):
            DatabaseTuningAdvisor(self.sql_driver)

        # Test invalid query handling
        self.sql_driver.execute_query.side_effect = None
        dta = DatabaseTuningAdvisor(self.sql_driver)

        invalid_query = "INVALID SQL"
        columns = dta.extract_columns(invalid_query)
        self.assertEqual(columns, {})

    def test_index_exists(self):
        """Test the robust index comparison functionality."""
        # Create test cases with various index definition patterns
        test_cases = [
            # Basic case - exact match
            {
                "candidate": Index("users", ("name",)),
                "existing_defs": {
                    "CREATE INDEX crystaldba_idx_users_name_1 ON users USING btree (name)"
                },
                "expected": True,
                "description": "Exact match",
            },
            # Different name but same structure
            {
                "candidate": Index("users", ("id",)),
                "existing_defs": {
                    "CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)"
                },
                "expected": True,
                "description": "Primary key detection",
            },
            # Different schema but same table and columns
            {
                "candidate": Index("users", ("email",)),
                "existing_defs": {
                    "CREATE UNIQUE INDEX users_email_key ON public.users USING btree (email)"
                },
                "expected": True,
                "description": "Schema-qualified match",
            },
            # Multi-column index with different order
            {
                "candidate": Index("orders", ("customer_id", "product_id"), "hash"),
                "existing_defs": {
                    "CREATE INDEX orders_idx ON orders USING hash (product_id, customer_id)"
                },
                "expected": True,
                "description": "Hash index with different column order",
            },
            # Partial match - not enough
            {
                "candidate": Index("products", ("category", "name", "price")),
                "existing_defs": {
                    "CREATE INDEX products_category_idx ON products USING btree (category)"
                },
                "expected": False,
                "description": "Partial coverage - not enough",
            },
            # Complete match but different type
            {
                "candidate": Index("payments", ("method", "status"), "hash"),
                "existing_defs": {
                    "CREATE INDEX payments_method_status_idx ON payments USING btree (method, status)"
                },
                "expected": False,
                "description": "Different index type",
            },
            # Different table
            {
                "candidate": Index("customers", ("id",)),
                "existing_defs": {
                    "CREATE INDEX users_id_idx ON users USING btree (id)"
                },
                "expected": False,
                "description": "Different table",
            },
            # Complex case with expression index
            {
                "candidate": Index("users", ("name",)),
                "existing_defs": {
                    "CREATE INDEX users_name_idx ON users USING btree (lower(name))"
                },
                "expected": False,
                "description": "Expression index vs regular column",
            },
        ]

        # Run all test cases
        for tc in test_cases:
            with self.subTest(description=tc["description"]):
                result = self.dta._index_exists(tc["candidate"], tc["existing_defs"])  # type: ignore
                self.assertEqual(
                    result,
                    tc["expected"],
                    f"Failed: {tc['description']}\nCandidate: {tc['candidate']}\n"
                    f"Existing: {tc['existing_defs']}\nExpected: {tc['expected']}",
                )

        # Test fallback mechanism when parsing fails
        with patch("pglast.parser.parse_sql", side_effect=Exception("Parsing error")):
            # Should use fallback and return True based on substring matching
            index = Index("users", ("name", "email"))
            exists = self.dta._index_exists(
                index,
                {
                    "CREATE INDEX users_name_email_idx ON users USING btree (name, email)"
                },
            )  # type: ignore
            self.assertTrue(exists, "Fallback mechanism should identify matching index")

            # Should return False when no match even with fallback
            index = Index("users", ("address",))
            exists = self.dta._index_exists(
                index,
                {
                    "CREATE INDEX users_name_email_idx ON users USING btree (name, email)"
                },
            )  # type: ignore
            self.assertFalse(
                exists, "Fallback mechanism should not match non-existing index"
            )

    def test_ndistinct_handling(self):
        """Test handling of ndistinct values in row estimation calculations."""
        # Mock necessary dependencies
        self.sql_driver.execute_query.return_value = []

        # Test cases with different ndistinct values
        test_cases = [
            {
                "stats": {
                    "total_width": 10.0,
                    "total_distinct": 5,
                },  # Positive ndistinct
                "expected": 180,  # 18.0 * 5.0 * 2.0
            },
            {
                "stats": {
                    "total_width": 10.0,
                    "total_distinct": -0.5,
                },  # Negative ndistinct
                "expected": 36,  # 18.0 * 1.0 * 2.0
            },
            {
                "stats": {"total_width": 10.0, "total_distinct": 0},  # Zero ndistinct
                "expected": 36,  # 18.0 * 1.0 * 2.0
            },
        ]

        for case in test_cases:
            result = self.dta._estimate_index_size_internal(  # type: ignore
                stats=case["stats"]
            )
            self.assertAlmostEqual(
                result,
                case["expected"],
                msg=f"Failed for n_distinct={case['stats']['total_distinct']}. Expected: {case['expected']}, Got: {result}",
            )

    def test_filter_long_text_columns(self):
        """Test filtering of long text columns from index candidates."""
        # Mock the column type query results
        type_query_results = [
            MockCell(
                {
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "character varying",
                    "character_maximum_length": 50,  # Short varchar - should keep
                    "avg_width": 4,
                    "potential_long_text": False,
                }
            ),
            MockCell(
                {
                    "table_name": "users",
                    "column_name": "bio",
                    "data_type": "text",  # Text type - needs length check
                    "character_maximum_length": None,
                    "avg_width": 105,
                    "potential_long_text": True,
                }
            ),
            MockCell(
                {
                    "table_name": "users",
                    "column_name": "description",
                    "data_type": "character varying",
                    "character_maximum_length": 200,  # Long varchar - should filter out
                    "avg_width": 70,
                    "potential_long_text": True,
                }
            ),
            MockCell(
                {
                    "table_name": "users",
                    "column_name": "status",
                    "data_type": "character varying",
                    "character_maximum_length": None,  # Unlimited varchar - needs length check
                    "avg_width": 10,
                    "potential_long_text": True,
                }
            ),
        ]

        def mock_execute_query(query):
            if "information_schema.columns" in query:
                return type_query_results
            return None

        self.sql_driver.execute_query.side_effect = mock_execute_query

        # Create test candidates
        candidates = [
            Index("users", ("name",)),  # Should keep (short varchar)
            Index("users", ("bio",)),  # Should filter out (long text)
            Index("users", ("description",)),  # Should filter out (long varchar)
            Index(
                "users", ("status",)
            ),  # Should keep (unlimited varchar but short actual length)
            Index("users", ("name", "status")),  # Should keep (both columns ok)
            Index("users", ("name", "bio")),  # Should filter out (contains long text)
            Index(
                "users", ("description", "status")
            ),  # Should filter out (contains long varchar)
        ]

        # Execute the filter with max_text_length = 100
        filtered = self.dta._filter_long_text_columns(candidates, max_text_length=100)  # type: ignore

        logger.info(f"Filtered: {filtered}")

        # Check results
        filtered_indexes = [(c.table, c.columns) for c in filtered]

        logger.info(f"Filtered indexes: {filtered_indexes}")

        # These should be kept
        self.assertIn(("users", ("name",)), filtered_indexes)
        self.assertIn(("users", ("status",)), filtered_indexes)
        self.assertIn(("users", ("name", "status")), filtered_indexes)

        # These should be filtered out
        self.assertNotIn(("users", ("bio",)), filtered_indexes)
        self.assertNotIn(("users", ("description",)), filtered_indexes)
        self.assertNotIn(("users", ("name", "bio")), filtered_indexes)
        self.assertNotIn(("users", ("description", "status")), filtered_indexes)

        # Verify the number of filtered results
        self.assertEqual(len(filtered), 3)


class TestDatabaseTuningAdvisorIntegration(unittest.TestCase):
    def setUp(self):
        """Set up a fresh DTA instance with mocked SQL driver for each test."""
        self.sql_driver = MagicMock()
        self.dta = DatabaseTuningAdvisor(
            sql_driver=self.sql_driver,
            budget_mb=50,  # 50 MB budget
            max_runtime_seconds=300,  # 300 seconds limit
            max_index_width=2,  # Up to 2-column indexes
            seed_columns_count=2,  # Top 2 single-column seeds
        )
        # Mock HypoPG extension as installed
        self.sql_driver.execute_query.side_effect = lambda q: [MockCell({"exists": 1})]

    def test_basic_workload_analysis(self):
        workload = [
            {"query": "SELECT * FROM users WHERE name = 'Alice'", "calls": 100},
            {"query": "SELECT * FROM orders WHERE user_id = 123", "calls": 50},
        ]
        global responses
        responses = [
            # pg_stat_statements
            [
                MockCell(
                    {
                        "queryid": 1,
                        "query": workload[0]["query"],
                        "calls": 100,
                        "avg_exec_time": 10.0,
                    }
                ),
                MockCell(
                    {
                        "queryid": 2,
                        "query": workload[1]["query"],
                        "calls": 50,
                        "avg_exec_time": 5.0,
                    }
                ),
            ],
            # pg_indexes
            [],
            # information_schema.columns
            [
                MockCell(
                    {
                        "table_name": "users",
                        "column_name": "name",
                        "data_type": "character varying",
                        "character_maximum_length": 150,
                        "avg_width": 30,
                        "potential_long_text": True,
                    }
                ),
                MockCell(
                    {
                        "table_name": "orders",
                        "column_name": "user_id",
                        "data_type": "integer",
                        "character_maximum_length": None,
                        "avg_width": 4,
                        "potential_long_text": False,
                    }
                ),
            ],
            # hypopg_create_index (for users.name, orders.user_id)
            [MockCell({"indexrelid": 1554}), MockCell({"indexrelid": 1555})],
            # hypopg_list_indexes
            [
                MockCell(
                    {"index_name": "crystaldba_idx_users_name_1", "index_size": 8000}
                ),
                MockCell(
                    {
                        "index_name": "crystaldba_idx_orders_user_id_1",
                        "index_size": 4000,
                    }
                ),
            ],
            # hypopg_reset
            [],
            # EXPLAIN without indexes
            [MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 100.0}}]})],  # users.name
            [
                MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 150.0}}]})
            ],  # orders.user_id
            [MockCell({"rel_size": 10000})],  # users table size
            [MockCell({"rel_size": 10000})],  # orders table size
            # pg_stats for size (users.name, orders.user_id)
            [MockCell({"total_width": 10, "total_distinct": 100})],
            # EXPLAIN with users.name index
            [MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 50.0}}]})],  # users.name
            [
                MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 150.0}}]})
            ],  # orders.user_id
            [MockCell({"total_width": 8, "total_distinct": 50})],
            # EXPLAIN without orders.user_id index
            [MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 100.0}}]})],  # users.name
            [
                MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 75.0}}]})
            ],  # orders.user_id
            # EXPLAIN with users.name and orders.user_id indexes
            [MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 50.0}}]})],  # users.name
            [
                MockCell({"QUERY PLAN": [{"Plan": {"Total Cost": 75.0}}]})
            ],  # orders.user_id
            # hypopg_reset (final cleanup)
            [],
        ]

        global responses_index
        responses_index = 0

        def mock_execute_query(query):
            global responses_index
            responses_index += 1
            logger.debug(
                f"Query: {query}\n    Response: {
                    list(json.dumps(x.cells) for x in responses[responses_index - 1])
                    if responses_index <= len(responses)
                    else None
                }\n--------------------------------------------------------"
            )
            return (
                responses[responses_index - 1]
                if responses_index <= len(responses)
                else None
            )

        self.sql_driver.execute_query.side_effect = mock_execute_query
        session = self.dta.analyze_workload(min_calls=50, min_avg_time_ms=5.0)
        recs = session.recommendations
        self.assertTrue(
            any(r.table == "users" and r.columns == ("name",) for r in recs)
        )
        self.assertTrue(
            any(r.table == "orders" and r.columns == ("user_id",) for r in recs)
        )
        self.assertTrue(all(r.progressive_improvement_multiple > 0 for r in recs))


class TestParameterReplacement(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.sql_driver = MagicMock()
        self.dta = DatabaseTuningAdvisor(
            sql_driver=self.sql_driver, budget_mb=10, max_runtime_seconds=60
        )

    def test_replace_parameters_basic(self):
        """Test basic parameter replacement functionality."""
        # Mock the column statistics lookup
        self.dta._column_stats_cache = {}  # type: ignore
        self.dta.extract_columns = MagicMock(
            return_value={"users": ["name", "id", "status"]}
        )  # type: ignore
        self.dta._identify_parameter_column = MagicMock(return_value=("users", "name"))  # type: ignore
        self.dta._get_column_statistics = MagicMock(  # type: ignore
            return_value={
                "data_type": "character varying",
                "common_vals": ["John", "Alice"],
                "histogram_bounds": None,
            }
        )

        query = "SELECT * FROM users WHERE name = $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertEqual(result, "select * from users where name = 'John'")

        # Verify the column was identified correctly
        self.dta._identify_parameter_column.assert_called_once()  # type: ignore

    def test_replace_parameters_numeric(self):
        """Test parameter replacement for numeric columns."""
        self.dta._column_stats_cache = {}  # type: ignore
        self.dta.extract_columns = MagicMock(
            return_value={"orders": ["id", "amount", "user_id"]}
        )  # type: ignore
        self.dta._identify_parameter_column = MagicMock(
            return_value=("orders", "amount")
        )  # type: ignore
        self.dta._get_column_statistics = MagicMock(  # type: ignore
            return_value={
                "data_type": "numeric",
                "common_vals": [99.99, 49.99],
                "histogram_bounds": [10.0, 50.0, 100.0, 500.0],
            }
        )

        # Range query
        query = "SELECT * FROM orders WHERE amount > $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertEqual(result, "select * from orders where amount > 100.0")

        # Equality query
        self.dta._identify_parameter_column = MagicMock(
            return_value=("orders", "amount")
        )  # type: ignore
        query = "SELECT * FROM orders WHERE amount = $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertEqual(result, "select * from orders where amount = 99.99")

    def test_replace_parameters_date(self):
        """Test parameter replacement for date columns."""
        self.dta._column_stats_cache = {}  # type: ignore
        self.dta.extract_columns = MagicMock(
            return_value={"orders": ["id", "order_date", "user_id"]}
        )  # type: ignore
        self.dta._identify_parameter_column = MagicMock(
            return_value=("orders", "order_date")
        )  # type: ignore
        self.dta._get_column_statistics = MagicMock(  # type: ignore
            return_value={
                "data_type": "timestamp without time zone",
                "common_vals": None,
                "histogram_bounds": None,
            }
        )

        query = "SELECT * FROM orders WHERE order_date > $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertEqual(result, "select * from orders where order_date > '2023-01-15'")

    def test_replace_parameters_like(self):
        """Test parameter replacement for LIKE patterns."""
        self.dta._column_stats_cache = {}  # type: ignore
        self.dta.extract_columns = MagicMock(return_value={"users": ["name", "email"]})  # type: ignore
        self.dta._identify_parameter_column = MagicMock(return_value=("users", "name"))  # type: ignore
        self.dta._get_column_statistics = MagicMock(  # type: ignore
            return_value={
                "data_type": "character varying",
                "common_vals": ["John", "Alice"],
                "histogram_bounds": None,
            }
        )

        query = "SELECT * FROM users WHERE name LIKE $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertEqual(result, "select * from users where name like '%test%'")

    def test_replace_parameters_multiple(self):
        """Test replacement of multiple parameters in a complex query."""
        self.dta._column_stats_cache = {}  # type: ignore
        self.dta.extract_columns = MagicMock(  # type: ignore
            return_value={
                "users": ["id", "name", "status"],
                "orders": ["id", "user_id", "amount", "order_date"],
            }
        )

        # We'll need to return different values based on the context
        def identify_column_side_effect(context, table_columns: Dict[str, Set[str]]):
            if "status =" in context:
                return ("users", "status")
            elif "amount BETWEEN" in context:
                return ("orders", "amount")
            elif "order_date >" in context:
                return ("orders", "order_date")
            return None

        self.dta._identify_parameter_column = MagicMock(
            side_effect=identify_column_side_effect
        )  # type: ignore

        def get_stats_side_effect(table, column):
            if table == "users" and column == "status":
                return {
                    "data_type": "character varying",
                    "common_vals": ["active", "inactive"],
                }
            elif table == "orders" and column == "amount":
                return {
                    "data_type": "numeric",
                    "common_vals": [99.99],
                    "histogram_bounds": [10.0, 50.0, 100.0],
                }
            elif table == "orders" and column == "order_date":
                return {"data_type": "timestamp without time zone"}
            return None

        self.dta._get_column_statistics = MagicMock(side_effect=get_stats_side_effect)  # type: ignore

        query = """
        SELECT u.name, o.amount
        FROM users u
        JOIN orders o ON u.id = o.user_id
        WHERE u.status = $1
        AND o.amount BETWEEN $2 AND $3
        AND o.order_date > $4
        """

        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertTrue("u.status = 'active'" in result)
        self.assertTrue("o.amount between 10.0 and 100.0" in result)
        self.assertTrue("o.order_date > '2023-01-15'" in result)

    def test_replace_parameters_fallback(self):
        """Test fallback behavior when column information is not available."""
        self.dta.extract_columns = MagicMock(return_value={})  # type: ignore

        # Simple query with numeric parameter
        query = "SELECT * FROM users WHERE id = $1"
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertTrue(
            "id = 46" in result or "id = '46'" in result
        )  # Either format is acceptable

        # Complex query with various parameters
        query = """
        SELECT * FROM users
        WHERE status = $1
        AND created_at > $2
        AND name LIKE $3
        AND age BETWEEN $4 AND $5
        """
        result = self.dta._replace_parameters(query.lower())  # type: ignore
        self.assertTrue(
            "status = 'active'" in result or "status = 'sample_value'" in result
        )
        self.assertTrue("created_at > '2023-01-01'" in result)
        self.assertTrue("name like '%sample%'" in result or "name like '%" in result)
        self.assertTrue("between 10 and 100" in result or "between 42 and 42" in result)

    def testextract_columns(self):
        """Test extracting table and column information from queries."""
        # Setup extract_columns mock to return realistic column data
        self.dta.extract_columns = MagicMock(
            return_value={"users": {"id", "name", "status"}}
        )

        query = "SELECT * FROM users WHERE name = $1 AND status = $2"
        result = self.dta.extract_columns(query)  # type: ignore
        self.assertEqual(result, {"users": {"id", "name", "status"}})

    def test_identify_parameter_column(self):
        """Test identifying which column a parameter belongs to."""
        table_columns = {
            "users": ["id", "name", "status", "email"],
            "orders": ["id", "user_id", "amount", "order_date"],
        }

        # Test equality pattern
        context = "SELECT * FROM users WHERE name = $1"
        result = self.dta._identify_parameter_column(context, table_columns)  # type: ignore
        self.assertEqual(result, ("users", "name"))

        # Test LIKE pattern
        context = "SELECT * FROM users WHERE email LIKE $1"
        result = self.dta._identify_parameter_column(context, table_columns)  # type: ignore
        self.assertEqual(result, ("users", "email"))

        # Test range pattern
        context = "SELECT * FROM orders WHERE amount > $1"
        result = self.dta._identify_parameter_column(context, table_columns)  # type: ignore
        self.assertEqual(result, ("orders", "amount"))

        # Test BETWEEN pattern
        context = "SELECT * FROM orders WHERE order_date BETWEEN $1 AND $2"
        result = self.dta._identify_parameter_column(context, table_columns)  # type: ignore
        self.assertEqual(result, ("orders", "order_date"))

        # Test no match
        context = "SELECT * FROM users WHERE $1"  # Invalid but should handle gracefully
        result = self.dta._identify_parameter_column(context, table_columns)  # type: ignore
        self.assertIsNone(result)

    def test_get_replacement_value(self):
        """Test generating replacement values based on statistics."""
        # String type with common values for equality
        stats = {
            "data_type": "character varying",
            "common_vals": ["active", "pending", "completed"],
            "histogram_bounds": None,
        }
        result = self.dta._get_replacement_value(stats, "status = $1")  # type: ignore
        self.assertEqual(result, "'active'")

        # Numeric type with histogram bounds for range
        stats = {
            "data_type": "numeric",
            "common_vals": [10.0, 20.0],
            "histogram_bounds": [5.0, 15.0, 25.0, 50.0, 100.0],
        }
        result = self.dta._get_replacement_value(stats, "amount > $1")  # type: ignore
        self.assertEqual(result, "25.0")

        # Date type
        stats = {"data_type": "date", "common_vals": None, "histogram_bounds": None}
        result = self.dta._get_replacement_value(stats, "created_at < $1")  # type: ignore
        self.assertEqual(result, "'2023-01-15'")

        # Boolean type
        stats = {
            "data_type": "boolean",
            "common_vals": [True, False],
            "histogram_bounds": None,
        }
        result = self.dta._get_replacement_value(stats, "is_active = $1")  # type: ignore
        self.assertEqual(result, "true")


class TestColumnAliasProcessing(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.sql_driver = MagicMock()
        self.dta = DatabaseTuningAdvisor(
            sql_driver=self.sql_driver, budget_mb=10, max_runtime_seconds=60
        )

    def test_condition_column_collector_simple(self):
        """Test basic functionality of ConditionColumnCollector."""
        query = "SELECT hobby FROM users WHERE name = 'Alice' AND age > 25"
        parsed = parse_sql(query)[0].stmt

        collector = ColumnCollector()
        collector(parsed)

        self.assertEqual(collector.columns, {"users": {"hobby", "name", "age"}})

        collector = ConditionColumnCollector()
        collector(parsed)

        self.assertEqual(collector.condition_columns, {"users": {"name", "age"}})

    def test_condition_column_collector_join(self):
        """Test condition column collection with JOIN conditions."""
        query = """
        SELECT u.name, o.order_date
        FROM users u
        JOIN orders o ON u.id = o.user_id
        WHERE o.status = 'pending' AND u.active = true
        """
        parsed = parse_sql(query)[0].stmt

        collector = ColumnCollector()
        collector(parsed)

        self.assertEqual(
            collector.columns,
            {
                "users": {"id", "active", "name"},
                "orders": {"user_id", "status", "order_date"},
            },
        )

        collector = ConditionColumnCollector()
        collector(parsed)

        self.assertEqual(
            collector.condition_columns,
            {"users": {"id", "active"}, "orders": {"user_id", "status"}},
        )

    def test_condition_column_collector_with_alias(self):
        """Test condition column collection with column aliases in conditions."""
        query = """
        SELECT u.name, u.age, COUNT(o.id) as order_count , o.order_date as begin_order_date, o.status as order_status
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        WHERE u.status = 'active'
        GROUP BY u.name
        HAVING order_count > 5
        ORDER BY order_count DESC
        """
        parsed = parse_sql(query)[0].stmt

        cond_collector = ConditionColumnCollector()
        cond_collector(parsed)

        # Should extract o.id from HAVING COUNT(o.id) > 5
        # But should NOT include order_count as a table column
        self.assertEqual(
            cond_collector.condition_columns,
            {"users": {"id", "status"}, "orders": {"user_id", "id"}},
        )

        # Verify order_count is recognized as an alias
        self.assertIn("order_count", cond_collector.column_aliases)

        collector = ColumnCollector()
        collector(parsed)

        self.assertEqual(
            collector.columns,
            {
                "users": {"id", "status", "name", "age"},
                "orders": {"user_id", "id", "status", "order_date"},
            },
        )

    def test_complex_query_with_alias_in_conditions(self):
        """Test complex query with aliases used in multiple conditions."""
        query = """
        SELECT
            u.name,
            u.email,
            EXTRACT(YEAR FROM u.created_at) as join_year,
            COUNT(o.id) as order_count,
            SUM(o.total) as revenue
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        WHERE u.status = 'active' AND join_year > 2020
        GROUP BY u.name, u.email, join_year
        HAVING order_count > 10 AND revenue > 1000
        ORDER BY revenue DESC
        """
        parsed = parse_sql(query)[0].stmt

        collector = ColumnCollector()
        collector(parsed)

        # Should extract the underlying columns from aliases used in conditions
        self.assertEqual(
            collector.columns,
            {
                "users": {"id", "status", "created_at", "name", "email"},
                "orders": {"user_id", "id", "total"},
            },
        )

        collector = ConditionColumnCollector()
        collector(parsed)

        # Should extract the underlying columns from aliases used in conditions
        self.assertEqual(
            collector.condition_columns,
            {
                "users": {"id", "status", "created_at"},
                "orders": {"user_id", "id", "total"},
            },
        )

        # Verify aliases are recognized
        self.assertIn("join_year", collector.column_aliases)
        self.assertIn("order_count", collector.column_aliases)
        self.assertIn("revenue", collector.column_aliases)

    def test_filter_candidates_by_query_conditions(self):
        """Test filtering index candidates based on query conditions."""
        # Mock the sql_driver for _column_exists
        self.sql_driver.execute_query.return_value = [MockCell({"1": 1})]

        # Create test queries
        q1 = "SELECT * FROM users WHERE name = 'Alice' AND age > 25"
        q2 = "SELECT * FROM orders WHERE status = 'pending' AND total > 100"
        queries = [(q1, parse_sql(q1)[0].stmt, 1.0), (q2, parse_sql(q2)[0].stmt, 1.0)]

        # Create test candidates (some with columns not in conditions)
        candidates = [
            Index("users", ("name",)),
            Index("users", ("name", "email")),  # email not in conditions
            Index("users", ("age",)),
            Index("orders", ("status", "total")),
            Index("orders", ("order_date",)),  # order_date not in conditions
        ]

        # Execute the filter
        filtered = self.dta._filter_candidates_by_query_conditions(queries, candidates)  # type: ignore

        # Check results
        filtered_tables_columns = [(c.table, c.columns) for c in filtered]
        self.assertIn(("users", ("name",)), filtered_tables_columns)
        self.assertIn(("users", ("age",)), filtered_tables_columns)
        self.assertIn(("orders", ("status", "total")), filtered_tables_columns)

        # These shouldn't be in the filtered list
        self.assertNotIn(("users", ("name", "email")), filtered_tables_columns)
        self.assertNotIn(("orders", ("order_date",)), filtered_tables_columns)

    def test_extract_condition_columns(self):
        """Test the _extract_condition_columns method directly."""
        query = """
        SELECT u.name, o.order_date
        FROM users u, orders o
        WHERE o.status = 'pending' AND u.active = true
        """
        parsed = parse_sql(query)[0].stmt

        collector = ConditionColumnCollector()
        collector(parsed)

        # Check results
        self.assertEqual(
            collector.condition_columns, {"users": {"active"}, "orders": {"status"}}
        )

    def test_condition_collector_with_order_by(self):
        """Test that columns used in ORDER BY are collected for indexing."""
        query = """
        SELECT u.name, o.order_date, o.amount
        FROM orders o
        JOIN users u ON o.user_id = u.id
        WHERE o.status = 'completed'
        ORDER BY o.order_date DESC
        """
        parsed = parse_sql(query)[0].stmt

        collector = ConditionColumnCollector()
        collector(parsed)

        # Should include o.order_date from ORDER BY clause
        self.assertEqual(
            collector.condition_columns,
            {"users": {"id"}, "orders": {"user_id", "status", "order_date"}},
        )

    def test_condition_collector_with_order_by_alias(self):
        """Test that columns in aliased expressions in ORDER BY are collected."""
        query = """
        SELECT u.name, COUNT(o.id) as order_count
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        WHERE u.status = 'active'
        GROUP BY u.name
        ORDER BY order_count DESC
        """
        parsed = parse_sql(query)[0].stmt

        collector = ConditionColumnCollector()
        collector(parsed)

        # Should extract o.id from ORDER BY order_count DESC
        # where order_count is COUNT(o.id)
        self.assertEqual(
            collector.condition_columns,
            {"users": {"id", "status"}, "orders": {"user_id", "id"}},
        )


class TestDiminishingReturns(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.sql_driver = MagicMock()
        self.dta = DatabaseTuningAdvisor(
            sql_driver=self.sql_driver, budget_mb=1000, max_runtime_seconds=120
        )

        # Mock the _check_time method to always return False (no time limit reached)
        self.dta._check_time = MagicMock(return_value=False)  # type: ignore

        # Mock the _estimate_index_size method to return a fixed size
        self.dta._estimate_index_size = MagicMock(
            return_value=1024 * 1024
        )  # 1MB per index   # type: ignore

    def test_enumerate_greedy_pareto_cost_benefit(self):
        """Test the Pareto optimal implementation with the specified cost/benefit analysis."""

        # Create test queries
        q1 = "SELECT * FROM test_table WHERE col1 = 1"
        queries = [(q1, parse_sql(q1)[0].stmt, 1.0)]

        # Create candidate indexes
        candidate_indexes = set()
        for i in range(10):
            candidate_indexes.add(IndexConfig(table="test_table", columns=(f"col{i}",)))

        # Base query cost
        base_cost = 1000.0

        # Define costs for different configurations
        config_costs = {
            0: 1000,  # No indexes
            1: 700,  # With index 0: 30% improvement
            2: 560,  # With indexes 0,1: 20% improvement
            3: 504,  # With indexes 0,1,2: 10% improvement
            4: 479,  # With indexes 0,1,2,3: 5% improvement
            5: 474,  # With indexes 0,1,2,3,4: 1% improvement
            6: 469,  # ~1% improvements each
            7: 464,
            8: 459,
            9: 454,
            10: 449,
        }

        # Define index sizes
        index_sizes = {
            0: 1 * 1024 * 1024,  # 1MB - very efficient
            1: 2 * 1024 * 1024,  # 2MB - efficient
            2: 2 * 1024 * 1024,  # 2MB - less efficient
            3: 8 * 1024 * 1024,  # 8MB - inefficient
            4: 16 * 1024 * 1024,  # 16MB - very inefficient
            5: 32 * 1024 * 1024,  # 32MB
            6: 32 * 1024 * 1024,
            7: 32 * 1024 * 1024,
            8: 32 * 1024 * 1024,
            9: 32 * 1024 * 1024,
        }

        # Base relation size
        base_relation_size = 50 * 1024 * 1024  # 50MB for test_table

        # Mock the cost evaluation
        def mock_evaluate_cost(queries, config):
            return config_costs[len(config)]

        # Mock the index size calculation
        def mock_index_size(table, columns):
            if len(columns) == 1 and columns[0].startswith("col"):
                index_num = int(columns[0][3:])
                return index_sizes.get(index_num, 1024 * 1024)
            return 1024 * 1024

        # Mock the estimate_table_size method
        def mock_estimate_table_size(table):
            return base_relation_size

        # Mock SQL driver execute_query method to simulate getting table size
        def mock_execute_query(query):
            if "pg_total_relation_size" in query:
                return [MockCell({"rel_size": base_relation_size})]
            return []

        self.dta._evaluate_configuration_cost = MagicMock(
            side_effect=mock_evaluate_cost
        )  # type: ignore
        self.dta._estimate_index_size = MagicMock(side_effect=mock_index_size)  # type: ignore
        self.dta._estimate_table_size = MagicMock(side_effect=mock_estimate_table_size)  # type: ignore
        self.dta.sql_driver.execute_query = MagicMock(side_effect=mock_execute_query)  # type: ignore

        # Set alpha parameter for cost/benefit analysis
        self.dta.pareto_alpha = 2.0

        # Set minimum time improvement threshold to stop after 3 indexes
        self.dta.min_time_improvement = 0.05  # 5% threshold

        # Call _enumerate_greedy with cost/benefit analysis
        current_indexes = set()
        current_cost = base_cost
        final_indexes, final_cost = self.dta._enumerate_greedy(
            queries, current_indexes, current_cost, candidate_indexes.copy()
        )  # type: ignore

        # We expect exactly 3 indexes to be selected with 5% threshold
        self.assertEqual(len(final_indexes), 3)
        self.assertEqual(final_cost, 504)  # Cost after adding 3 indexes

        # Test with a lower threshold - should include more indexes
        self.dta.min_time_improvement = 0.015  # 1.5% threshold

        current_indexes = set()
        current_cost = base_cost
        final_indexes_lower_threshold, final_cost_lower_threshold = (
            self.dta._enumerate_greedy(  # type: ignore
                queries, current_indexes, current_cost, candidate_indexes.copy()
            )
        )  # type: ignore

        # With 1% threshold, should include at least 3 indexes
        self.assertEqual(len(final_indexes_lower_threshold), 3)

        # Test with a higher threshold - should include fewer indexes
        self.dta.min_time_improvement = 0.25  # 25% threshold

        current_indexes = set()
        current_cost = base_cost
        final_indexes_higher_threshold, final_cost_higher_threshold = (
            self.dta._enumerate_greedy(  # type: ignore
                queries, current_indexes, current_cost, candidate_indexes.copy()
            )
        )

        # With 25% threshold, should include only the first 1 index
        self.assertEqual(len(final_indexes_higher_threshold), 1)


def test_explain_plan_diff():
    """Test the explain plan diff functionality."""
    # Create a before plan with sequential scan
    before_plan = {
        "Plan": {
            "Node Type": "Aggregate",
            "Strategy": "Plain",
            "Startup Cost": 300329.67,
            "Total Cost": 300329.68,
            "Plan Rows": 1,
            "Plan Width": 32,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "users",
                    "Alias": "users",
                    "Startup Cost": 0.00,
                    "Total Cost": 286022.64,
                    "Plan Rows": 1280,
                    "Plan Width": 32,
                    "Filter": "email LIKE '%example.com'",
                }
            ],
        }
    }

    # Create an after plan with index scan instead
    after_plan = {
        "Plan": {
            "Node Type": "Aggregate",
            "Strategy": "Plain",
            "Startup Cost": 17212.28,
            "Total Cost": 17212.29,
            "Plan Rows": 1,
            "Plan Width": 32,
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "users",
                    "Alias": "users",
                    "Index Name": "users_email_idx",
                    "Startup Cost": 0.43,
                    "Total Cost": 17212.00,
                    "Plan Rows": 1280,
                    "Plan Width": 32,
                    "Filter": "email LIKE '%example.com'",
                }
            ],
        }
    }

    # Generate the diff
    diff_output = ExplainPlanArtifact.create_plan_diff(before_plan, after_plan)

    # Verify the diff contains key expected elements
    assert "PLAN CHANGES:" in diff_output
    assert "Cost:" in diff_output
    assert "improvement" in diff_output

    # Verify it detected the change from Seq Scan to Index Scan
    assert "Seq Scan" in diff_output
    assert "Index Scan" in diff_output

    # Verify it includes some form of diff notation
    assert "→" in diff_output

    # Verify the cost values are shown in the diff
    assert "300329" in diff_output
    assert "17212" in diff_output

    # Verify it mentions the structural change
    assert (
        "sequential scans replaced" in diff_output or "new index scans" in diff_output
    )

    # Test with invalid plan data
    empty_diff = ExplainPlanArtifact.create_plan_diff({}, {})
    assert "Cannot generate diff" in empty_diff

    # Test with missing Plan field
    invalid_diff = ExplainPlanArtifact.create_plan_diff(
        {"NotAPlan": {}}, {"NotAPlan": {}}
    )
    assert "Cannot generate diff" in invalid_diff


if __name__ == "__main__":
    unittest.main()
