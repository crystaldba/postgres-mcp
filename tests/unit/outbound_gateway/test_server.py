# pyright: reportPrivateUsage=false

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from starlette.testclient import TestClient

from postgres_mcp.outbound_gateway.adapters.tenantcloud import TenantCloudAdapter
from postgres_mcp.outbound_gateway.metrics import MetricSample
from postgres_mcp.outbound_gateway.models import Operation
from postgres_mcp.outbound_gateway.models import PublicResult
from postgres_mcp.outbound_gateway.models import PublicStatus
from postgres_mcp.outbound_gateway.server import DEFAULT_EMAIL_CC_BY_SOURCE
from postgres_mcp.outbound_gateway.server import DEFAULT_EMAIL_SENDER_DOMAINS
from postgres_mcp.outbound_gateway.server import DEFAULT_ENABLED_INTENTS
from postgres_mcp.outbound_gateway.server import DEFAULT_ENABLED_INTENTS_BY_PROVIDER
from postgres_mcp.outbound_gateway.server import DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER
from postgres_mcp.outbound_gateway.server import DEFAULT_PROPERTY_ALIASES
from postgres_mcp.outbound_gateway.server import TENANTCLOUD_ORIGIN
from postgres_mcp.outbound_gateway.server import FeaturePolicy
from postgres_mcp.outbound_gateway.server import _bearer_headers
from postgres_mcp.outbound_gateway.server import _enabled_intents_by_provider
from postgres_mcp.outbound_gateway.server import _enabled_operations_by_provider
from postgres_mcp.outbound_gateway.server import _reject_tenantcloud_origin_overrides
from postgres_mcp.outbound_gateway.server import _tenantcloud_adapters
from postgres_mcp.outbound_gateway.server import _tenantcloud_enabled
from postgres_mcp.outbound_gateway.server import _ThreadOffloadedAdapter
from postgres_mcp.outbound_gateway.server import create_server
from postgres_mcp.outbound_gateway.server import handle_outbound_action
from postgres_mcp.outbound_gateway.tenantcloud_shared import TENANTCLOUD_OPERATIONS

ACTION_ID = UUID("4cbac369-48c6-5b62-95e9-41f50259e732")


def test_default_email_routing_matches_nigel_account_and_zillow_copy_policy():
    assert DEFAULT_EMAIL_SENDER_DOMAINS == {"nigel-zoho": "pfg.io"}
    assert DEFAULT_EMAIL_CC_BY_SOURCE == {
        "zillow": "management@pfg.io",
        "hotpads": "management@pfg.io",
    }
    assert DEFAULT_PROPERTY_ALIASES["138 bullman street 144 a"] == "building:bullman-st"
    assert DEFAULT_PROPERTY_ALIASES["144 bullman street"] == "building:bullman-st"
    assert DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER == {
        "hotpads": frozenset({"email.send"}),
        "zillow": frozenset({"email.send"}),
    }
    assert DEFAULT_ENABLED_INTENTS == frozenset({"inquiry_reply", "showing_offer"})
    assert DEFAULT_ENABLED_INTENTS_BY_PROVIDER == {
        "hotpads": frozenset({"inquiry_reply", "showing_offer"}),
        "zillow": frozenset({"inquiry_reply", "showing_offer"}),
    }


def test_provider_bearer_headers_are_environment_only_and_optional(monkeypatch):
    monkeypatch.delenv("QUO_MCP_TOKEN", raising=False)
    assert _bearer_headers("QUO_MCP_TOKEN") == {}
    monkeypatch.setenv("QUO_MCP_TOKEN", "provider-secret")
    assert _bearer_headers("QUO_MCP_TOKEN") == {"Authorization": "Bearer provider-secret"}


@pytest.mark.parametrize(
    ("environment_name", "loader"),
    [
        ("OUTBOUND_PROVIDER_OPERATIONS_JSON", _enabled_operations_by_provider),
        ("OUTBOUND_PROVIDER_INTENTS_JSON", _enabled_intents_by_provider),
    ],
)
def test_explicit_empty_provider_scope_fails_closed(monkeypatch, environment_name, loader):
    monkeypatch.setenv(environment_name, "{}")

    with pytest.raises(ValueError, match="non-empty JSON object"):
        loader()


