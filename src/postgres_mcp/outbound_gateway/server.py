"""Focused MCP surface and runtime assembly for outbound actions."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import ModuleType
from typing import Any
from typing import Coroutine
from typing import Mapping
from uuid import uuid5

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse

from postgres_mcp.sql import DbConnPool
from postgres_mcp.sql import SqlDriver

from .adapters.base import ProviderAdapter
from .adapters.calendar import CalendarAdapter
from .adapters.cliq import CliqAdapter
from .adapters.email import EmailAdapter
from .adapters.quo import QuoSmsAdapter
from .adapters.tenantcloud import TenantCloudAdapter
from .context import ACTION_NAMESPACE
from .context import ActionContextLoader
from .context import RoutingPolicy
from .evidence import DatabasePreflightEvidenceLoader
from .metrics import GatewayObservability
from .metrics import render_prometheus
from .models import ARGUMENT_MODELS
from .models import ActionRole
from .models import ExecuteRequest
from .models import IntentKind
from .models import Operation
from .models import PublicResult
from .models import PublicStatus
from .models import StatusRequest
from .models import SuggestRequest
from .models import parse_outbound_request
from .provider_client import McpProviderClient
from .provider_client import McpServerConfig
from .repository import OutboundGatewayRepository
from .service import OutboundActionService
from .store import PostgresActionStore
from .tenantcloud_shared import TENANTCLOUD_OPERATIONS
from .worker import OutboundWorker

# TenantCloud's API origin is a fixed literal, never a runtime-configurable
# value. Task 7's adapter and this module both depend on this exact string;
# nothing in this file ever reads an environment variable to build it.
TENANTCLOUD_ORIGIN = "https://api.tenantcloud.com"

# Defense in depth: these variable names do not correspond to anything this
# module reads to build the origin above. Their presence almost certainly
# means an operator believes they can retarget the TenantCloud origin through
# configuration. Fail closed and loudly instead of silently ignoring the
# attempt -- checked first, before any module loading or token acquisition.
_TENANTCLOUD_ORIGIN_OVERRIDE_ENV_VARS = (
    "TENANTCLOUD_API_BASE_URL",
    "TENANTCLOUD_API_SCHEME",
    "TENANTCLOUD_API_HOST",
    "TENANTCLOUD_API_PORT",
    "TENANTCLOUD_API_USERNAME",
    "TENANTCLOUD_API_PASSWORD",
    "TENANTCLOUD_API_QUERY",
    "TENANTCLOUD_API_FRAGMENT",
)

DEFAULT_EMAIL_SENDER_DOMAINS = {"nigel-zoho": "pfg.io"}
DEFAULT_EMAIL_CC_BY_SOURCE = {
    "zillow": "management@pfg.io",
    "hotpads": "management@pfg.io",
}
DEFAULT_PROPERTY_ALIASES = {
    "138 bullman street 144 a": "building:bullman-st",
    "144 bullman street": "building:bullman-st",
    "16 north main street 16": "building:16-n-main",
}
DEFAULT_ENABLED_OPERATIONS = frozenset({Operation.EMAIL_SEND})
DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER = {
    "hotpads": frozenset({Operation.EMAIL_SEND.value}),
    "zillow": frozenset({Operation.EMAIL_SEND.value}),
}
DEFAULT_ENABLED_INTENTS = frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value})
DEFAULT_ENABLED_INTENTS_BY_PROVIDER = {
    "hotpads": frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value}),
    "zillow": frozenset({IntentKind.INQUIRY_REPLY.value, IntentKind.SHOWING_OFFER.value}),
}


@dataclass(frozen=True)
class FeaturePolicy:
    writes_enabled: bool
    kill_switch: bool
    enabled_operations: frozenset[Operation] = DEFAULT_ENABLED_OPERATIONS


@dataclass(frozen=True)
class GatewayRuntime:
    pool: DbConnPool
    service: OutboundActionService
    store: PostgresActionStore
    policy: FeaturePolicy
    observability: GatewayObservability


_REQUEST_OPERATIONS = tuple(sorted(operation.value for operation in Operation))
_REQUEST_INTENTS = tuple(sorted(intent.value for intent in IntentKind))
_REQUEST_ROLES = tuple(sorted(role.value for role in ActionRole))
_REQUEST_ARGUMENT_KEYS_BY_OPERATION = {
    operation: tuple(sorted(model.model_fields)) for operation, model in ARGUMENT_MODELS.items()
}
_REQUEST_TOP_LEVEL_KEYS = {
    "execute": ("action_role", "appointment_slot", "arguments", "intent_kind", "op", "operation", "wakeup_event_id"),
    "status": ("action_id", "op"),
    "suggest": ("op", "wakeup_event_id"),
    "request": ("action_id", "action_role", "appointment_slot", "arguments", "intent_kind", "op", "operation", "wakeup_event_id"),
}
_REQUEST_ENUM_VALUES = {
    "operation": _REQUEST_OPERATIONS,
    "intent_kind": _REQUEST_INTENTS,
    "action_role": _REQUEST_ROLES,
}


def _request_type_from_location(location: tuple[object, ...], fallback: str) -> str:
    candidate = location[0] if location else None
    if isinstance(candidate, str) and candidate in _REQUEST_TOP_LEVEL_KEYS:
        return candidate
    return fallback


def _arguments_have_extra_fields(arguments: object, operation: Operation | None) -> bool:
    if operation is None or not isinstance(arguments, Mapping):
        return False
    try:
        ARGUMENT_MODELS[operation].model_validate(arguments)
    except ValidationError as error:
        return any(detail.get("type") == "extra_forbidden" for detail in error.errors(include_input=False))
    return False


def _safe_validation_message(error: ValidationError, request: Mapping[str, Any]) -> str:
    """Render trusted schema metadata without rendering request/error data."""
    raw_request_type = request.get("op")
    request_type = (
        raw_request_type
        if isinstance(raw_request_type, str) and raw_request_type in _REQUEST_TOP_LEVEL_KEYS
        else "request"
    )
    try:
        operation = Operation(request.get("operation"))
    except (TypeError, ValueError):
        operation = None
        argument_keys: tuple[str, ...] | None = None
    else:
        argument_keys = _REQUEST_ARGUMENT_KEYS_BY_OPERATION[operation]
    arguments = request.get("arguments")
    guidance: list[str] = []
    for detail in error.errors(include_input=False):
        location = detail.get("loc", ())
        error_type = detail.get("type")
        location_request_type = _request_type_from_location(location, request_type)
        if error_type == "extra_forbidden":
            if location_request_type == "execute" and _arguments_have_extra_fields(arguments, operation):
                guidance.append(f"arguments: accepted keys: {', '.join(argument_keys or ())}")
            else:
                guidance.append(
                    f"{location_request_type}: accepted keys: {', '.join(_REQUEST_TOP_LEVEL_KEYS[location_request_type])}"
                )
            continue
        known_enum = next((part for part in location if part in _REQUEST_ENUM_VALUES), None)
        if known_enum is not None:
            values = ", ".join(_REQUEST_ENUM_VALUES[known_enum])
            guidance.append(f"{known_enum}: accepted values: {values}")
            continue
        known_argument = next((part for part in location if argument_keys is not None and part in argument_keys), None)
        if known_argument is not None and error_type == "missing" and argument_keys is not None:
            guidance.append(f"arguments.{known_argument}: accepted keys: {', '.join(argument_keys)}")
            continue
        if error_type == "union_tag_invalid":
            guidance.append("op: accepted values: execute, status, suggest")
            continue
        if error_type == "missing":
            guidance.append(f"{request_type}: accepted keys: {', '.join(_REQUEST_TOP_LEVEL_KEYS[request_type])}")
    unique_guidance = list(dict.fromkeys(guidance))
    if not unique_guidance:
        return "invalid outbound action request"
    return f"invalid outbound action request: {'; '.join(unique_guidance)}"


async def handle_outbound_action(
    service: OutboundActionService,
    policy: FeaturePolicy,
    request: dict[str, Any],
) -> dict[str, Any]:
    try:
        parsed = parse_outbound_request(request)
    except ValidationError as exc:
        raise ValueError(_safe_validation_message(exc, request)) from exc
    if isinstance(parsed, SuggestRequest):
        suggestion = await service.suggest(parsed.wakeup_event_id)
        enabled_operations = sorted(
            operation for operation in suggestion["enabled_operations"] if operation in {enabled.value for enabled in policy.enabled_operations}
        )
        return {
            "wakeup_event_id": parsed.wakeup_event_id,
            "provider": suggestion["provider"],
            "suggestions": suggestion["suggestions"],
            "enabled_operations": enabled_operations,
            "enabled_intents": sorted(suggestion["enabled_intents"]),
        }
    if isinstance(parsed, StatusRequest):
        result = await service.status(parsed.action_id)
    else:
        assert isinstance(parsed, ExecuteRequest)
        if not policy.writes_enabled or policy.kill_switch:
            detail = "kill_switch_open" if policy.kill_switch else "writes_disabled"
            action_id = uuid5(
                ACTION_NAMESPACE,
                f"v1:wakeup:{parsed.wakeup_event_id}:role:{parsed.action_role}:ordinal:0",
            )
            result = PublicResult(
                status=PublicStatus.REJECTED,
                action_id=action_id,
                action_uid=None,
                provider_request_ref=None,
                retryable=False,
                detail_code=detail,
            )
        elif parsed.operation not in policy.enabled_operations:
            action_id = uuid5(
                ACTION_NAMESPACE,
                f"v1:wakeup:{parsed.wakeup_event_id}:role:{parsed.action_role}:ordinal:0",
            )
            result = PublicResult(
                status=PublicStatus.REJECTED,
                action_id=action_id,
                action_uid=None,
                provider_request_ref=None,
                retryable=False,
                detail_code="operation_disabled",
            )
        else:
            result = await service.execute(parsed)
    return result.model_dump(mode="json")


def create_server(
    service: OutboundActionService,
    policy: FeaturePolicy,
    *,
    observability: GatewayObservability | None = None,
) -> FastMCP:
    mcp = FastMCP(
        "comm-outbound-gateway",
        instructions="One durable provider-neutral outbound action tool.",
        host="127.0.0.1",
        port=8094,
        streamable_http_path="/mcp",
        json_response=True,
    )

    @mcp.tool(
        name="outbound_action",
        description=(
            "Execute or inspect one durable outbound email, Quo, Cliq, calendar, or "
            "TenantCloud action. You choose the target id (to_address, to_phone, "
            "channel_or_chat_id, calendar_id, thread_id, lead_id, etc.) as part of "
            "arguments -- it is never derived from wakeup_event_id for you. Use suggest "
            '({"op": "suggest", "wakeup_event_id"}) to ask what the wake implies '
            "-- it returns advisory target ids drawn from the wake, never blocks, and "
            "stays reachable even when writes are disabled. Its answer is a suggestion "
            "only: you may pass any target id you like to execute, including ones that "
            "disagree with suggest."
        ),
        structured_output=True,
    )
    async def outbound_action(request: dict[str, Any]) -> dict[str, Any]:
        return await handle_outbound_action(service, policy, request)

    @mcp.resource("health://outbound-gateway", name="outbound-gateway-health")
    def health() -> str:
        return json.dumps(
            {
                "status": "ok",
                "writes_enabled": policy.writes_enabled,
                "kill_switch": policy.kill_switch,
            },
            sort_keys=True,
        )

    if observability is not None:

        @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
        async def healthz(_request: Request):
            healthy = await observability.database_healthy()
            return JSONResponse(
                {
                    "status": "ok" if healthy else "unhealthy",
                    "writes_enabled": policy.writes_enabled,
                    "kill_switch": policy.kill_switch,
                },
                status_code=200 if healthy else 503,
            )

        @mcp.custom_route("/metrics", methods=["GET"], include_in_schema=False)
        async def metrics(_request: Request):
            return PlainTextResponse(
                render_prometheus(await observability.collect()),
                media_type="text/plain; version=0.0.4",
            )

    return mcp


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.casefold() in {"1", "true", "yes", "on"}:
        return True
    if raw.casefold() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _json_mapping(name: str, default: dict[str, str]) -> dict[str, str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{name} must be a JSON string-to-string object")
    return value


def _enabled_operations() -> frozenset[Operation]:
    raw = os.environ.get("OUTBOUND_ENABLED_OPERATIONS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_OPERATIONS
    value = json.loads(raw)
    if not isinstance(value, list) or not value:
        raise ValueError("OUTBOUND_ENABLED_OPERATIONS_JSON must be a non-empty JSON array")
    try:
        return frozenset(Operation(item) for item in value if isinstance(item, str))
    except ValueError as exc:
        raise ValueError("OUTBOUND_ENABLED_OPERATIONS_JSON contains an unsupported operation") from exc


def _enabled_operations_by_provider() -> dict[str, frozenset[str]]:
    raw = os.environ.get("OUTBOUND_PROVIDER_OPERATIONS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER
    value = json.loads(raw)
    if not isinstance(value, dict) or not value:
        raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON must be a non-empty JSON object")
    parsed: dict[str, frozenset[str]] = {}
    for provider, operations in value.items():
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(operations, list)
            or not operations
            or not all(isinstance(item, str) for item in operations)
        ):
            raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON values must be non-empty string arrays")
        try:
            parsed[provider.casefold()] = frozenset(Operation(item).value for item in operations)
        except ValueError as exc:
            raise ValueError("OUTBOUND_PROVIDER_OPERATIONS_JSON contains an unsupported operation") from exc
    return parsed


def _enabled_intents() -> frozenset[str]:
    raw = os.environ.get("OUTBOUND_ENABLED_INTENTS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_INTENTS
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("OUTBOUND_ENABLED_INTENTS_JSON must be a non-empty string array")
    try:
        return frozenset(IntentKind(item).value for item in value)
    except ValueError as exc:
        raise ValueError("OUTBOUND_ENABLED_INTENTS_JSON contains an unsupported intent") from exc


def _enabled_intents_by_provider() -> dict[str, frozenset[str]]:
    raw = os.environ.get("OUTBOUND_PROVIDER_INTENTS_JSON")
    if raw is None:
        return DEFAULT_ENABLED_INTENTS_BY_PROVIDER
    value = json.loads(raw)
    if not isinstance(value, dict) or not value:
        raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON must be a non-empty JSON object")
    parsed: dict[str, frozenset[str]] = {}
    for provider, intents in value.items():
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(intents, list)
            or not intents
            or not all(isinstance(item, str) for item in intents)
        ):
            raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON values must be non-empty string arrays")
        try:
            parsed[provider.casefold()] = frozenset(IntentKind(item).value for item in intents)
        except ValueError as exc:
            raise ValueError("OUTBOUND_PROVIDER_INTENTS_JSON contains an unsupported intent") from exc
    return parsed


def _bearer_headers(name: str) -> dict[str, str]:
    token = os.environ.get(name, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _tenantcloud_enabled(enabled_operations: frozenset[Operation]) -> bool:
    return bool(enabled_operations & TENANTCLOUD_OPERATIONS)


def _reject_tenantcloud_origin_overrides() -> None:
    present = sorted(name for name in _TENANTCLOUD_ORIGIN_OVERRIDE_ENV_VARS if os.environ.get(name))
    if present:
        raise ValueError(
            "TenantCloud API origin is a fixed literal (" + TENANTCLOUD_ORIGIN + "); unsupported override variable(s) set: " + ", ".join(present)
        )


_TENANTCLOUD_MODULE_NAMES = ("tenantcloud_auth", "tenantcloud_client", "tenantcloud_mutations")


def _load_tenantcloud_modules(module_dir: str) -> tuple[ModuleType, ModuleType, ModuleType]:
    if not os.path.isdir(module_dir):
        raise ValueError(f"TenantCloud module directory not found: {module_dir} (is the CDS repo mounted at /repo?)")
    for name in _TENANTCLOUD_MODULE_NAMES:
        path = os.path.join(module_dir, f"{name}.py")
        if not os.path.isfile(path):
            raise ValueError(f"TenantCloud module not found: {path} (is the CDS repo mounted at /repo?)")

    # scripts/tenantcloud_mutations.py (Comm-Data-Store) imports
    # `from scripts.tenantcloud_client import ...` -- it belongs to that
    # repo's own `scripts` package layout. Rather than depend on
    # Comm-Data-Store as an installed package (a cross-repo dependency this
    # gateway does not otherwise have), stand up a private, process-local
    # `scripts` alias pointed at the mounted directory purely so that
    # internal import resolves, then restore whatever (if anything) was
    # already registered under that name so this cannot leak or collide.
    qualified_names = tuple(f"scripts.{name}" for name in _TENANTCLOUD_MODULE_NAMES)
    previous = {name: sys.modules.get(name) for name in ("scripts", *qualified_names)}
    package = ModuleType("scripts")
    package.__path__ = [module_dir]
    sys.modules["scripts"] = package
    for name in qualified_names:
        sys.modules.pop(name, None)
    try:
        modules = tuple(importlib.import_module(f"scripts.{name}") for name in _TENANTCLOUD_MODULE_NAMES)
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return modules  # type: ignore[return-value]


def _build_tenantcloud_adapter() -> TenantCloudAdapter:
    # Ordering matters: reject any attempt to override the origin before
    # doing anything else -- in particular before reading, validating, or
    # opening any file, and long before any HTTP call that could acquire a
    # token.
    _reject_tenantcloud_origin_overrides()

    control_url = os.environ.get("TENANTCLOUD_RUNNER_CONTROL_URL", "").strip()
    bearer_file = os.environ.get("TENANTCLOUD_RUNNER_BEARER_FILE", "").strip()
    next_bearer_file = os.environ.get("TENANTCLOUD_RUNNER_NEXT_BEARER_FILE", "").strip() or None
    module_dir = os.environ.get("TENANTCLOUD_MODULE_DIR", "/repo/scripts")

    if not control_url:
        raise ValueError("TENANTCLOUD_RUNNER_CONTROL_URL is required while a TenantCloud operation is enabled")
    if not bearer_file:
        raise ValueError("TENANTCLOUD_RUNNER_BEARER_FILE is required while a TenantCloud operation is enabled")
    if not os.path.isfile(bearer_file):
        raise ValueError(f"TENANTCLOUD_RUNNER_BEARER_FILE does not exist: {bearer_file}")
    if next_bearer_file is not None and not os.path.isfile(next_bearer_file):
        raise ValueError(f"TENANTCLOUD_RUNNER_NEXT_BEARER_FILE does not exist: {next_bearer_file}")

    # Imported by file path, not by package name: the facade lives in a
    # different repository (Comm-Data-Store), mounted read-only at /repo in
    # the running container. This gateway cannot add a cross-repo Python
    # dependency on it.
    auth_module, client_module, mutations_module = _load_tenantcloud_modules(module_dir)

    # HttpRunnerControl itself enforces literal HTTP loopback (127.0.0.1 or
    # ::1, no credentials/query/fragment/path) at construction time, before
    # any request is made -- see scripts/tenantcloud_auth.py in Comm-Data-Store.
    control = auth_module.HttpRunnerControl(control_url, bearer_file, next_bearer_file)
    auth = auth_module.TenantCloudAuth("tenantcloud-runner", control=control, profile_access=False)

    # The control and auth objects are stateless with respect to token
    # lifetime and are safe to share. The CLIENT is not: it owns the
    # AuthRefreshBudget, which permits one refresh and then caches that token
    # for the budget's lifetime. That budget is scan-local by design, so it
    # must not outlive a single gateway operation -- see TenantCloudAdapter's
    # docstring for the 2026-08-10 incident this prevents.
    def build_mutations():
        client = client_module.TenantCloudClient(auth, base_url=TENANTCLOUD_ORIGIN)
        return mutations_module.TenantCloudMutations(client)

    return TenantCloudAdapter(mutations_factory=build_mutations)


def _run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class _ThreadOffloadedAdapter:
    """Wraps a ``ProviderAdapter`` whose ``invoke``/``poll``/``reconcile`` are
    declared ``async`` but perform synchronous, blocking HTTP calls under the
    hood.

    TenantCloudAdapter (Task 7, adapters/tenantcloud.py) is built around the
    shared ``TenantCloudMutations`` facade (Comm-Data-Store,
    scripts/tenantcloud_mutations.py), which is entirely synchronous urllib
    HTTP -- including up to ``TenantCloudAuth.worst_case_auth_block_seconds``
    (180s) of blocking auth-refresh work per call. Task 7 explicitly parked
    the decision of whether to protect the event loop from that blocking for
    this task to make.

    Decision: offload here, at the server wiring boundary, via
    ``asyncio.to_thread``, rather than inside the adapter. The adapter stays
    a plain synchronous-under-async implementation with no event-loop
    concerns of its own (and is exercised that way, unwrapped, by its own
    unit tests); this wrapper is the one place that knows the concrete
    facade is blocking and pays the thread-hop cost for it. Every other
    ProviderAdapter in this gateway (email/quo/cliq/calendar) calls out
    through the async ``McpProviderClient`` and does not need this wrapper.
    """

    def __init__(self, inner: ProviderAdapter) -> None:
        self._inner = inner

    def validate(self, context: Any) -> None:
        self._inner.validate(context)

    def build_request(self, context: Any, action_uid: Any) -> Any:
        return self._inner.build_request(context, action_uid)

    def parse_receipt(self, context: Any, observation: Any) -> Any:
        return self._inner.parse_receipt(context, observation)

    async def invoke(self, client: Any, request: Any) -> Any:
        return await asyncio.to_thread(_run_coroutine_sync, self._inner.invoke(client, request))

    async def poll(self, client: Any, observation: Any) -> Any:
        return await asyncio.to_thread(_run_coroutine_sync, self._inner.poll(client, observation))

    async def reconcile(self, client: Any, context: Any, action_uid: Any, observation: Any) -> Any:
        return await asyncio.to_thread(
            _run_coroutine_sync,
            self._inner.reconcile(client, context, action_uid, observation),
        )


def _tenantcloud_adapters(enabled_operations: frozenset[Operation]) -> dict[Operation, ProviderAdapter]:
    """Build the (at most one) shared TenantCloud adapter, registered for all
    four TenantCloud operations, iff at least one of them is enabled.

    Fail-closed: when enabled, any missing/invalid configuration raises
    immediately (at startup) instead of registering a partially-working or
    silently-disabled adapter. When no TenantCloud operation is enabled,
    this never touches the environment beyond the operations set already in
    hand, so an unconfigured TenantCloud integration cannot break the rest
    of the gateway.
    """
    if not _tenantcloud_enabled(enabled_operations):
        return {}
    adapter: ProviderAdapter = _ThreadOffloadedAdapter(_build_tenantcloud_adapter())
    return {operation: adapter for operation in TENANTCLOUD_OPERATIONS}


async def build_runtime() -> GatewayRuntime:
    database_uri = os.environ.get("DATABASE_URI")
    if not database_uri:
        raise ValueError("DATABASE_URI is required")
    pool = DbConnPool(database_uri)
    await pool.pool_connect()
    driver = SqlDriver(conn=pool)
    policy = FeaturePolicy(
        writes_enabled=_bool("OUTBOUND_GATEWAY_WRITES_ENABLED", False),
        kill_switch=_bool("OUTBOUND_GATEWAY_KILL_SWITCH", True),
        enabled_operations=_enabled_operations(),
    )
    routing = RoutingPolicy(
        version=os.environ.get("OUTBOUND_ROUTING_POLICY_VERSION", "appointment-v1"),
        email_account_by_provider=_json_mapping(
            "OUTBOUND_EMAIL_ACCOUNTS_JSON",
            {"zillow": "nigel-zoho", "hotpads": "nigel-zoho", "tenantcloud": "nigel-zoho"},
        ),
        quo_line_by_provider=_json_mapping(
            "OUTBOUND_QUO_LINES_JSON",
            {provider: os.environ.get("OUTBOUND_QUO_PHONE_NUMBER_ID", "") for provider in ("hotpads", "quo", "tenantcloud", "zillow", "zumper")},
        ),
        calendar_by_profile={"appointment-setter": os.environ.get("OUTBOUND_CALENDAR_NAME", "nigel")},
        calendar_account_by_profile={"appointment-setter": os.environ.get("OUTBOUND_CALENDAR_ACCOUNT", "nigel-zoho")},
        cliq_target_by_intent=_json_mapping(
            "OUTBOUND_CLIQ_TARGETS_JSON",
            {"lead_alert": "tenant-leads", "manual_review_alert": "tenant-leads"},
        ),
        property_aliases=_json_mapping(
            "OUTBOUND_PROPERTY_ALIASES_JSON",
            DEFAULT_PROPERTY_ALIASES,
        ),
        conversation_aliases=_json_mapping("OUTBOUND_CONVERSATION_ALIASES_JSON", {}),
        enabled_operations_by_provider=_enabled_operations_by_provider(),
        enabled_intents=_enabled_intents(),
        enabled_intents_by_provider=_enabled_intents_by_provider(),
    )
    context_repository = OutboundGatewayRepository(driver)
    store = PostgresActionStore(driver)
    observability = GatewayObservability(
        driver,
        circuit_failure_threshold=int(os.environ.get("OUTBOUND_CIRCUIT_FAILURE_THRESHOLD", "5")),
        circuit_window_seconds=int(os.environ.get("OUTBOUND_CIRCUIT_WINDOW_SECONDS", "300")),
        circuit_open_seconds=int(os.environ.get("OUTBOUND_CIRCUIT_OPEN_SECONDS", "180")),
        old_action_seconds=int(os.environ.get("OUTBOUND_ALERT_OLD_ACTION_SECONDS", "300")),
        evidence_failure_threshold=int(os.environ.get("OUTBOUND_ALERT_EVIDENCE_FAILURE_THRESHOLD", "3")),
        alert_window_seconds=int(os.environ.get("OUTBOUND_ALERT_WINDOW_SECONDS", "300")),
    )
    provider_client = McpProviderClient(
        {
            "agent-email": McpServerConfig(
                name="agent-email",
                url=os.environ.get("AGENT_EMAIL_MCP_URL", "http://127.0.0.1:9090/mcp"),
                transport="streamable_http",
                headers=_bearer_headers("EMAIL_MCP_TOKEN"),
                allowed_tools=frozenset(
                    {
                        "email_send",
                        "email_get_thread",
                        "request_status",
                        "cliq_channel_bot_post",
                        "cliq_chat_post",
                        "calendar_create_event",
                        "calendar_update_event",
                        "calendar_delete_event",
                    }
                ),
            ),
            "quo": McpServerConfig(
                name="quo",
                url=os.environ.get("QUO_MCP_URL", "http://127.0.0.1:8080/sse"),
                transport="sse",
                headers=_bearer_headers("QUO_MCP_TOKEN"),
                allowed_tools=frozenset({"send_message", "list_messages", "get_message"}),
            ),
        }
    )
    email_domains = _json_mapping(
        "OUTBOUND_EMAIL_SENDER_DOMAINS_JSON",
        {
            "nigel-zoho": os.environ.get(
                "OUTBOUND_DEFAULT_EMAIL_DOMAIN",
                DEFAULT_EMAIL_SENDER_DOMAINS["nigel-zoho"],
            )
        },
    )
    email_cc_by_source = _json_mapping(
        "OUTBOUND_EMAIL_CC_BY_SOURCE_JSON",
        DEFAULT_EMAIL_CC_BY_SOURCE,
    )
    calendar_accounts = {routing.calendar_by_profile["appointment-setter"]: routing.calendar_account_by_profile["appointment-setter"]}
    adapters = {
        Operation.EMAIL_SEND: EmailAdapter(
            sender_domains=email_domains,
            cc_by_source=email_cc_by_source,
        ),
        Operation.QUO_SMS_SEND: QuoSmsAdapter(user_id=os.environ.get("OUTBOUND_QUO_USER_ID", "gateway")),
        Operation.CLIQ_CHANNEL_POST: CliqAdapter(Operation.CLIQ_CHANNEL_POST),
        Operation.CLIQ_CHAT_POST: CliqAdapter(Operation.CLIQ_CHAT_POST),
        Operation.CALENDAR_CREATE: CalendarAdapter(account_by_calendar=calendar_accounts),
        Operation.CALENDAR_UPDATE: CalendarAdapter(account_by_calendar=calendar_accounts),
        Operation.CALENDAR_DELETE: CalendarAdapter(account_by_calendar=calendar_accounts),
    }
    adapters.update(_tenantcloud_adapters(policy.enabled_operations))
    service = OutboundActionService(
        store=store,
        context_loader=ActionContextLoader(context_repository, routing),
        evidence_loader=DatabasePreflightEvidenceLoader(driver),
        adapters=adapters,
        provider_client=provider_client,
        clock=lambda: datetime.now(timezone.utc),
        lease_owner=os.environ.get("OUTBOUND_GATEWAY_LEASE_OWNER", "outbound-gateway"),
        circuit_guard=observability,
        retry_base_seconds=int(os.environ.get("OUTBOUND_RETRY_BASE_SECONDS", "5")),
        retry_max_seconds=int(os.environ.get("OUTBOUND_RETRY_MAX_SECONDS", "900")),
    )
    return GatewayRuntime(
        pool=pool,
        service=service,
        store=store,
        policy=policy,
        observability=observability,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comm-outbound-gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8094)
    return parser


async def _serve() -> None:
    args = _parser().parse_args()
    runtime = await build_runtime()
    mcp = create_server(
        runtime.service,
        runtime.policy,
        observability=runtime.observability,
    )
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    try:
        await mcp.run_streamable_http_async()
    finally:
        await runtime.pool.close()


def main() -> None:
    asyncio.run(_serve())


async def _work() -> None:
    runtime = await build_runtime()
    worker = OutboundWorker(
        store=runtime.store,
        service=runtime.service,
        batch_size=int(os.environ.get("OUTBOUND_WORKER_BATCH_SIZE", "20")),
        max_attempts=int(os.environ.get("OUTBOUND_MAX_ATTEMPTS", "5")),
        observability=runtime.observability,
    )
    interval = max(1.0, float(os.environ.get("OUTBOUND_WORKER_INTERVAL_SECONDS", "5")))
    try:
        while True:
            if runtime.policy.writes_enabled and not runtime.policy.kill_switch:
                await worker.run_once()
            await asyncio.sleep(interval)
    finally:
        await runtime.pool.close()


def worker_main() -> None:
    asyncio.run(_work())
