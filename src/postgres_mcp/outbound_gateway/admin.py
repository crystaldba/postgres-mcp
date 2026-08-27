"""Operator-only evidence resolution CLI. Never registered as an MCP tool."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any
from uuid import UUID

from postgres_mcp.sql import DbConnPool
from postgres_mcp.sql import SafeSqlDriver
from postgres_mcp.sql import SqlDriver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comm-outbound-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("action_id")
    resolve.add_argument("--operator", required=True)
    resolve.add_argument(
        "--evidence-kind",
        required=True,
        choices=("authoritative_acceptance", "authoritative_non_acceptance"),
    )
    resolve.add_argument("--evidence-reference", required=True)
    resolve.add_argument("--evidence-hash", required=True)
    resolve.add_argument("--resolution", required=True, choices=("completed", "definitive_failed"))
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--provider-message-id")
    remediate = commands.add_parser("remediate")
    remediate.add_argument("action_id")
    remediate.add_argument("--operator", required=True)
    remediate.add_argument("--reason", required=True)
    return parser


async def _run(args: argparse.Namespace, driver: Any) -> UUID:
    action_id = UUID(args.action_id)
    if args.command == "resolve":
        if len(args.evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in args.evidence_hash):
            raise ValueError("evidence hash must be 64 lowercase hexadecimal characters")
        rows = await SafeSqlDriver.execute_param_query(
            driver,
            """
            SELECT action_id FROM resolve_outbound_action_from_evidence(
                {}, {}, {}, {}, {}, {}, {}, {}, '{{}}'::jsonb
            )
            """,
            [
                action_id,
                args.operator,
                args.evidence_kind,
                args.evidence_reference,
                args.evidence_hash,
                args.resolution,
                args.reason,
                args.provider_message_id,
            ],
        )
    else:
        rows = await SafeSqlDriver.execute_param_query(
            driver,
            "SELECT action_id FROM create_outbound_remediation_context({}, {}, {})",
            [action_id, args.operator, args.reason],
        )
    if not rows:
        raise RuntimeError("operator action returned no durable record")
    return UUID(str(rows[0].cells["action_id"]))


def main() -> None:
    args = build_parser().parse_args()
    database_uri = os.environ.get("DATABASE_URI")
    if not database_uri:
        raise SystemExit("DATABASE_URI is required")
    pool = DbConnPool(database_uri)

    async def execute() -> UUID:
        try:
            return await _run(args, SqlDriver(conn=pool))
        finally:
            await pool.close()

    try:
        action_id = asyncio.run(execute())
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(action_id)