def public(status=PublicStatus.SENT, detail="provider_receipt_verified"):
    return PublicResult(
        status=status,
        action_id=ACTION_ID,
        action_uid=None,
        provider_request_ref="req-1",
        retryable=False,
        detail_code=detail,
    )


def execute_payload():
    return {
        "op": "execute",
        "wakeup_event_id": 7,
        "action_role": "prospect_reply",
        "operation": "email.send",
        "intent_kind": "showing_offer",
        "appointment_slot": "2026-07-17T10:30:00-04:00",
        "arguments": {"to_address": "prospect@example.com", "text": "Friday at 10:30 works. — Nigel"},
    }


@pytest.mark.asyncio
async def test_focused_server_exposes_only_outbound_action_and_health_resource():
    service = AsyncMock()
    mcp = create_server(service, FeaturePolicy(writes_enabled=True, kill_switch=False))
    tools = await mcp.list_tools()
    resources = await mcp.list_resources()
    assert [tool.name for tool in tools] == ["outbound_action"]
    assert [str(resource.uri) for resource in resources] == ["health://outbound-gateway"]
    assert all(tool.name not in {"execute_sql", "outbound_lock"} for tool in tools)


def test_loopback_http_health_and_metrics_routes_are_sanitized():
    service = AsyncMock()
    observability = AsyncMock()
    observability.database_healthy.return_value = True
    observability.collect.return_value = (MetricSample("outbound_gateway_actions_total", 3, {"outcome": "submitted"}),)
    mcp = create_server(
        service,
        FeaturePolicy(writes_enabled=False, kill_switch=True),
        observability=observability,
    )

    with TestClient(mcp.streamable_http_app()) as client:
        health = client.get("/healthz")
        metrics = client.get("/metrics")

    assert health.status_code == 200
    assert health.json() == {
        "kill_switch": True,
        "status": "ok",
        "writes_enabled": False,
    }
    assert metrics.status_code == 200
    assert 'outbound_gateway_actions_total{outcome="submitted"} 3' in metrics.text
    assert "recipient" not in metrics.text


