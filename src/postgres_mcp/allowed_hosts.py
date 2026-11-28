"""Host header allowlist for the sse and streamable-http transports.

The server answers HTTP 421 "Invalid Host header" to any request whose Host
header is not on the list, whatever address it is bound to. The list always
contains the localhost defaults; --allowed-hosts can only add to it.
"""

import argparse
import ipaddress
import re
from collections.abc import Iterable

from mcp.server.transport_security import TransportSecuritySettings

# The same values the MCP SDK installs for a server constructed on loopback.
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
DEFAULT_ALLOWED_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")

_LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
_NAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_PORT = re.compile(r"[0-9]{1,5}")

_EXPECTED = "expected NAME, NAME:PORT or NAME:*, where NAME is a host name, an IPv4 address or a bracketed IPv6 address"


def _expand_entry(entry: str) -> list[str]:
    """Turn one validated, lowercased entry into the Host header patterns it allows."""
    if "*" in entry.removesuffix(":*"):
        raise ValueError("wildcards are only supported as the port (NAME:*); list each name")
    if "/" in entry:
        raise ValueError("give a host name, not a URL or path")

    if entry.startswith("["):
        address, bracket, rest = entry[1:].partition("]")
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            raise ValueError(_EXPECTED) from None
        if not bracket or (rest and not rest.startswith(":")):
            raise ValueError(_EXPECTED)
        name = f"[{address}]"
        port = rest[1:] if rest else None
    else:
        name, colon, port = entry.partition(":")
        if not _NAME.fullmatch(name):
            raise ValueError(_EXPECTED)
        port = port if colon else None

    if port is None:
        return [name, f"{name}:*"]
    if port == "*":
        return [f"{name}:*"]
    if not _PORT.fullmatch(port) or not 1 <= int(port) <= 65535:
        raise ValueError(f"port must be a number from 1 to 65535 or *; {_EXPECTED}")
    return [f"{name}:{int(port)}"]


def parse_allowed_hosts(value: str) -> list[str]:
    """Parse a comma-separated --allowed-hosts value into Host header patterns.

    NAME allows NAME with or without a port, NAME:PORT allows only that port,
    and NAME:* allows NAME with any port (but not without one). Entries are
    lowercased. Raises argparse.ArgumentTypeError naming the first bad entry.
    """
    patterns: list[str] = []
    for raw in value.split(","):
        entry = raw.strip()
        try:
            if not entry:
                raise ValueError("entry is empty")
            expanded = _expand_entry(entry.lower())
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid entry {entry!r}: {e}") from None
        patterns.extend(p for p in expanded if p not in patterns)
    return patterns


def host_check_settings(extra_hosts: Iterable[str] = ()) -> TransportSecuritySettings:
    """Build the Host header allowlist: the localhost defaults plus extra_hosts.

    Allowed Origins are always the localhost defaults.
    """
    hosts: list[str] = list(DEFAULT_ALLOWED_HOSTS)
    hosts.extend(h for h in extra_hosts if h not in hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,  # SDK field that turns the Host and Origin checks on
        allowed_hosts=hosts,
        allowed_origins=list(DEFAULT_ALLOWED_ORIGINS),
    )
