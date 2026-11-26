"""Warn when an unauthenticated HTTP transport is exposed beyond the local machine."""

import ipaddress
import logging

logger = logging.getLogger(__name__)


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
