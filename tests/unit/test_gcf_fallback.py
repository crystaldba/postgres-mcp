"""Fallback coverage for when the optional gcf-python dependency is NOT installed.

Unlike test_gcf_format.py (which importorskips gcf and exercises the encode path), this
file deliberately does not skip: it simulates gcf being absent so the graceful-degradation
path is covered even in a default install without the ``[gcf]`` extra.
"""

import sys
from unittest.mock import patch

from postgres_mcp import gcf_format
from postgres_mcp.server import ResponseFormat
from postgres_mcp.server import format_text_response

# Setting sys.modules["gcf"] = None makes `import gcf` raise ImportError, simulating a
# default install where the optional dependency was never installed.
_GCF_ABSENT = patch.dict(sys.modules, {"gcf": None})


def test_available_is_false_when_gcf_absent():
    with _GCF_ABSENT:
        assert gcf_format.available() is False


def test_encode_rows_returns_none_when_gcf_absent():
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    with _GCF_ABSENT:
        assert gcf_format.encode_rows(rows) is None


def test_format_text_response_falls_back_to_text_when_gcf_absent(monkeypatch):
    # Even with GCF selected, an absent dependency must degrade to the normal text path.
    import postgres_mcp.server as server

    monkeypatch.setattr(server, "current_response_format", ResponseFormat.GCF)
    rows = [{"id": i, "name": f"row {i}"} for i in range(20)]
    with _GCF_ABSENT:
        out = format_text_response(rows)
    assert len(out) == 1
    assert not out[0].text.startswith("GCF profile=generic")
