"""Optional Graph Compact Format (GCF) encoding for record-shaped tool results.

Opt-in via ``--response-format=gcf`` (or ``POSTGRES_MCP_RESPONSE_FORMAT=gcf``). When
enabled, a tool result that is a list of uniform row objects (for example the rows from
``execute_sql`` or the schema-listing tools) is encoded as a GCF generic-profile block
(https://gcformat.com) instead of text: the repeated column names are factored into a
single header, so the result costs fewer tokens when it crosses the LLM boundary.

The substitution is conservative and never changes what a model can read:

* row values are first normalized to JSON-native scalars (the same coercion a JSON
  response applies: ``Decimal``/``date``/``datetime`` become strings, etc.);
* GCF is used only when it encodes without error, is strictly smaller than the compact
  JSON of the same rows (never-grow), and decodes back to those rows (lossless).

Any failure of those checks returns ``None`` and the caller keeps its existing text, so
enabling GCF can only shrink a row result, never grow or alter one. ``gcf-python`` is an
optional dependency; if it is not installed the encoder is inert and JSON text is used.
"""

import json
from typing import Any


def available() -> bool:
    """Return True when the optional gcf-python dependency is importable."""
    try:
        import gcf  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def is_row_list(value: Any) -> bool:
    """Return True when value is a non-empty list of dict rows (the encodable shape)."""
    return isinstance(value, list) and len(value) > 0 and all(isinstance(row, dict) for row in value)


def encode_rows(rows: list[dict[str, Any]]) -> str | None:
    """Return a GCF generic-profile wire for rows, or None to keep the text response.

    None whenever gcf-python is unavailable, the rows are not JSON-serializable, encoding
    errors, the wire is not strictly smaller than compact JSON (never-grow), or the wire
    does not decode back to the same rows (lossless). Callers treat None as "send text".
    """
    try:
        import gcf  # type: ignore
    except ImportError:
        return None

    # Normalize to JSON-native scalars so DB-native cell types (Decimal, date, datetime,
    # UUID, ...) encode the same way a JSON response would render them.
    try:
        safe_rows = json.loads(json.dumps(rows, default=str))
    except (TypeError, ValueError):
        return None
    if not is_row_list(safe_rows):
        return None

    try:
        wire = gcf.encode_generic(safe_rows)
    except Exception:
        return None

    # Never-grow: compare against the compact JSON the same rows would produce.
    baseline = json.dumps(safe_rows, separators=(",", ":"))
    if len(wire) >= len(baseline):
        return None

    # Fail-safe: require a lossless round-trip back to the normalized rows.
    try:
        if gcf.decode_generic(wire) != safe_rows:
            return None
    except Exception:
        return None

    return wire
