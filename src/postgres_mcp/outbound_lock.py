"""First-class outbound intent lock operations for the Comm-Data-Store MCP server.

Wraps the existing, concurrency-safe SQL functions (`acquire_outbound_intent_lock`
/ `complete_outbound_intent_lock` / `release_outbound_intent_lock`, migration 026)
behind one tool with an `op` param, so agents never hand-write lock SQL through
psql/asyncpg again. All responses are strict JSON.

The load-bearing semantic (2026-07-01 near-miss: an agent read a completed lock —
which also has `released_at` set — as "free to proceed"): duplicate-send evidence
is encoded structurally in every response as
`is_duplicate_send_evidence = (completed_at IS NOT NULL AND request_ref IS NOT NULL)`,
independent of `released_at`. The released-vs-completed misread is then impossible.

The DB-facing runner takes an injected sql driver, so the operation logic is unit
testable with a mock and the pure response shaping needs no DB at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import LiteralString
from typing import cast

from .sql import SafeSqlDriver

VALID_OPS = ("acquire", "complete", "release", "check")

# The full lock row, selected the same way everywhere so evidence fields are always present.
_LOCK_COLUMNS = "id, identity_key, property_scope, intent_kind, holder, request_ref, completed, acquired_at, expires_at, completed_at, released_at"


def _iso(value: Any) -> Any:
    """JSON-safe timestamp: ISO string for datetimes, passthrough otherwise."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def is_duplicate_send_evidence(cells: dict[str, Any]) -> bool:
    """A lock proves a send already happened ONLY when it is completed AND carries a
    request ref. `released_at` is deliberately NOT consulted — a completed lock also
    has released_at set, and a plain release (abort) has released_at set but is NOT
    evidence of a send."""
    return cells.get("completed_at") is not None and bool(
        (cells.get("request_ref") or "").strip() if isinstance(cells.get("request_ref"), str) else cells.get("request_ref")
    )


def lock_json(cells: dict[str, Any]) -> dict[str, Any]:
    """Strict-JSON view of one lock row, with the evidence flag encoded."""
    return {
        "lock_id": cells.get("id"),
        "identity_key": cells.get("identity_key"),
        "property_scope": cells.get("property_scope"),
        "intent_kind": cells.get("intent_kind"),
        "holder": cells.get("holder"),
        "request_ref": cells.get("request_ref"),
        "completed": cells.get("completed"),
        "acquired_at": _iso(cells.get("acquired_at")),
        "expires_at": _iso(cells.get("expires_at")),
        "completed_at": _iso(cells.get("completed_at")),
        "released_at": _iso(cells.get("released_at")),
        "is_duplicate_send_evidence": is_duplicate_send_evidence(cells),
    }


def _err(op: str, message: str) -> dict[str, Any]:
    return {"op": op, "error": message}


async def _lock_row(driver, lock_id: int) -> dict[str, Any] | None:
    rows = await SafeSqlDriver.execute_param_query(
        driver,
        f"SELECT {_LOCK_COLUMNS} FROM outbound_intent_locks WHERE id = {{}}",
        [lock_id],
    )
    return rows[0].cells if rows else None


async def run_outbound_lock(
    driver,
    *,
    op: str,
    identity_key: str | None = None,
    property_scope: str = "",
    intent_kind: str | None = None,
    holder: str | None = None,
    lock_id: int | None = None,
    request_ref: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Execute one lock operation and return a strict-JSON-serializable dict."""
    if op not in VALID_OPS:
        return _err(op, f"unknown op '{op}' (expected one of {', '.join(VALID_OPS)})")

    if op == "acquire":
        if not (identity_key and intent_kind and holder):
            return _err(op, "acquire requires identity_key, intent_kind and holder")
        rows = await SafeSqlDriver.execute_param_query(
            driver,
            "SELECT * FROM acquire_outbound_intent_lock({}, {}, {}, {})",
            [identity_key, property_scope or "", intent_kind, holder],
        )
        r = rows[0].cells if rows else {}
        if r.get("acquired"):
            return {"op": op, "acquired": True, "lock_id": r.get("lock_id"), "existing": None}
        # Blocked. Return the FULL blocking lock so the caller needs no follow-up query.
        existing = None
        if r.get("lock_id"):
            blk = await _lock_row(driver, int(r["lock_id"]))
            if blk:
                existing = lock_json(blk)
        return {"op": op, "acquired": False, "lock_id": None, "existing": existing}

    if op == "complete":
        if not (lock_id and holder and request_ref):
            return _err(op, "complete requires lock_id, holder and request_ref (the verified send handle)")
        rows = await SafeSqlDriver.execute_param_query(
            driver,
            "SELECT complete_outbound_intent_lock({}, {}, {}) AS completed",
            [lock_id, holder, request_ref],
        )
        completed = bool(rows[0].cells.get("completed")) if rows else False
        row = await _lock_row(driver, int(lock_id))
        return {"op": op, "completed": completed, "lock": lock_json(row) if row else None}

    if op == "release":
        if not (lock_id and holder):
            return _err(op, "release requires lock_id and holder")
        rows = await SafeSqlDriver.execute_param_query(
            driver,
            "SELECT release_outbound_intent_lock({}, {}) AS released",
            [lock_id, holder],
        )
        return {"op": op, "released": bool(rows[0].cells.get("released")) if rows else False}

    # op == "check": recent locks for an identity (+ optional scope/intent), with evidence.
    if not identity_key:
        return _err(op, "check requires identity_key")
    where = ["identity_key = normalize_intent_lock_text({})"]
    params: list[Any] = [identity_key]
    if property_scope:
        where.append("property_scope = normalize_intent_lock_text({})")
        params.append(property_scope)
    if intent_kind:
        where.append("intent_kind = normalize_intent_lock_text({})")
        params.append(intent_kind)
    where.append("acquired_at > now() - make_interval(days => {})")
    params.append(max(1, int(days)))
    query = f"SELECT {_LOCK_COLUMNS} FROM outbound_intent_locks WHERE {' AND '.join(where)} ORDER BY acquired_at DESC LIMIT 50"
    rows = await SafeSqlDriver.execute_param_query(
        driver,
        cast(LiteralString, query),
        params,
    )
    return {"op": op, "locks": [lock_json(row.cells) for row in (rows or [])]}
