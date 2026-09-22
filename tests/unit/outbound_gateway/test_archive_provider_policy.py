# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest

from postgres_mcp.outbound_gateway.archive.provider_policy import _enabled_intents_by_provider
from postgres_mcp.outbound_gateway.archive.provider_policy import _enabled_operations_by_provider


@pytest.mark.parametrize(
    ("environment_name", "loader"),
    [
        ("OUTBOUND_PROVIDER_OPERATIONS_JSON", _enabled_operations_by_provider),
        ("OUTBOUND_PROVIDER_INTENTS_JSON", _enabled_intents_by_provider),
    ],
)
def test_explicit_empty_provider_scope_fails_closed(monkeypatch, environment_name, loader):
    monkeypatch.setenv(environment_name, "{}")

    with pytest.raises(ValueError, match="non-empty JSON object"):
        loader()
