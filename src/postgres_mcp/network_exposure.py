"""Startup warning for network transports listening beyond loopback.

The sse and streamable-http transports have no authentication, so anyone who
can reach the listening port can call every tool, including execute_sql.
"""

import ipaddress


def is_loopback_host(host: str) -> bool:
    """Return True if a bind address only accepts connections from this machine."""
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def exposure_warning(transport: str, host: str, port: int, restricted: bool) -> str | None:
    """Return the warning to log for a network transport bound to host, or None if host is loopback."""
    if is_loopback_host(host):
        return None
    address = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    sql = "read-only SQL (restricted mode)" if restricted else "arbitrary SQL, including writes and DDL (unrestricted mode)"
    return (
        f"The {transport} transport is listening on {address}, which is not a loopback address, "
        f"and has no authentication: anyone who can connect to this port can run {sql}. "
        "Expose it only on loopback (for Docker: -p 127.0.0.1:<port>:<port>), or behind an "
        "authenticating reverse proxy or VPN. Inside a container this warning appears even when "
        "the port is published on loopback only, because the server cannot see how it is published."
    )
