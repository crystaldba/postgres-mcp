"""Load AgentPolicy objects from YAML files or environment variables."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

try:
    import yaml  # PyYAML — already in postgres-mcp deps via pglast extras
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from .agent_policy import AccessMode, AgentPolicy, AgentPolicyRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = {"name", "token"}
_VALID_MODES = {m.value for m in AccessMode}


def _parse_policy_dict(data: dict) -> AgentPolicy:
    """Validate and convert a raw dict (from YAML) into an AgentPolicy."""
    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(f"Agent policy missing required fields: {missing}")

    name: str = data["name"]
    token: str = str(data["token"])

    raw_mode = data.get("access_mode", "readonly")
    if raw_mode not in _VALID_MODES:
        raise ValueError(
            f"Agent '{name}': invalid access_mode '{raw_mode}'. "
            f"Must be one of {sorted(_VALID_MODES)}."
        )

    schemas_raw = data.get("allowed_schemas", ["*"])
    if isinstance(schemas_raw, str):
        schemas_raw = [schemas_raw]

    tools_raw = data.get("allowed_tools", ["*"])
    if isinstance(tools_raw, str):
        tools_raw = [tools_raw]

    audit: bool = bool(data.get("audit", True))

    return AgentPolicy.from_token(
        name=name,
        token=token,
        access_mode=raw_mode,
        allowed_schemas=list(schemas_raw),
        allowed_tools=list(tools_raw),
        audit=audit,
    )


def load_policies_from_yaml(path: str | Path) -> AgentPolicyRegistry:
    """Load an AgentPolicyRegistry from a YAML file.

    Expected format::

        agents:
          - name: reporting-bot
            token: "rpt-abc123"
            access_mode: readonly
            allowed_schemas: ["analytics", "public"]
            allowed_tools: ["execute_sql", "list_objects"]
            audit: true

          - name: migration-agent
            token: "mig-xyz789"
            access_mode: unrestricted
            allowed_schemas: ["*"]
            allowed_tools: ["*"]
            audit: true

        default_access_mode: readonly   # optional — applied to token-less requests
    """
    if yaml is None:
        raise ImportError(
            "PyYAML is required to load agent policies from YAML. "
            "Install it with: pip install pyyaml"
        )

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Agent policy file not found: {path}")

    with path.open() as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Agent policy file must be a YAML mapping, got {type(raw).__name__}")

    registry = AgentPolicyRegistry()

    # Optional default policy for unauthenticated requests
    default_mode = raw.get("default_access_mode")
    if default_mode:
        if default_mode not in _VALID_MODES:
            raise ValueError(f"Invalid default_access_mode '{default_mode}'")
        registry.default_policy = AgentPolicy.from_token(
            name="__default__",
            token="__default__",
            access_mode=default_mode,
            allowed_schemas=["*"],
            allowed_tools=["*"],
            audit=False,
        )
        logger.info("Default (unauthenticated) access mode: %s", default_mode)

    agents_raw = raw.get("agents", [])
    if not isinstance(agents_raw, list):
        raise ValueError("'agents' must be a YAML list")

    for i, agent_data in enumerate(agents_raw):
        try:
            policy = _parse_policy_dict(agent_data)
            registry.register(policy)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Error in agent #{i + 1}: {exc}") from exc

    logger.info("Loaded %d agent policies from %s", len(registry), path)
    return registry


# ---------------------------------------------------------------------------
# Environment variable loader (12-factor / Docker-friendly)
# ---------------------------------------------------------------------------

def load_policies_from_env(env_var: str = "AGENT_POLICIES_JSON") -> AgentPolicyRegistry | None:
    """Load policies from a JSON string in an environment variable.

    Useful for Docker / Kubernetes deployments where mounting a file is
    inconvenient.  Set the env var to a JSON array of policy objects::

        AGENT_POLICIES_JSON='[{"name":"bot","token":"t1","access_mode":"readonly"}]'

    Returns None if the env var is not set.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {env_var}: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(f"{env_var} must be a JSON array of policy objects")

    registry = AgentPolicyRegistry()
    for i, item in enumerate(data):
        try:
            registry.register(_parse_policy_dict(item))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Error in {env_var}[{i}]: {exc}") from exc

    logger.info("Loaded %d agent policies from env var %s", len(registry), env_var)
    return registry
