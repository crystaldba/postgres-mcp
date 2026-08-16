import datetime
import json
from decimal import Decimal
from typing import Any

import pytest

from postgres_mcp import gcf_format
from postgres_mcp.server import ResponseFormat
from postgres_mcp.server import format_text_response

gcf = pytest.importorskip("gcf", reason="gcf-python optional dependency not installed")


def _rows(n: int) -> list[dict[str, Any]]:
    # Non-alphabetical column order is deliberate: it must be preserved end to end.
    return [
        {
            "id": 60 + i,
            "name": f"Customer {i}",
            "country": "US",
            "plan": "pro",
            "active": bool(i % 2),
            "credit_balance": Decimal("33.37"),
            "signup_date": datetime.date(2024, 1, 1 + (i % 27)),
        }
        for i in range(n)
    ]


def test_is_row_list():
    assert gcf_format.is_row_list([{"a": 1}, {"a": 2}])
    assert not gcf_format.is_row_list([])
    assert not gcf_format.is_row_list("a string")
    assert not gcf_format.is_row_list([{"a": 1}, "not a dict"])


def test_encode_rows_factors_header_and_round_trips():
    rows = _rows(20)
    wire = gcf_format.encode_rows(rows)
    assert wire is not None
    assert wire.startswith("GCF profile=generic")
    # The shared field names appear in exactly one header line.
    assert wire.count("{id,name,country,plan,active,credit_balance,signup_date}") == 1

    # Lossless against the JSON-normalized rows (Decimal/date rendered as JSON scalars).
    safe = json.loads(json.dumps(rows, default=str))
    assert gcf.decode_generic(wire) == safe


def test_encode_rows_preserves_column_order():
    wire = gcf_format.encode_rows(_rows(10))
    assert wire is not None
    keys = list(gcf.decode_generic(wire)[0].keys())
    assert keys == ["id", "name", "country", "plan", "active", "credit_balance", "signup_date"]


def test_encode_rows_never_grows_on_tiny_result():
    # A single one-cell row: GCF cannot beat compact JSON, so the encoder declines.
    assert gcf_format.encode_rows([{"n": 1}]) is None


def test_encode_rows_declines_non_serializable():
    class Unserializable:
        pass

    assert gcf_format.encode_rows([{"x": Unserializable()}]) is None


def test_format_text_response_uses_gcf_when_enabled(monkeypatch):
    import postgres_mcp.server as server

    monkeypatch.setattr(server, "current_response_format", ResponseFormat.GCF)
    out = format_text_response(_rows(20))
    assert len(out) == 1
    assert out[0].text.startswith("GCF profile=generic")


def test_format_text_response_stays_text_by_default():
    # Default format is JSON: the payload is rendered as text, never GCF.
    out = format_text_response(_rows(20))
    assert not out[0].text.startswith("GCF profile=generic")


def test_format_text_response_passes_through_non_rows(monkeypatch):
    import postgres_mcp.server as server

    monkeypatch.setattr(server, "current_response_format", ResponseFormat.GCF)
    out = format_text_response("No results")
    assert out[0].text == "No results"
