from __future__ import annotations

import pytest

from postgres_mcp.outbound_gateway.admin import _run
from postgres_mcp.outbound_gateway.admin import build_parser


def test_admin_requires_operator_and_authoritative_evidence():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["resolve", "00000000-0000-0000-0000-000000000001"])
    args = parser.parse_args(
        [
            "resolve",
            "00000000-0000-0000-0000-000000000001",
            "--operator",
            "danpark",
            "--evidence-kind",
            "authoritative_acceptance",
            "--evidence-reference",
            "provider-message-1",
            "--evidence-hash",
            "a" * 64,
            "--resolution",
            "completed",
            "--reason",
            "verified in provider history",
        ]
    )
    assert args.operator == "danpark"
    assert args.resolution == "completed"


def test_admin_remediation_accepts_no_recipient_or_provider_override():
    parser = build_parser()
    args = parser.parse_args(
        [
            "remediate",
            "00000000-0000-0000-0000-000000000001",
            "--operator",
            "danpark",
            "--reason",
            "provider proved first action failed",
        ]
    )
    assert args.command == "remediate"
    assert not hasattr(args, "recipient")
    assert not hasattr(args, "provider")


@pytest.mark.asyncio
async def test_admin_resolution_query_survives_literal_empty_json_object():
    class Row:
        cells = {"action_id": "00000000-0000-0000-0000-000000000001"}

    class Driver:
        async def execute_query(self, query, *args, **kwargs):
            assert "'{}'::jsonb" in query
            return [Row()]

    args = build_parser().parse_args(
        [
            "resolve",
            "00000000-0000-0000-0000-000000000001",
            "--operator",
            "danpark",
            "--evidence-kind",
            "authoritative_acceptance",
            "--evidence-reference",
            "provider-message-1",
            "--evidence-hash",
            "a" * 64,
            "--resolution",
            "completed",
            "--reason",
            "verified in provider history",
        ]
    )

    assert str(await _run(args, Driver())) == "00000000-0000-0000-0000-000000000001"
