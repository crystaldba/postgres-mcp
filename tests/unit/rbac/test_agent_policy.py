"""Unit tests for per-agent RBAC: AgentPolicy, AgentPolicyRegistry, policy_loader."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from postgres_mcp.rbac.agent_policy import AccessMode, AgentPolicy, AgentPolicyRegistry
from postgres_mcp.rbac.policy_loader import load_policies_from_env, load_policies_from_yaml
from postgres_mcp.rbac.rbac_sql_driver import (
    RbacSqlDriver,
    SchemaViolationError,
    ToolViolationError,
)


# ---------------------------------------------------------------------------
# AgentPolicy
# ---------------------------------------------------------------------------

class TestAgentPolicy:
    def test_from_token_defaults(self):
        p = AgentPolicy.from_token("bot", "secret")
        assert p.name == "bot"
        assert p.access_mode == AccessMode.READONLY
        assert "*" in p.allowed_schemas
        assert "*" in p.allowed_tools
        assert p.audit is True

    def test_token_is_hashed(self):
        p = AgentPolicy.from_token("bot", "my-plaintext-token")
        assert "my-plaintext-token" not in p.token_digest
        assert len(p.token_digest) == 64  # SHA-256 hex

    def test_matches_token_correct(self):
        p = AgentPolicy.from_token("bot", "correct-token")
        assert p.matches_token("correct-token") is True

    def test_matches_token_wrong(self):
        p = AgentPolicy.from_token("bot", "correct-token")
        assert p.matches_token("wrong-token") is False

    def test_allows_schema_wildcard(self):
        p = AgentPolicy.from_token("bot", "t", allowed_schemas=["*"])
        assert p.allows_schema("any_schema") is True

    def test_allows_schema_specific(self):
        p = AgentPolicy.from_token("bot", "t", allowed_schemas=["analytics", "public"])
        assert p.allows_schema("analytics") is True
        assert p.allows_schema("secrets") is False

    def test_allows_tool_wildcard(self):
        p = AgentPolicy.from_token("bot", "t", allowed_tools=["*"])
        assert p.allows_tool("execute_sql") is True

    def test_allows_tool_specific(self):
        p = AgentPolicy.from_token("bot", "t", allowed_tools=["execute_sql", "list_objects"])
        assert p.allows_tool("execute_sql") is True
        assert p.allows_tool("run_health_check") is False

    def test_access_mode_string_conversion(self):
        p = AgentPolicy.from_token("bot", "t", access_mode="unrestricted")
        assert p.access_mode == AccessMode.UNRESTRICTED

    def test_invalid_access_mode_raises(self):
        with pytest.raises(ValueError):
            AgentPolicy.from_token("bot", "t", access_mode="superuser")


# ---------------------------------------------------------------------------
# AgentPolicyRegistry
# ---------------------------------------------------------------------------

class TestAgentPolicyRegistry:
    def test_register_and_lookup(self):
        registry = AgentPolicyRegistry()
        p = AgentPolicy.from_token("bot", "tok1")
        registry.register(p)
        found = registry.lookup("tok1")
        assert found is p

    def test_lookup_unknown_token_returns_none(self):
        registry = AgentPolicyRegistry()
        assert registry.lookup("no-such-token") is None

    def test_lookup_or_default_falls_back(self):
        default = AgentPolicy.from_token("default", "d", access_mode="readonly")
        registry = AgentPolicyRegistry(default_policy=default)
        result = registry.lookup_or_default(None)
        assert result is default

    def test_lookup_or_default_no_default_returns_none(self):
        registry = AgentPolicyRegistry()
        assert registry.lookup_or_default(None) is None

    def test_len(self):
        registry = AgentPolicyRegistry()
        registry.register(AgentPolicy.from_token("a", "t1"))
        registry.register(AgentPolicy.from_token("b", "t2"))
        assert len(registry) == 2


# ---------------------------------------------------------------------------
# policy_loader — YAML
# ---------------------------------------------------------------------------

YAML_CONTENT = textwrap.dedent("""
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
        audit: false

    default_access_mode: readonly
