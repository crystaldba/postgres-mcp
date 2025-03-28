"""Safe SQL driver wrapper for executing queries."""

import logging
from typing import Any

import psycopg2.extensions as ext
from psycopg2.sql import SQL
from psycopg2.sql import Composable

from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


class LiteralParam(Composable):
    """
    A `Composable` representing an SQL value to include in a query.

    Usually you will want to include placeholders in the query and pass values
    as `~cursor.execute()` arguments. If however you really really need to
    include a literal value in the query you can use this object.

    The string returned by `!as_string()` follows the normal :ref:`adaptation
    rules <python-types-adaptation>` for Python objects.

    Example::

        >>> s1 = LiteralParam("foo")
        >>> s2 = LiteralParam("ba'r")
        >>> s3 = LiteralParam(42)
        >>> print(sql.SQL(', ').join([s1, s2, s3]).as_string(conn))
        'foo', 'ba''r', 42

    """

    @property
    def wrapped(self):
        """The object wrapped by the `!LiteralParam`."""
        return self._wrapped  # type: ignore

    def as_string(self, context):
        a = ext.adapt(self.wrapped)

        rv = a.getquoted()
        if isinstance(rv, bytes):
            rv = rv.decode(ext.encodings["UTF8"])

        return rv


class SafeSqlDriver:
    """Wrapper for safely executing SQL queries."""

    @staticmethod
    def sql_to_query(sql: Composable) -> str:
        """Convert a SQL string to a query string."""
        return sql.as_string({})  # type: ignore

    @staticmethod
    def param_sql_to_query(query: str, params: list[Any]) -> str:
        """Convert a SQL string to a query string."""
        return SafeSqlDriver.sql_to_query(
            SQL(query).format(*[LiteralParam(p) for p in params])
        )

    @staticmethod
    def execute_param_query(
        sql_driver: SqlDriver, query: str, params: list[Any] | None = None
    ):
        """Execute a query after validating it is safe"""
        if params:
            query_params = SafeSqlDriver.param_sql_to_query(query, params)
            return sql_driver.execute_query(query_params)
        else:
            return sql_driver.execute_query(query)
