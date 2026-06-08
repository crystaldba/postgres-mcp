"""AgentPolicy dataclass and registry for per-agent RBAC."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

logger = logging.getLogger(__name__)


class AccessMode(str, Enum):
    """Per-agent access mode, mirrors server-level AccessMode."""

    UNRESTRICTED = "unrestricted"
    RESTRICTED = "restricted"
    READONLY = "readonly"


@dataclass(frozen=True)
class AgentPolicy:
    """Immutable access policy for a single AI agent.

    Each agent is identified by a bearer token sent in the
    ``X-Agent-Token`` HTTP header (or ``AGENT_TOKEN`` MCP meta field).
    The token is stored as a SHA-256 digest so plaintext secrets never
    live in memory after startup.

    Attributes
    ----------
    name:
        Human-readable agent name used in audit logs.
    token_digest:
        SHA-256 hex digest of the raw bearer token.
    access_mode:
        Server-level access mode applied when this agent executes SQL.
    allowed_schemas:
        Set of schema names the agent may query.  ``{"*"}`` means all
        schemas are permitted.
    allowed_tools:
        Set of MCP tool names the agent may call.  ``{"*"}`` means all
        tools are permitted.
    audit:
        When ``True``, every query is emitted as a structured JSON log
        entry at INFO level.
    """

    name: str
    token_digest: str
    access_mode: AccessMode = AccessMode.READONLY
    allowed_schemas: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    allowed_tools: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    audit: bool = True

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_token(
        cls,
        name: str,
        token: str,
        access_mode: AccessMode | str = AccessMode.READONLY,
        allowed_schemas: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        audit: bool = True,
    ) -> "AgentPolicy":
        """Create a policy from a plaintext token (hashed on construction)."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        return cls(
            name=name,
            token_digest=digest,
            access_mode=AccessMode(access_mode),
            allowed_schemas=frozenset(allowed_schemas or ["*"]),
            allowed_tools=frozenset(allowed_tools or ["*"]),
            audit=audit,
        )

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------

    def allows_schema(self, schema: str) -> bool:
        """Return True if *schema* is permitted by this policy."""
        return "*" in self.allowed_schemas or schema in self.allowed_schemas

    def allows_tool(self, tool_name: str) -> bool:
        """Return True if *tool_name* is permitted by this policy."""
        return "*" in self.allowed_tools or tool_name in self.allowed_tools

    def matches_token(self, raw_token: str) -> bool:
        """Constant-time comparison to prevent timing attacks."""
        candidate = hashlib.sha256(raw_token.encode()).hexdigest()
        return hmac.compare_digest(self.token_digest, candidate)


class AgentPolicyRegistry:
    """Thread-safe registry that maps token digests to AgentPolicy objects.

    Usage
    -----
    registry = AgentPolicyRegistry()
    registry.register(AgentPolicy.from_token("bot", "secret-token", "readonly"))
    policy = registry.lookup("secret-token")   # returns AgentPolicy or None
    """

    def __init__(self, default_policy: AgentPolicy | None = None) -> None:
        self._policies: dict[str, AgentPolicy] = {}
        # When no token is supplied the server falls back to this policy.
        # If None, requests without a token are rejected.
        self.default_policy = default_policy

    def register(self, policy: AgentPolicy) -> None:
        """Add or replace a policy in the registry."""
        if policy.token_digest in self._policies:
            logger.warning(
                "Overwriting existing policy for agent '%s'", policy.name
            )
        self._policies[policy.token_digest] = policy
        logger.info("Registered agent policy: name=%s mode=%s", policy.name, policy.access_mode)

    def lookup(self, raw_token: str) -> AgentPolicy | None:
        """Return the policy for *raw_token*, or None if not found."""
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        return self._policies.get(digest)

    def lookup_or_default(self, raw_token: str | None) -> AgentPolicy | None:
        """Return policy for token, fall back to default, or None."""
        if raw_token:
            policy = self.lookup(raw_token)
            if policy is not None:
                return policy
        return self.default_policy

    def __len__(self) -> int:
        return len(self._policies)

    def __repr__(self) -> str:
        names = [p.name for p in self._policies.values()]
        return f"AgentPolicyRegistry(agents={names}, default={self.default_policy})"
