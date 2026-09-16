"""AWS RDS IAM authentication for the connection pool.

When enabled, the password in the connection URL is replaced with a short-lived
token minted by ``rds:generate-db-auth-token`` instead of a static secret. Tokens
are valid for 15 minutes, so :class:`~postgres_mcp.sql.sql_driver.DbConnPool`
rebuilds its pool with a fresh one before that window closes.
"""

import asyncio
import logging
import os
import re
from typing import Optional
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

logger = logging.getLogger(__name__)

# RDS caps token lifetime at 15 minutes. Rebuild a minute early so a token can
# never expire between the freshness check and the connection attempt.
DEFAULT_TOKEN_TTL_SECONDS = 840

DEFAULT_PORT = 5432

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# e.g. production-db.cr3rkue9ef2z.us-east-1.rds.amazonaws.com
_RDS_HOST_RE = re.compile(r"\.(?P<region>[a-z]{2}(?:-[a-z]+)+-\d+)\.rds\.amazonaws\.com$", re.IGNORECASE)


def iam_auth_enabled() -> bool:
    """Whether to mint IAM auth tokens instead of using the URL's password."""
    return os.environ.get("PG_IAM_AUTH", "").strip().lower() in _TRUTHY


def token_ttl_seconds() -> int:
    """How long a pool may live before it is rebuilt with a fresh token."""
    raw = os.environ.get("PG_IAM_TOKEN_TTL_SECONDS")
    if not raw:
        return DEFAULT_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        logger.warning(f"Ignoring non-integer PG_IAM_TOKEN_TTL_SECONDS={raw!r}; using {DEFAULT_TOKEN_TTL_SECONDS}s")
        return DEFAULT_TOKEN_TTL_SECONDS
    if ttl <= 0:
        logger.warning(f"Ignoring non-positive PG_IAM_TOKEN_TTL_SECONDS={ttl}; using {DEFAULT_TOKEN_TTL_SECONDS}s")
        return DEFAULT_TOKEN_TTL_SECONDS
    return ttl


def _resolve_region(hostname: str) -> str:
    """Region to sign the token for.

    An explicit override wins, then the region embedded in the RDS hostname,
    then the ambient AWS environment. The hostname outranks AWS_REGION because
    the latter is often pointed somewhere else entirely.
    """
    explicit = os.environ.get("PG_IAM_REGION")
    if explicit:
        return explicit

    match = _RDS_HOST_RE.search(hostname)
    if match:
        return match.group("region")

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(var)
        if value:
            return value

    raise ValueError(
        f"Could not determine the AWS region for host {hostname!r}. Set PG_IAM_REGION explicitly.",
    )


def _generate_token(hostname: str, port: int, username: str, region: str) -> str:
    """Mint an RDS IAM auth token. Blocking; call via asyncio.to_thread."""
    try:
        import boto3
    except ImportError as e:
        raise ValueError(
            "PG_IAM_AUTH is enabled but boto3 is not installed. Install the 'iam' extra, e.g. `uv sync --extra iam`.",
        ) from e

    session = boto3.Session(profile_name=os.environ.get("PG_IAM_PROFILE") or None, region_name=region)
    client = session.client("rds", region_name=region)
    return client.generate_db_auth_token(DBHostname=hostname, Port=port, DBUsername=username, Region=region)


def _ensure_sslmode(query: str) -> str:
    """RDS rejects IAM auth over an unencrypted connection, so require TLS."""
    params = parse_qsl(query, keep_blank_values=True)
    if any(key.lower() == "sslmode" for key, _ in params):
        return query
    logger.info("No sslmode in the connection URL; defaulting to sslmode=require, which RDS IAM auth mandates")
    params.append(("sslmode", "require"))
    return urlencode(params)


async def apply_iam_password(connection_url: str) -> str:
    """Return ``connection_url`` with its password replaced by a fresh IAM token."""
    parsed = urlparse(connection_url)

    hostname: Optional[str] = parsed.hostname
    username: Optional[str] = parsed.username
    if not hostname:
        raise ValueError("PG_IAM_AUTH requires a host in the connection URL")
    if not username:
        raise ValueError("PG_IAM_AUTH requires a user in the connection URL (the IAM-mapped database role)")

    port = parsed.port or DEFAULT_PORT
    region = _resolve_region(hostname)

    logger.info(f"Minting an RDS IAM auth token for {username}@{hostname}:{port} in {region}")
    # boto3 is synchronous and this runs on the event loop that serves the MCP
    # stdio transport, so keep the AWS round-trip off it.
    token = await asyncio.to_thread(_generate_token, hostname, port, username, region)

    netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{hostname}:{port}"
    return urlunparse(parsed._replace(netloc=netloc, query=_ensure_sslmode(parsed.query)))