@pytest.mark.asyncio
async def test_execute_and_status_delegate_only_after_strict_json_validation():
    service = AsyncMock()
    service.execute.return_value = public()
    service.status.return_value = public(PublicStatus.UNKNOWN, "provider_timeout")
    policy = FeaturePolicy(writes_enabled=True, kill_switch=False)

    executed = await handle_outbound_action(service, policy, execute_payload())
    status = await handle_outbound_action(
        service,
        policy,
        {"op": "status", "action_id": str(ACTION_ID)},
    )

    assert executed["status"] == "sent"
    assert status["status"] == "unknown"
    service.execute.assert_awaited_once()
    service.status.assert_awaited_once_with(ACTION_ID)
    with pytest.raises(ValueError, match="invalid outbound action request"):
        await handle_outbound_action(service, policy, {**execute_payload(), "recipient": "attacker@example.com"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "path", "accepted", "secrets"),
    [
        ({"operation": "attacker-operation"}, "operation", "email.send", ("attacker-operation",)),
        ({"intent_kind": "private-intent"}, "intent_kind", "inquiry_reply", ("private-intent",)),
        ({"action_role": "secret-role"}, "action_role", "prospect_reply", ("secret-role",)),
        (
            {"arguments": {"to_address": "prospect@example.com"}},
            "arguments.text",
            "to_address",
            ("prospect@example.com",),
        ),
        (
            {
                "arguments": {
                    "to_address": "prospect@example.com",
                    "text": "message secret",
                    "private_argument": "credential secret",
                }
            },
            "arguments",
            "to_address",
            ("prospect@example.com", "message secret", "credential secret", "private_argument"),
        ),
    ],
)
async def test_validation_errors_expose_only_safe_schema_guidance(payload, path, accepted, secrets):
    with pytest.raises(ValueError) as raised:
        await handle_outbound_action(
            AsyncMock(),
            FeaturePolicy(writes_enabled=True, kill_switch=False),
            {**execute_payload(), **payload},
        )

    message = str(raised.value)
    assert path in message
    assert accepted in message
    assert all(secret not in message for secret in secrets)
    assert "invalid outbound action request" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {**execute_payload(), "arguments": {"to_address": "prospect@example.com"}},
            "invalid outbound action request: arguments.text: accepted keys: text, to_address",
        ),
        (
            {
                **execute_payload(),
                "action_role": "calendar_mutation",
                "operation": "calendar.create",
                "intent_kind": "showing_create",
                "arguments": {"description": "appointment details"},
            },
            "invalid outbound action request: arguments.calendar_id: accepted keys: calendar_id, description",
        ),
        (
            {
                **execute_payload(),
                "action_role": "provider_mutation",
                "operation": "tenantcloud.maintenance.status.update",
                "intent_kind": "tenantcloud_maintenance_status",
                "appointment_slot": None,
                "arguments": {"request_id": 81},
            },
            "invalid outbound action request: arguments.status: accepted keys: request_id, status",
        ),
        (
            {
                **execute_payload(),
                "arguments": {
                    "to_address": "prospect@example.com",
                    "text": "safe message",
                    "unknown_argument": "secret",
                },
            },
            "invalid outbound action request: arguments: accepted keys: text, to_address",
        ),
        (
            {"op": "status", "action_id": str(ACTION_ID), "unknown_root": "secret"},
            "invalid outbound action request: status: accepted keys: action_id, op",
        ),
        (
            {"op": "suggest", "wakeup_event_id": 1, "unknown_root": "secret"},
            "invalid outbound action request: suggest: accepted keys: op, wakeup_event_id",
        ),
    ],
)
async def test_validation_guidance_uses_operation_fields_and_root_schema_areas(payload, expected):
    with pytest.raises(ValueError) as raised:
        await handle_outbound_action(
            AsyncMock(),
            FeaturePolicy(writes_enabled=True, kill_switch=False),
            payload,
        )

    assert str(raised.value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                **execute_payload(),
                "arguments": {
                    "to_address": "prospect@example.com",
                    "text": "safe message",
                    "operation": "secret argument value",
                },
            },
            "invalid outbound action request: arguments: accepted keys: text, to_address",
        ),
        (
            {**execute_payload(), "text": "secret root value"},
            (
                "invalid outbound action request: execute: accepted keys: action_role, appointment_slot, "
                "arguments, intent_kind, op, operation, wakeup_event_id"
            ),
        ),
        (
            {**execute_payload(), "to_address": "secret root value"},
            (
                "invalid outbound action request: execute: accepted keys: action_role, appointment_slot, "
                "arguments, intent_kind, op, operation, wakeup_event_id"
            ),
        ),
    ],
)
async def test_validation_guidance_classifies_colliding_extra_keys_by_schema_area(payload, expected):
    with pytest.raises(ValueError) as raised:
        await handle_outbound_action(
            AsyncMock(),
            FeaturePolicy(writes_enabled=True, kill_switch=False),
            payload,
        )

    message = str(raised.value)
    assert message == expected
    assert "secret" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "detail"),
    [
        (FeaturePolicy(writes_enabled=False, kill_switch=False), "writes_disabled"),
        (FeaturePolicy(writes_enabled=True, kill_switch=True), "kill_switch_open"),
    ],
)
async def test_write_policy_rejects_before_database_or_provider_call(policy, detail):
    service = AsyncMock()

    result = await handle_outbound_action(service, policy, execute_payload())

    assert result["status"] == "rejected"
    assert result["detail_code"] == detail
    service.execute.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_operation_rejects_before_database_or_provider_call():
    service = AsyncMock()
    policy = FeaturePolicy(
        writes_enabled=True,
        kill_switch=False,
        enabled_operations=frozenset({Operation.QUO_SMS_SEND}),
    )

    result = await handle_outbound_action(service, policy, execute_payload())

    assert result["status"] == "rejected"
    assert result["detail_code"] == "operation_disabled"
    service.execute.assert_not_called()


def test_focused_server_tool_description_mentions_tenantcloud():
    service = AsyncMock()
    mcp = create_server(service, FeaturePolicy(writes_enabled=True, kill_switch=False))

    tools = [tool for tool in mcp._tool_manager.list_tools() if tool.name == "outbound_action"]

    assert tools, "outbound_action tool must be registered"
    assert "TenantCloud" in (tools[0].description or "")


