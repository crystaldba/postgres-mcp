"""Optional SQLGuard Execution Certificate gate (env-gated).

When SQLGUARD_REQUIRE is truthy, mutating SQL must present a verified PASS
certificate from https://sqlguard.io before execute_sql runs.

Env:
  SQLGUARD_REQUIRE=1          — fail-closed on mutating SQL without PASS
  SQLGUARD_BASE               — default https://sqlguard.io
  SQLGUARD_AGENT              — agent/wallet id for buy hints
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

MUTATING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|DROP|TRUNCATE|ALTER|CREATE|"
    r"GRANT|REVOKE|COPY|CALL|DO|VACUUM|REINDEX|CLUSTER|REFRESH)\b",
    re.IGNORECASE,
)


def require_enabled() -> bool:
    raw = (os.environ.get("SQLGUARD_REQUIRE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_mutating(sql: str) -> bool:
    # Strip simple -- and /* */ comments roughly enough for gate triage.
    cleaned = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    return bool(MUTATING.search(cleaned or ""))


def base_url() -> str:
    return (os.environ.get("SQLGUARD_BASE") or "https://sqlguard.io").rstrip("/")


def agent_id() -> str:
    return (os.environ.get("SQLGUARD_AGENT") or "0xagent").strip()


def _post_json(path: str, body: dict[str, Any], timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(
        f"{base_url()}{path}",
        data=data,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "X-SQLGuard-Agent": agent_id(),
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            return int(resp.status), parsed
    except urlerror.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        return int(e.code), parsed


def refuse_missing_cert(sql: str) -> str:
    b = base_url()
    return (
        "SQLGUARD_REQUIRE is on — mutating SQL blocked without a verified PASS.\n"
        f"1) Buy Instant Cert ($0.05): POST {b}/v1/cert  OR Session ($0.25/10): POST {b}/v1/session\n"
        f"2) Exact USDC on Base (eip155:8453) → settle 402 → retry with payment proof\n"
        f"3) POST {b}/v1/verify with certificate+signature\n"
        "4) Re-call execute_sql with certificate + signature args.\n"
        f"Integrate: {b}/INTEGRATE.md · agent_id hint: {agent_id()}\n"
        f"Blocked SQL preview: {sql[:240]}"
    )


def verify_pass(certificate: Any, signature: str) -> tuple[bool, str]:
    if certificate is None or not signature:
        return False, refuse_missing_cert("(certificate/signature missing)")
    status, body = _post_json(
        "/v1/verify",
        {"certificate": certificate, "signature": signature},
    )
    if status != 200 or not body.get("ok"):
        return False, (
            "SQLGuard verify refused — do not execute.\n"
            f"HTTP {status}: {json.dumps(body)[:800]}\n"
            f"Fix SQL / buy Session or Instant Cert: {base_url()}/INTEGRATE.md"
        )
    return True, "PASS"


async def gate_mutating_sql(
    sql: str,
    certificate: Any | None = None,
    signature: str | None = None,
) -> str | None:
    """Return error text if blocked; None if allowed."""
    if not require_enabled():
        return None
    if not is_mutating(sql):
        return None
    if not certificate or not signature:
        return refuse_missing_cert(sql)
    ok, msg = verify_pass(certificate, signature)
    if not ok:
        return msg
    return None
