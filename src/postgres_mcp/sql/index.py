from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndexConfig:
    """Immutable index configuration for hashing."""

    table: str
    columns: tuple[str, ...]
    using: str = "btree"
    potential_problematic_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": list(self.columns),
            "using": self.using,
            "definition": self.definition,
        }

    @property
    def definition(self) -> str:
        return f"CREATE INDEX {self.name} ON {self.table} USING {self.using} ({', '.join(self.columns)})"

    @property
    def name(self) -> str:
        return f"crystaldba_idx_{self.table}_{'_'.join(self.columns)}_{len(self.columns)}" + ("" if self.using == "btree" else f"_{self.using}")

    def __str__(self) -> str:
        return self.definition

    def __repr__(self) -> str:
        return self.definition