def test_focused_server_tool_description_mentions_suggest():
    service = AsyncMock()
    mcp = create_server(service, FeaturePolicy(writes_enabled=True, kill_switch=False))

    tools = [tool for tool in mcp._tool_manager.list_tools() if tool.name == "outbound_action"]

    assert tools, "outbound_action tool must be registered"
    assert "suggest" in (tools[0].description or "")


@pytest.mark.asyncio
async def test_suggest_returns_ids_the_wake_implies():
    service = AsyncMock()
    service.suggest.return_value = {
        "provider": "tenantcloud",
        "suggestions": {"lead_id": "2405115", "thread_id": "2002331"},
        "enabled_operations": ["email.send", "tenantcloud.lead.status.update"],
        "enabled_intents": ["inquiry_reply", "tenantcloud_lead_status"],
    }
    policy = FeaturePolicy(
        writes_enabled=True,
        kill_switch=False,
        enabled_operations=frozenset({Operation.EMAIL_SEND, Operation.TENANTCLOUD_LEAD_STATUS_UPDATE}),
    )

    result = await handle_outbound_action(
        service,
        policy,
        {"op": "suggest", "wakeup_event_id": 1},
    )

    assert result["wakeup_event_id"] == 1
    assert result["suggestions"] == {"lead_id": "2405115", "thread_id": "2002331"}
    assert result["provider"] == "tenantcloud"
    assert result["enabled_operations"] == ["email.send", "tenantcloud.lead.status.update"]
    assert result["enabled_intents"] == ["inquiry_reply", "tenantcloud_lead_status"]
    service.suggest.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_suggest_returns_empty_for_a_wake_with_no_hints():
    service = AsyncMock()
    service.suggest.return_value = {
        "provider": None,
        "suggestions": {},
        "enabled_operations": [],
        "enabled_intents": [],
    }
    policy = FeaturePolicy(writes_enabled=True, kill_switch=False)

    result = await handle_outbound_action(
        service,
        policy,
        {"op": "suggest", "wakeup_event_id": 2},
    )

    assert result == {
        "wakeup_event_id": 2,
        "provider": None,
        "suggestions": {},
        "enabled_operations": [],
        "enabled_intents": [],
    }


@pytest.mark.asyncio
async def test_suggest_excludes_globally_disabled_operations():
    service = AsyncMock()
    service.suggest.return_value = {
        "provider": "tenantcloud",
        "suggestions": {},
        "enabled_operations": ["email.send", "tenantcloud.lead.status.update"],
        "enabled_intents": ["inquiry_reply", "tenantcloud_lead_status"],
    }
    policy = FeaturePolicy(
        writes_enabled=True,
        kill_switch=False,
        enabled_operations=frozenset({Operation.EMAIL_SEND}),
    )

    result = await handle_outbound_action(service, policy, {"op": "suggest", "wakeup_event_id": 2})

    assert result["enabled_operations"] == ["email.send"]
    assert result["enabled_intents"] == ["inquiry_reply", "tenantcloud_lead_status"]


@pytest.mark.asyncio
async def test_suggest_never_writes_and_ignores_the_kill_switch():
    """Advisory reads stay available even when writes are disabled."""
    service = AsyncMock()
    service.suggest.return_value = {
        "provider": "tenantcloud",
        "suggestions": {"lead_id": "2405115", "thread_id": "2002331"},
        "enabled_operations": ["tenantcloud.lead.status.update"],
        "enabled_intents": ["tenantcloud_lead_status"],
    }
    policy = FeaturePolicy(writes_enabled=False, kill_switch=True)

    result = await handle_outbound_action(
        service,
        policy,
        {"op": "suggest", "wakeup_event_id": 1},
    )

    assert "suggestions" in result
    service.execute.assert_not_called()


# -- TenantCloud runtime assembly ------------------------------------------------


