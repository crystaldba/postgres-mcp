from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any, LiteralString

import sqlalchemy
from attrs import define
from attrs import field

from ..dta.sql_driver import SqlDriver

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@define
class LocalSqlDriver(SqlDriver):
    """A SQL driver implementation that uses SQLAlchemy directly."""

    engine_url: str | sqlalchemy.URL = field(kw_only=True)
    create_engine_params: dict[str, Any] = field(factory=dict, kw_only=True)
    _engine: Engine = field(default=None, kw_only=True, alias="engine", metadata={"serializable": False})

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = sqlalchemy.create_engine(self.engine_url, **self.create_engine_params)
        return self._engine

    async def connect(self):
        """Establish a connection to the database."""
        # Connection is established lazily when needed
        pass

    def local_execute_query_raw(self, query: str) -> list[dict[str, Any]] | None:
        """Execute a query and return raw results."""
        with self.engine.connect() as con:
            try:
                results = con.execute(sqlalchemy.text(query))
                if not results.returns_rows:
                    return None
                return [dict(result._mapping) for result in results]  # pyright: ignore[reportPrivateUsage]
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Error: client sql execution error {e!r}")
                raise

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,
    ) -> list[SqlDriver.RowResult] | None:
        """Execute a query and return results as RowResult objects."""
        # For now, ignoring params and force_readonly
        raw_results = self.local_execute_query_raw(query)
        if raw_results is None:
            return None
        return [SqlDriver.RowResult(cells=row) for row in raw_results]

    def get_table_schema(self, table_name: str, schema: str | None = None) -> str | None:
        """Get the schema definition for a table."""
        try:
            if schema is not None and table_name.startswith(f"{schema}."):
                table_name = table_name[len(schema) + 1 :]
            table = sqlalchemy.Table(table_name, sqlalchemy.MetaData(), schema=schema, autoload_with=self.engine)
            res = str([(c.name, c.type) for c in table.columns])
            return res
        except sqlalchemy.exc.NoSuchTableError:
            return None

    async def close(self) -> None:
        """Close the database connection."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
