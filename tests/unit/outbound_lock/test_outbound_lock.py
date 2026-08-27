"""Unit tests for the outbound_lock operations (pure semantics + op runner with a
mocked sql driver — no DB)."""

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from postgres_mcp import outbound_lock as ol


class _Row:
    def __init__(self, cells):
        self.cells = cells


def _mock_query(side_effects):
    """Patch SafeSqlDriver.execute_param_query to return queued results per call.
    Each side effect is a list-of-cells-dicts (wrapped in _Row) or None."""
    calls = []

    async def fake(driver, query, params=None):
        calls.append((query, params))
        res = side_effects.pop(0)
        return None if res is None else [_Row(c) for c in res]

    return patch.object(ol.SafeSqlDriver, "execute_param_query", AsyncMock(side_effect=fake)), calls


# ---- the load-bearing semantic ----


def test_evidence_true_only_when_completed_and_request_ref():
    assert ol.is_duplicate_send_evidence({"completed_at": datetime.now(timezone.utc), "request_ref": "AC123"}) is True


def test_evidence_false_for_released_but_not_completed():
    # a plain release (abort) sets released_at but NOT completed_at — NOT a send
    assert ol.is_duplicate_send_evidence({"completed_at": None, "released_at": datetime.now(timezone.utc), "request_ref": None}) is False


def test_evidence_false_when_completed_without_request_ref():
    assert ol.is_duplicate_send_evidence({"completed_at": datetime.now(timezone.utc), "request_ref": "   "}) is False
    assert ol.is_duplicate_send_evidence({"completed_at": datetime.now(timezone.utc), "request_ref": None}) is False


def test_lock_json_serializes_timestamps_and_encodes_evidence():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    out = ol.lock_json(
        {
            "id": 5,
            "identity_key": "k",
            "property_scope": "317 s main st #3",
            "intent_kind": "showing-confirmation",
            "holder": "run-1",
            "request_ref": "AC9",
            "completed": True,
            "acquired_at": now,
            "expires_at": now,
            "completed_at": now,
            "released_at": now,
        }
    )
    assert out["lock_id"] == 5
    assert out["completed_at"] == "2026-07-02T12:00:00+00:00"
    assert out["is_duplicate_send_evidence"] is True


# ---- op runner ----


@pytest.mark.asyncio
async def test_acquire_acquired_returns_lock_id_no_existing():
    patcher, _ = _mock_query([[{"acquired": True, "lock_id": 42}]])
    with patcher:
        out = await ol.run_outbound_lock(None, op="acquire", identity_key="k", intent_kind="showing-confirmation", holder="run-1")
    assert out == {"op": "acquire", "acquired": True, "lock_id": 42, "existing": None}


@pytest.mark.asyncio
async def test_acquire_blocked_returns_full_existing_evidence():
    now = datetime.now(timezone.utc)
    blocking = {
        "id": 7,
        "identity_key": "k",
        "property_scope": "",
        "intent_kind": "x",
        "holder": "other",
        "request_ref": "AC9",
        "completed": True,
        "acquired_at": now,
        "expires_at": now,
        "completed_at": now,
        "released_at": now,
    }
    # call 1: acquire returns blocked with the blocker's lock_id; call 2: fetch full row
    patcher, calls = _mock_query([[{"acquired": False, "lock_id": 7}], [blocking]])
    with patcher:
        out = await ol.run_outbound_lock(None, op="acquire", identity_key="k", intent_kind="x", holder="run-1")
    assert out["acquired"] is False
    assert out["existing"]["lock_id"] == 7
    assert out["existing"]["is_duplicate_send_evidence"] is True
    assert len(calls) == 2  # no extra round-trips beyond the one lookup


@pytest.mark.asyncio
async def test_acquire_requires_fields():
    out = await ol.run_outbound_lock(None, op="acquire", identity_key="k")
    assert "error" in out


@pytest.mark.asyncio
async def test_complete_returns_completed_and_lock():
    now = datetime.now(timezone.utc)
    row = {
        "id": 3,
        "identity_key": "k",
        "property_scope": "",
        "intent_kind": "x",
        "holder": "run-1",
        "request_ref": "AC9",
        "completed": True,
        "acquired_at": now,
        "expires_at": now,
        "completed_at": now,
        "released_at": now,
    }
    patcher, _ = _mock_query([[{"completed": True}], [row]])
    with patcher:
        out = await ol.run_outbound_lock(None, op="complete", lock_id=3, holder="run-1", request_ref="AC9")
    assert out["completed"] is True
    assert out["lock"]["is_duplicate_send_evidence"] is True


@pytest.mark.asyncio
async def test_complete_requires_request_ref():
    out = await ol.run_outbound_lock(None, op="complete", lock_id=3, holder="run-1")
    assert "error" in out


@pytest.mark.asyncio
async def test_release_returns_bool():
    patcher, _ = _mock_query([[{"released": True}]])
    with patcher:
        out = await ol.run_outbound_lock(None, op="release", lock_id=3, holder="run-1")
    assert out == {"op": "release", "released": True}


@pytest.mark.asyncio
async def test_check_lists_locks_and_applies_filters():
    now = datetime.now(timezone.utc)
    row = {
        "id": 1,
        "identity_key": "k",
        "property_scope": "p",
        "intent_kind": "x",
        "holder": "h",
        "request_ref": None,
        "completed": False,
        "acquired_at": now,
        "expires_at": now,
        "completed_at": None,
        "released_at": None,
    }
    patcher, calls = _mock_query([[row]])
    with patcher:
        out = await ol.run_outbound_lock(None, op="check", identity_key="k", property_scope="p", intent_kind="x", days=3)
    assert out["op"] == "check"
    assert out["locks"][0]["is_duplicate_send_evidence"] is False
    # scope + intent filters present -> 4 params (identity, scope, intent, days)
    _query, params = calls[0]
    assert params == ["k", "p", "x", 3]


@pytest.mark.asyncio
async def test_unknown_op_errors():
    out = await ol.run_outbound_lock(None, op="frobnicate")
    assert "error" in out