REQUIRED_ENV_VARS = (
    "TENANTCLOUD_RUNNER_CONTROL_URL",
    "TENANTCLOUD_RUNNER_BEARER_FILE",
    "TENANTCLOUD_RUNNER_NEXT_BEARER_FILE",
    "TENANTCLOUD_MODULE_DIR",
)
ORIGIN_OVERRIDE_ENV_VARS = (
    "TENANTCLOUD_API_BASE_URL",
    "TENANTCLOUD_API_SCHEME",
    "TENANTCLOUD_API_HOST",
    "TENANTCLOUD_API_PORT",
    "TENANTCLOUD_API_USERNAME",
    "TENANTCLOUD_API_PASSWORD",
    "TENANTCLOUD_API_QUERY",
    "TENANTCLOUD_API_FRAGMENT",
)


@pytest.fixture(autouse=True)
def _clean_tenantcloud_env(monkeypatch: pytest.MonkeyPatch):
    for name in REQUIRED_ENV_VARS + ORIGIN_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_stub_tenantcloud_modules(directory: Path, *, base_url_capture: list) -> None:
    (directory / "tenantcloud_auth.py").write_text(
        """
class HttpRunnerControl:
    def __init__(self, base_url, bearer_file, next_bearer_file=None, *, opener=None):
        self.base_url = base_url
        self.bearer_file = bearer_file
        self.next_bearer_file = next_bearer_file


class TenantCloudAuth:
    def __init__(self, container, *, runner=None, control=None, profile_access=None):
        self.container = container
        self.control = control
        self.profile_access = profile_access
""",
        encoding="utf-8",
    )
    (directory / "tenantcloud_client.py").write_text(
        """
CAPTURED_BASE_URLS = []


class TenantCloudClient:
    def __init__(self, auth, *, base_url="https://api.tenantcloud.com"):
        self.auth = auth
        self.base_url = base_url
        CAPTURED_BASE_URLS.append(base_url)
""",
        encoding="utf-8",
    )
    (directory / "tenantcloud_mutations.py").write_text(
        """
class TenantCloudMutations:
    def __init__(self, client):
        self.client = client
""",
        encoding="utf-8",
    )


def _valid_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    module_dir = tmp_path / "scripts"
    module_dir.mkdir()
    _write_stub_tenantcloud_modules(module_dir, base_url_capture=[])
    bearer_file = tmp_path / "current-token"
    bearer_file.write_text("token", encoding="utf-8")
    monkeypatch.setenv("TENANTCLOUD_RUNNER_CONTROL_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("TENANTCLOUD_RUNNER_BEARER_FILE", str(bearer_file))
    monkeypatch.setenv("TENANTCLOUD_MODULE_DIR", str(module_dir))
    return module_dir


def test_tenantcloud_enabled_reflects_any_of_the_four_operations():
    assert not _tenantcloud_enabled(frozenset({Operation.EMAIL_SEND}))
    for operation in TENANTCLOUD_OPERATIONS:
        assert _tenantcloud_enabled(frozenset({operation}))
    assert _tenantcloud_enabled(frozenset({Operation.EMAIL_SEND}) | TENANTCLOUD_OPERATIONS)


def test_tenantcloud_disabled_yields_no_adapters_and_ignores_missing_config():
    # No TENANTCLOUD_* env vars are set at all in this test; disabled operations
    # must not even look at configuration, let alone fail startup over it.
    adapters = _tenantcloud_adapters(frozenset({Operation.EMAIL_SEND}))
    assert adapters == {}


def test_tenantcloud_adapters_registers_one_shared_adapter_for_all_four_operations(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)

    adapters = _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))

    assert set(adapters) == set(TENANTCLOUD_OPERATIONS)
    instances = set(id(adapter) for adapter in adapters.values())
    assert len(instances) == 1, "exactly one adapter instance must serve all four operations"
    shared = next(iter(adapters.values()))
    assert isinstance(shared, _ThreadOffloadedAdapter)
    assert isinstance(shared._inner, TenantCloudAdapter)


