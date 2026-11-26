"""Helpers for configuring DNS rebinding protection on the HTTP transports."""

from __future__ import annotations

import logging
import re

from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

# host, host:port, host:*, or bracketed IPv6 with optional port
_HOST_PATTERN = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(:(\d+|\*))?")


def parse_allowed_hosts(value: str) -> list[str]:
    """Parse a comma-separated list of Host header values.

    An entry without a port expands to both ``entry`` and ``entry:*`` so that it
    matches whether or not the client's Host header carries a port.
    """
    result: list[str] = []
    for piece in value.split(","):
        entry = piece.strip().lower()
        if not entry:
            continue
        match = _HOST_PATTERN.fullmatch(entry)
        if not match:
            raise ValueError(
                f"Invalid allowed host {entry!r}: expected a host name or IP address with an optional port "
                "(host, host:port, host:*, or [ipv6]:port), without scheme, path or other wildcards"
            )
        candidates = [entry] if match.group(2) else [entry, f"{entry}:*"]
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    if not result:
        raise ValueError("No allowed hosts given: expected a comma-separated list of host names")
    return result


def build_transport_security(
    current: TransportSecuritySettings | None,
    allowed_hosts: str | None,
    disable_protection: bool,
) -> TransportSecuritySettings | None:
    """Compute the transport security settings for the requested CLI options.

    Returns ``current`` unchanged when no option is given. Allowed hosts are added
    to the existing (localhost) defaults, never substituted for them.
    """
    if disable_protection and allowed_hosts is not None:
        raise ValueError("--allowed-hosts and --disable-dns-rebinding-protection are mutually exclusive")

    if disable_protection:
        logger.warning(
            "DNS rebinding protection is DISABLED: the server accepts requests with any Host or Origin header. "
            "The HTTP transports have no authentication; only do this behind a trusted authenticating proxy."
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    if allowed_hosts is None:
        return current

    extra = parse_allowed_hosts(allowed_hosts)
    if current is not None and current.enable_dns_rebinding_protection:
        base_hosts = list(current.allowed_hosts)
        base_origins = list(current.allowed_origins)
    else:
        base_hosts = list(DEFAULT_ALLOWED_HOSTS)
        base_origins = list(DEFAULT_ALLOWED_ORIGINS)

    hosts = list(dict.fromkeys(base_hosts + extra))
    logger.info(f"DNS rebinding protection enabled; allowed Host headers: {', '.join(hosts)}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=base_origins,
    )
