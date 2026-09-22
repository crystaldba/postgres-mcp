"""Replay production traffic history in disposable PostgreSQL; never send.

Capture uses GATEWAY_BACKTEST_PRODUCTION_DSN in a read-only repeatable-read
transaction. Snapshot removes message bodies, names, and replaces phone numbers
and subject keys. Replay needs only that snapshot, Docker, and this checkout.

uv run python tests/historical/backtest_gateway_traffic.py --capture SNAPSHOT
uv run python tests/historical/backtest_gateway_traffic.py --snapshot SNAPSHOT --report REPORT
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import docker
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postgres_mcp.outbound_gateway.repository import OutboundGatewayRepository
from postgres_mcp.outbound_gateway.traffic_control import check_traffic
from postgres_mcp.sql import SqlDriver

BASELINE_COMMIT = "12ebe21355d393edd287fa97aa7c6bfbb9eba2d1"


def stamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def digits(value):
    return re.sub(r"[^0-9]", "", value) if isinstance(value, str) else ""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
        json.dump(value, output, default=str, indent=2)
        output.write("\n")


def capture(path):
    with psycopg.connect(os.environ["GATEWAY_BACKTEST_PRODUCTION_DSN"], row_factory=dict_row) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='60s'")
        snapshot = {"captured_at": conn.execute("SELECT now() AS time").fetchone()["time"]}
        snapshot["actions"] = conn.execute("""
            SELECT action_id, wakeup_event_id, subject_key, operation, created_at,
                   canonical_context->>'recipient_phone' AS phone,
                   (canonical_context->>'channel_id')::bigint AS channel_id,
                   dispatch_started_at, state, detail_code, provenance
            FROM outbound_actions ORDER BY created_at, action_id
        """).fetchall()
        snapshot["attempts"] = conn.execute("""
            SELECT attempt_id, action_id, created_at, from_state, to_state, event_kind
            FROM outbound_action_attempts ORDER BY created_at, attempt_id
        """).fetchall()
        snapshot["wakes"] = conn.execute("""
            SELECT w.id, w.created_at, w.webui_accepted_at
            FROM hermes_wakeup_events w
            WHERE EXISTS (SELECT 1 FROM outbound_actions a WHERE a.wakeup_event_id=w.id)
        """).fetchall()
        # Fetch only messages inside potential first-decision intervals. The
        # upper bound is the first recorded state event after action creation.
        snapshot["messages"] = conn.execute("""
            SELECT m.id, m.channel_id, m.created_at, m.updated_at, m.direction, m.source,
                   r.payload#>'{data,object,from}' AS sender,
                   r.payload#>'{data,object,to}' AS recipient
            FROM messages m LEFT JOIN raw_events r ON r.id=m.raw_event_id
            WHERE EXISTS (
                SELECT 1 FROM outbound_actions a
                JOIN hermes_wakeup_events w ON w.id=a.wakeup_event_id
                WHERE m.channel_id=(a.canonical_context->>'channel_id')::bigint
                  AND m.created_at>coalesce(w.webui_accepted_at,w.created_at)
                  AND m.created_at<=coalesce((
                      SELECT min(t.created_at) FROM outbound_action_attempts t
                      WHERE t.action_id=a.action_id AND t.created_at>=a.created_at
                  ), a.created_at)
            ) ORDER BY m.created_at,m.id
        """).fetchall()
        conn.rollback()

    # Preserve digit lengths, punctuation, arrays and nulls, so replay exercises
    # actual historical payload shapes without storing customer phone numbers.
    phones = set()

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif digits(value):
            phones.add(digits(value))

    for action in snapshot["actions"]:
        collect(action["phone"])
    for message in snapshot["messages"]:
        collect(message["sender"])
        collect(message["recipient"])
    counters = Counter()
    replacements = {}
    for phone in sorted(phones):
        counters[len(phone)] += 1
        replacement = str(counters[len(phone)]).zfill(len(phone))
        assert len(replacement) == len(phone)
        replacements[phone] = replacement

    def redact(value):
        if isinstance(value, list):
            return [redact(item) for item in value]
        if digits(value):
            replacement = iter(replacements[digits(value)])
            return re.sub(r"[0-9]", lambda _: next(replacement), value)
        return value

    for action in snapshot["actions"]:
        action["phone"] = redact(action["phone"])
        action["subject_key"] = hashlib.sha256(action["subject_key"].encode()).hexdigest()
    for message in snapshot["messages"]:
        message["sender"] = redact(message["sender"])
        message["recipient"] = redact(message["recipient"])
    write_json(path, snapshot)
    print(json.dumps({"captured_actions": len(snapshot["actions"]), "captured_messages": len(snapshot["messages"])}))


def old_repository():
    """Freeze the pre-fix method from git; all its SQL still executes on PG."""
    root = Path(__file__).resolve().parents[2]
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_COMMIT}:src/postgres_mcp/outbound_gateway/repository.py"],
        cwd=root,
        text=True,
    )
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OutboundGatewayRepository")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "newest_activity_after")
    import postgres_mcp.outbound_gateway.repository as module

    namespace = dict(module.__dict__)
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<pre-fix-probe>", "exec"), namespace)
    return type("PreFixRepository", (OutboundGatewayRepository,), {method.name: namespace[method.name]})


async def load_snapshot(conn, snapshot):
    # Applies only to disposable replay DB. Statistics keep temporal views fast.
    await conn.execute("SET jit=off")
    await conn.execute("""
        CREATE TABLE replay_clock (at timestamptz);
        INSERT INTO replay_clock VALUES ('2000-01-01');
        CREATE TABLE action_history (
            action_id uuid PRIMARY KEY, subject_key text, operation text,
            created_at timestamptz, canonical_context jsonb, dispatch_started_at timestamptz
        );
        CREATE TABLE attempt_history (
            attempt_id bigint PRIMARY KEY, action_id uuid, created_at timestamptz, to_state text
        );
        CREATE INDEX ON attempt_history (action_id, created_at DESC, attempt_id DESC);
        CREATE TABLE message_history (
            id bigint PRIMARY KEY, channel_id bigint, created_at timestamptz, direction text, source text
        );
        CREATE TABLE raw_events (id bigint PRIMARY KEY, payload jsonb);
        CREATE TABLE hermes_wakeup_events (id bigint PRIMARY KEY, created_at timestamptz, webui_accepted_at timestamptz);
        CREATE VIEW messages AS
            SELECT m.*, m.id AS raw_event_id, 'historical message'::text AS body
            FROM message_history m, replay_clock c WHERE m.created_at<=c.at;
        CREATE VIEW outbound_actions AS
            SELECT a.action_id,a.subject_key,a.operation,a.created_at,a.canonical_context,
                   '{}'::jsonb AS arguments,
                   coalesce(t.to_state,'received') AS state,
                   CASE WHEN a.dispatch_started_at<=c.at THEN a.dispatch_started_at END AS dispatch_started_at
            FROM action_history a CROSS JOIN replay_clock c
            LEFT JOIN LATERAL (
                SELECT h.to_state FROM attempt_history h
                WHERE h.action_id=a.action_id AND h.created_at<=c.at
                ORDER BY h.created_at DESC,h.attempt_id DESC LIMIT 1
            ) t ON true
            WHERE a.created_at<=c.at;
    """)
    async with conn.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO action_history VALUES (%s,%s,%s,%s,%s,%s)",
            [
                (a["action_id"], a["subject_key"], a["operation"], a["created_at"], Jsonb({"recipient_phone": a["phone"]}), a["dispatch_started_at"])
                for a in snapshot["actions"]
            ],
        )
        await cursor.executemany(
            "INSERT INTO attempt_history VALUES (%s,%s,%s,%s)",
            [(t["attempt_id"], t["action_id"], t["created_at"], t["to_state"]) for t in snapshot["attempts"]],
        )
        await cursor.executemany(
            "INSERT INTO message_history VALUES (%s,%s,%s,%s,%s)",
            [(m["id"], m["channel_id"], m["created_at"], m["direction"], m["source"]) for m in snapshot["messages"]],
        )
        await cursor.executemany(
            "INSERT INTO raw_events VALUES (%s,%s)",
            [(m["id"], Jsonb({"data": {"object": {"from": m["sender"], "to": m["recipient"]}}})) for m in snapshot["messages"]],
        )
        await cursor.executemany(
            "INSERT INTO hermes_wakeup_events VALUES (%s,%s,%s)", [(w["id"], w["created_at"], w["webui_accepted_at"]) for w in snapshot["wakes"]]
        )
    await conn.execute("ANALYZE")


def expected_decision(action, visible_actions, messages):
    # Independent specification: active same-subject action wins; otherwise any
    # actual same-subject send or relevant message makes the reply stale.
    active = {"received", "dependency_wait", "prepared", "dispatching", "provider_accepted", "unknown", "reconciling", "retry_ready"}
    others = [a for a in visible_actions if str(a["action_id"]) != action["action_id"] and a["subject_key"] == action["subject_key"]]
    if any(a["state"] in active for a in others):
        return "lease_held"
    if any(a["created_at"] > action["watermark"] and (a["dispatch_started_at"] or a["state"] == "completed") for a in others):
        return "stale_context"
    if action["operation"] != "quo.sms.send":
        return "stale_context" if messages else "pass"
    target = digits(action["phone"])
    for message in messages:
        values = []
        for endpoint in (message["sender"], message["recipient"]):
            values.extend(endpoint if isinstance(endpoint, list) else [endpoint])
        if (message["source"] or "").lower() in {"quo", "openphone"} and target and target in {digits(v) for v in values}:
            return "stale_context"
    return "pass"


async def replay(snapshot, dsn):
    baseline_class = old_repository()
    wakes = {w["id"]: w for w in snapshot["wakes"]}
    attempts = {}
    for attempt in snapshot["attempts"]:
        attempts.setdefault(attempt["action_id"], []).append(attempt)
    results = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await load_snapshot(conn, snapshot)
        driver = SqlDriver(conn=conn)
        old, fixed = baseline_class(driver), OutboundGatewayRepository(driver)
        for index, original in enumerate(snapshot["actions"]):
            action = dict(original)
            wake = wakes.get(action["wakeup_event_id"])
            if not wake:
                results.append({"action_id": action["action_id"], "inconclusive": ["missing_wake"]})
                continue
            lower = stamp(action["created_at"])
            events = [stamp(t["created_at"]) for t in attempts.get(action["action_id"], []) if stamp(t["created_at"]) >= lower]
            upper = max(lower, min(events) - timedelta(microseconds=1)) if events else lower
            action["watermark"] = stamp(wake["webui_accepted_at"] or wake["created_at"])
            result = {
                "action_id": action["action_id"],
                "wake_id": action["wakeup_event_id"],
                "operation": action["operation"],
                "channel_id": action["channel_id"],
                "subject_key": action["subject_key"],
                "created_at": action["created_at"],
                "recorded_detail": action["detail_code"],
                "inconclusive": [],
            }
            if action["watermark"] > lower:
                result["inconclusive"].append("wake_watermark_later_than_action")
            if not events:
                result["inconclusive"].append("missing_state_history")
            bounds = []
            for at in (lower, upper):
                await conn.execute("UPDATE replay_clock SET at=%s", (at,))
                visible = [
                    m for m in snapshot["messages"] if m["channel_id"] == action["channel_id"] and action["watermark"] < stamp(m["created_at"]) <= at
                ]
                async with conn.cursor(row_factory=dict_row) as cursor:
                    visible_actions = await (await cursor.execute("SELECT * FROM outbound_actions")).fetchall()
                expected = expected_decision(action, visible_actions, visible)
                kwargs = dict(
                    recipient_key=action["subject_key"],
                    channel_id=action["channel_id"],
                    wakeup_event_id=action["wakeup_event_id"],
                    action_id=UUID(action["action_id"]),
                    override=False,
                    logger=logging.getLogger("historical-replay"),
                )
                before = await check_traffic(old, **kwargs)
                after = await check_traffic(fixed, **kwargs)
                bounds.append(
                    {
                        "at": at,
                        "before": before.reason,
                        "after": after.reason,
                        "expected": expected,
                        "check_failed": before.check_failed or after.check_failed,
                        "message_ids": [m["id"] for m in visible],
                    }
                )
                if any(stamp(m["updated_at"]) > at for m in visible if m["updated_at"]):
                    result["inconclusive"].append("message_updated_after_decision")
            if any(bounds[0][key] != bounds[1][key] for key in ("before", "after", "expected", "message_ids")):
                result["inconclusive"].append("activity_changed_inside_decision_interval")
            result["bounds"] = bounds
            result["inconclusive"] = sorted(set(result["inconclusive"]))
            result["matches_expected"] = all(b["after"] == b["expected"] and not b["check_failed"] for b in bounds)
            result["changed"] = any(b["before"] != b["after"] for b in bounds)
            results.append(result)
            if (index + 1) % 100 == 0:
                print(f"Replayed {index + 1}/{len(snapshot['actions'])} historical actions", flush=True)
    eligible = [r for r in results if not r["inconclusive"]]
    return {
        "summary": {
            "historical_actions": len(results),
            "conclusive_actions": len(eligible),
            "inconclusive_actions": len(results) - len(eligible),
            "date_start": snapshot["actions"][0]["created_at"],
            "date_end": snapshot["actions"][-1]["created_at"],
            "operations": dict(Counter(r["operation"] for r in eligible)),
            "distinct_subjects": len({r["subject_key"] for r in eligible}),
            "distinct_channels": len({r["channel_id"] for r in eligible}),
            "transitions": dict(Counter(f"{r['bounds'][0]['before']} -> {r['bounds'][0]['after']}" for r in eligible)),
            "mismatches": sum(not r["matches_expected"] for r in eligible),
            "all_snapshot_mismatches": sum(not r.get("matches_expected", True) for r in results),
            "inconclusive_reasons": dict(Counter(reason for r in results for reason in r["inconclusive"])),
        },
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.capture:
        capture(args.capture)
        return
    if not args.snapshot or not args.report:
        parser.error("Provide --capture or both --snapshot and --report")
    snapshot = json.loads(args.snapshot.read_text())
    client = docker.from_env()
    container = client.containers.run(
        "postgres:16", detach=True, remove=True, environment={"POSTGRES_HOST_AUTH_METHOD": "trust"}, ports={"5432/tcp": ("127.0.0.1", None)}
    )
    try:
        container.reload()
        port = container.ports["5432/tcp"][0]["HostPort"]
        dsn = f"host=127.0.0.1 port={port} user=postgres dbname=postgres"
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        report = asyncio.run(replay(snapshot, dsn))
        report["snapshot_sha256"] = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
        report["baseline_commit"] = BASELINE_COMMIT
        report["fixed_repository_sha256"] = hashlib.sha256(
            (Path(__file__).resolve().parents[2] / "src/postgres_mcp/outbound_gateway/repository.py").read_bytes()
        ).hexdigest()
        write_json(args.report, report)
        print(json.dumps(report["summary"], indent=2))
        if report["summary"]["conclusive_actions"] < 100 or report["summary"]["all_snapshot_mismatches"]:
            raise SystemExit(1)
    finally:
        container.stop(timeout=1)
        client.close()


if __name__ == "__main__":
    main()