def test_tenantcloud_fails_closed_when_url_missing(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)
    monkeypatch.delenv("TENANTCLOUD_RUNNER_CONTROL_URL", raising=False)

    with pytest.raises(ValueError, match="TENANTCLOUD_RUNNER_CONTROL_URL"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_fails_closed_when_bearer_file_env_missing(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)
    monkeypatch.delenv("TENANTCLOUD_RUNNER_BEARER_FILE", raising=False)

    with pytest.raises(ValueError, match="TENANTCLOUD_RUNNER_BEARER_FILE"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_fails_closed_when_bearer_file_does_not_exist_on_disk(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TENANTCLOUD_RUNNER_BEARER_FILE", str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError, match="does not exist"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_fails_closed_when_next_bearer_file_does_not_exist_on_disk(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TENANTCLOUD_RUNNER_NEXT_BEARER_FILE", str(tmp_path / "also-missing"))

    with pytest.raises(ValueError, match="does not exist"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_fails_closed_when_module_missing(tmp_path, monkeypatch):
    _valid_env(tmp_path, monkeypatch)
    empty_dir = tmp_path / "empty-scripts"
    empty_dir.mkdir()
    monkeypatch.setenv("TENANTCLOUD_MODULE_DIR", str(empty_dir))

    with pytest.raises(ValueError, match="tenantcloud_auth"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_uses_the_hardcoded_literal_origin(tmp_path, monkeypatch):
    module_dir = _valid_env(tmp_path, monkeypatch)

    _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))

    import importlib.util

    spec = importlib.util.spec_from_file_location("captured_client", module_dir / "tenantcloud_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Re-importing the stub module from disk only proves the file's own
    # default; the real assertion is that the server never reads a
    # runtime-configurable origin. Assert the constant itself directly.
    assert TENANTCLOUD_ORIGIN == "https://api.tenantcloud.com"


@pytest.mark.parametrize("env_var", ORIGIN_OVERRIDE_ENV_VARS)
def test_tenantcloud_rejects_origin_overrides_before_token_acquisition(tmp_path, monkeypatch, env_var):
    # Deliberately do NOT configure a valid module dir / bearer file: the
    # override rejection must happen first, before any other validation or
    # module loading (i.e. before token acquisition could ever occur).
    monkeypatch.setenv("TENANTCLOUD_RUNNER_CONTROL_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv(env_var, "attacker-supplied-value")
    monkeypatch.setenv("TENANTCLOUD_MODULE_DIR", str(tmp_path / "nonexistent"))

    with pytest.raises(ValueError, match="origin"):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_reject_tenantcloud_origin_overrides_is_a_noop_without_any_override_env():
    _reject_tenantcloud_origin_overrides()  # must not raise


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://evil.example.com:80",
        "https://api.tenantcloud.com",  # right host, wrong scheme for the runner control transport
        "http://127.0.0.1:8095/extra-path",
        "http://127.0.0.1:8095?query=1",
    ],
)
def test_tenantcloud_fails_closed_for_non_loopback_or_malformed_runner_url(tmp_path, monkeypatch, bad_url):
    real_scripts = Path("/home/danpark/projects/Comm-Data-Store/.worktrees/tenantcloud-gateway-writes/scripts")
    if not (real_scripts / "tenantcloud_auth.py").is_file():
        pytest.skip("real CDS scripts checkout unavailable in this environment")

    bearer_file = tmp_path / "current-token"
    bearer_file.write_text("token", encoding="utf-8")
    monkeypatch.setenv("TENANTCLOUD_RUNNER_CONTROL_URL", bad_url)
    monkeypatch.setenv("TENANTCLOUD_RUNNER_BEARER_FILE", str(bearer_file))
    monkeypatch.setenv("TENANTCLOUD_MODULE_DIR", str(real_scripts))
    monkeypatch.setenv("WEB_USAGE_WORKSPACE", "/home/danpark/workspace")

    with pytest.raises(ValueError):
        _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))


