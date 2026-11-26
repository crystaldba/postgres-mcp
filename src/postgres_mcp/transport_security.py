"""Helpers for configuring DNS rebinding protection on the HTTP transports."""

from __future__ import annotations

import ipaddress
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


def is_loopback_host(host: str) -> bool:
    """True when a bind address only accepts connections from the local machine."""
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def warn_if_network_exposed(transport: str, host: str, port: int, access_mode: str) -> None:
    """Warn when an unauthenticated HTTP transport is bound to a non-loopback address.

    The sse and streamable-http transports have no authentication, so anyone who can
    reach the port can use the configured database credentials. This is easy to do by
    accident (for example ``0.0.0.0``, or publishing a container port on all interfaces).
    """
    if is_loopback_host(host):
        return
    consequence = "run arbitrary SQL" if access_mode == "unrestricted" else "run read-only SQL"
    logger.warning(
        f"The {transport} transport is bound to {host}:{port} and has no authentication: anyone who can reach this "
        f"port can {consequence} with the configured database credentials. Only expose it on a trusted network, or "
        "put an authenticating reverse proxy or VPN in front. When running in a container, publish the port on "
        "loopback only (for example docker run -p 127.0.0.1:8000:8000)."
    )