""")


class TestPolicyLoader:
    def test_load_yaml(self, tmp_path: Path):
        policy_file = tmp_path / "agents.yaml"
        policy_file.write_text(YAML_CONTENT)

        registry = load_policies_from_yaml(policy_file)
        assert len(registry) == 2

        rpt = registry.lookup("rpt-abc123")
        assert rpt is not None
        assert rpt.name == "reporting-bot"
        assert rpt.access_mode == AccessMode.READONLY
        assert "analytics" in rpt.allowed_schemas
        assert rpt.audit is True

        mig = registry.lookup("mig-xyz789")
        assert mig is not None
        assert mig.access_mode == AccessMode.UNRESTRICTED
        assert mig.audit is False

    def test_yaml_default_policy(self, tmp_path: Path):
        policy_file = tmp_path / "agents.yaml"
        policy_file.write_text(YAML_CONTENT)
        registry = load_policies_from_yaml(policy_file)
        assert registry.default_policy is not None
        assert registry.default_policy.access_mode == AccessMode.READONLY

    def test_yaml_missing_required_field(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("agents:\n  - name: bot\n")
        with pytest.raises(ValueError, match="missing required fields"):
            load_policies_from_yaml(bad)

    def test_yaml_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_policies_from_yaml("/no/such/file.yaml")

    def test_load_from_env(self, monkeypatch):
        data = [{"name": "env-bot", "token": "env-tok", "access_mode": "restricted"}]
        monkeypatch.setenv("AGENT_POLICIES_JSON", json.dumps(data))
        registry = load_policies_from_env("AGENT_POLICIES_JSON")
        assert registry is not None
        assert len(registry) == 1
        p = registry.lookup("env-tok")
        assert p is not None
        assert p.access_mode == AccessMode.RESTRICTED

    def test_load_from_env_not_set_returns_none(self, monkeypatch):
        monkeypatch.delenv("AGENT_POLICIES_JSON", raising=False)
        assert load_policies_from_env("AGENT_POLICIES_JSON") is None


# ---------------------------------------------------------------------------
# RbacSqlDriver
# ---------------------------------------------------------------------------

class TestRbacSqlDriver:
    def _make_driver(self, **policy_kwargs):
        base = MagicMock()
        base.execute_query = AsyncMock(return_value=[])
        policy = AgentPolicy.from_token(
            name="test-agent",
            token="test-token",
            **policy_kwargs,
        )
        driver = RbacSqlDriver(sql_driver=base, policy=policy)
        # Bypass inner driver wrapping for unit tests
        driver._effective_driver = base
        return driver, base

    @pytest.mark.asyncio
    async def test_allowed_tool_passes(self):
        driver, base = self._make_driver(
            access_mode="unrestricted",
            allowed_tools=["execute_sql"],
        )
        await driver.execute_query("SELECT 1", tool_name="execute_sql")
        base.execute_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_blocked_tool_raises(self):
        driver, _ = self._make_driver(
            access_mode="unrestricted",
            allowed_tools=["execute_sql"],
        )
        with pytest.raises(ToolViolationError):
            await driver.execute_query("SELECT 1", tool_name="run_health_check")

    @pytest.mark.asyncio
    async def test_schema_violation_raises(self):
        driver, _ = self._make_driver(
            access_mode="unrestricted",
            allowed_schemas=["public"],
        )
        with pytest.raises(SchemaViolationError):
            await driver.execute_query("SELECT * FROM secrets.passwords")

    @pytest.mark.asyncio
    async def test_allowed_schema_passes(self):
        driver, base = self._make_driver(
            access_mode="unrestricted",
            allowed_schemas=["public"],
        )
        await driver.execute_query("SELECT * FROM public.users")
        base.execute_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_wildcard_schema_allows_all(self):
        driver, base = self._make_driver(
            access_mode="unrestricted",
            allowed_schemas=["*"],
        )
        await driver.execute_query("SELECT * FROM any_schema.any_table")
        base.execute_query.assert_called_once()