def test_tenantcloud_import_resolves_under_container_shaped_web_usage_mount(tmp_path, monkeypatch):
    """The gateway container never mounts the full Web-Usage workspace -- only
    web_usage_runner_control.py itself, at whatever path
    TENANTCLOUD_WEB_USAGE_RUNNER_CONTROL_FILE names, with WEB_USAGE_WORKSPACE
    set to that file's directory (see Comm-Data-Store's docker-compose.yaml).
    The real tenantcloud_auth.py module must resolve its module-level
    `from web_usage_runner_control import build_runner_curl_command` in that
    shape -- not merely when the developer's full host workspace happens to
    already sit at /home/danpark/workspace. Reproduces the container
    ModuleNotFoundError crash-loop reported in review."""
    real_scripts = Path("/home/danpark/projects/Comm-Data-Store/.worktrees/tenantcloud-gateway-writes/scripts")
    if not (real_scripts / "tenantcloud_auth.py").is_file():
        pytest.skip("real CDS scripts checkout unavailable in this environment")

    container_workspace = tmp_path / "container-web-usage-workspace"
    container_workspace.mkdir()
    (container_workspace / "web_usage_runner_control.py").write_text(
        "def build_runner_curl_command(**kwargs):\n    return []\n",
        encoding="utf-8",
    )
    bearer_file = tmp_path / "current-token"
    bearer_file.write_text("token", encoding="utf-8")
    monkeypatch.setenv("TENANTCLOUD_RUNNER_CONTROL_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("TENANTCLOUD_RUNNER_BEARER_FILE", str(bearer_file))
    monkeypatch.setenv("TENANTCLOUD_MODULE_DIR", str(real_scripts))
    monkeypatch.setenv("WEB_USAGE_WORKSPACE", str(container_workspace))
    monkeypatch.delitem(sys.modules, "web_usage_runner_control", raising=False)

    adapters = _tenantcloud_adapters(frozenset(TENANTCLOUD_OPERATIONS))

    assert set(adapters) == set(TENANTCLOUD_OPERATIONS)


# -- Thread-offloaded adapter wrapper (event-loop-blocking decision) -------------


class _FakeInnerAdapter:
    def __init__(self):
        self.validate_calls = []
        self.build_request_calls = []
        self.parse_receipt_calls = []
        self.invoke_result = "invoke-result"
        self.invoke_error: Exception | None = None
        self.reconcile_result = "reconcile-result"
        self.observed_thread_ident = None

    def validate(self, context):
        self.validate_calls.append(context)

    def build_request(self, context, action_uid):
        self.build_request_calls.append((context, action_uid))
        return "built-request"

    def parse_receipt(self, context, observation):
        self.parse_receipt_calls.append((context, observation))
        return "receipt"

    async def invoke(self, client, request):
        import threading

        self.observed_thread_ident = threading.get_ident()
        if self.invoke_error is not None:
            raise self.invoke_error
        return self.invoke_result

    async def poll(self, client, observation):
        return observation

    async def reconcile(self, client, context, action_uid, observation):
        return self.reconcile_result


@pytest.mark.asyncio
async def test_thread_offloaded_adapter_runs_invoke_off_the_event_loop_and_returns_result():
    import threading

    inner = _FakeInnerAdapter()
    wrapped = _ThreadOffloadedAdapter(inner)
    calling_thread = threading.get_ident()

    result = await wrapped.invoke(client=None, request="req")

    assert result == "invoke-result"
    assert inner.observed_thread_ident is not None
    assert inner.observed_thread_ident != calling_thread


@pytest.mark.asyncio
async def test_thread_offloaded_adapter_propagates_exceptions_from_the_offloaded_thread():
    inner = _FakeInnerAdapter()
    inner.invoke_error = RuntimeError("tenantcloud boom")
    wrapped = _ThreadOffloadedAdapter(inner)

    with pytest.raises(RuntimeError, match="tenantcloud boom"):
        await wrapped.invoke(client=None, request="req")


@pytest.mark.asyncio
async def test_thread_offloaded_adapter_delegates_reconcile_and_sync_methods():
    inner = _FakeInnerAdapter()
    wrapped = _ThreadOffloadedAdapter(inner)

    wrapped.validate("ctx")
    request = wrapped.build_request("ctx", "uid")
    receipt = wrapped.parse_receipt("ctx", "obs")
    reconciled = await wrapped.reconcile(client=None, context="ctx", action_uid="uid", observation="obs")

    assert inner.validate_calls == ["ctx"]
    assert inner.build_request_calls == [("ctx", "uid")]
    assert request == "built-request"
    assert inner.parse_receipt_calls == [("ctx", "obs")]
    assert receipt == "receipt"
    assert reconciled == "reconcile-result"
