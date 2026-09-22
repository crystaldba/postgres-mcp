"""The TenantCloud facade must not outlive a single gateway operation.

The facade's AuthRefreshBudget is scan-local by design (Comm-Data-Store,
docs/superpowers/specs/2026-07-21-tenantcloud-inactive-runner-refresh-design.md):
it permits exactly one token refresh, then caches that token for the budget's
lifetime so a short batch does not re-read a frozen token per endpoint.

The gateway used to build one facade in build_runtime() and reuse it for the
container's whole life, turning "scan-local" into "forever". On 2026-08-10 a
single Firefox lapse forced one refresh; that token was cached and served for
days after it expired. Three lead status updates were stranded and only a
container restart cleared it.

These pin the facade's LIFETIME. They deliberately do not relax the budget or
the cache -- those are the documented anti-storm design.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from postgres_mcp.outbound_gateway.adapters.tenantcloud import TenantCloudAdapter
from postgres_mcp.outbound_gateway.models import Operation

TOKEN_TTL = 600
TIMELINE = (0, 300, 900, 1500, 3600, 7200)
LAPSE_AT = 900


class _Stop(Exception):
    """Abort the adapter right after the facade call; only token choice matters."""


class _Facade:
    """Mimics the real facade's budget: one refresh, then a permanent cache."""

    def __init__(self, clock: dict[str, Any], log: list[tuple[int, str]]) -> None:
        self._clock = clock
        self._log = log
        self._used = 0
        self._cached: str | None = None

    def _token(self) -> str:
        now = self._clock["t"]
        if self._cached is not None:
            return self._cached
        if now != LAPSE_AT:
            return f"tok@{(now // TOKEN_TTL) * TOKEN_TTL}"
        if self._used >= 1:
            raise RuntimeError("authentication unavailable")
        self._used += 1
        self._cached = f"tok@{now}"
        return self._cached

    def reconcile_lead_status(self, lead_id: object) -> Any:
        self._log.append((self._clock["t"], self._token()))
        raise _Stop()

    def mark_lead_working(self, lead_id: object) -> Any:  # pragma: no cover
        self._log.append((self._clock["t"], self._token()))
        raise _Stop()


def _request() -> Any:
    return type(
        "Request",
        (),
        {
            "tool": Operation.TENANTCLOUD_LEAD_STATUS_UPDATE.value,
            "arguments": {"lead_id": "1", "target_reference": "lead:1"},
        },
    )()


def _drive(adapter: TenantCloudAdapter) -> None:
    try:
        asyncio.run(adapter.invoke(None, _request()))
    except _Stop:
        pass


def _is_live(token: str, now: int) -> bool:
    return now - int(token.split("@")[1]) < TOKEN_TTL


def test_every_operation_receives_a_live_token() -> None:
    """RED before the fix: one lapse cached a token that was served for hours."""
    clock: dict[str, Any] = {"t": 0}
    log: list[tuple[int, str]] = []
    adapter = TenantCloudAdapter(mutations_factory=lambda: _Facade(clock, log))

    for moment in TIMELINE:
        clock["t"] = moment
        _drive(adapter)

    stale = [(t, tok) for t, tok in log if not _is_live(tok, t)]
    assert stale == [], f"operations served an expired token: {stale}"


def test_operations_do_not_share_an_auth_refresh_budget() -> None:
    """RED before the fix: the second operation inherited a spent budget."""
    clock: dict[str, Any] = {"t": LAPSE_AT}
    log: list[tuple[int, str]] = []
    made: list[_Facade] = []

    def factory() -> _Facade:
        facade = _Facade(clock, log)
        made.append(facade)
        return facade

    adapter = TenantCloudAdapter(mutations_factory=factory)
    _drive(adapter)
    _drive(adapter)

    assert len(made) == 2, "each operation must build its own facade"
    assert all(f._used <= 1 for f in made)
    assert [f._used for f in made] == [1, 1], "second operation could not refresh"


def test_one_operation_shares_a_single_facade() -> None:
    """GREEN both ways: pre-write readback and the write stay in one scan.

    Splitting them across facades would double provider auth work and break the
    one-refresh reservation within a dispatch.
    """
    clock: dict[str, Any] = {"t": 0}
    made: list[_Facade] = []

    def factory() -> _Facade:
        facade = _Facade(clock, [])
        made.append(facade)
        return facade

    _drive(TenantCloudAdapter(mutations_factory=factory))
    assert len(made) == 1, "a single operation must not build multiple facades"


def test_a_fixed_double_is_supplied_through_the_factory() -> None:
    """GREEN: tests pin a double with ``mutations_factory=lambda: double``.

    There is no instance constructor to reach for, so no caller can hand the
    adapter a long-lived facade by accident.
    """
    clock: dict[str, Any] = {"t": 0}
    facade = _Facade(clock, [])
    adapter = TenantCloudAdapter(mutations_factory=lambda: facade)
    _drive(adapter)
    _drive(adapter)


def test_adapter_accepts_no_ready_made_facade() -> None:
    """GREEN: the instance constructor is gone, so the latch is unreachable.

    Passing a long-lived facade is exactly what stranded three leads; the API
    must refuse it rather than rely on callers remembering.
    """
    clock: dict[str, Any] = {"t": 0}
    facade = _Facade(clock, [])
    with pytest.raises(TypeError):
        TenantCloudAdapter(mutations=facade)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TenantCloudAdapter()  # type: ignore[call-arg]


def test_production_wiring_builds_a_facade_per_operation(monkeypatch: Any) -> None:
    """RED against the old wiring, for the reason that actually broke production.

    _build_tenantcloud_adapter() used to hand TenantCloudAdapter a single
    facade instance, so every operation for the container's lifetime shared one
    AuthRefreshBudget. This asserts the built adapter makes a new facade per
    operation -- the only thing that keeps the budget scan-local in a daemon.
    """
    from postgres_mcp.outbound_gateway import server as server_module

    built: list[object] = []

    class _Client:
        def __init__(self, auth: object, base_url: str) -> None:
            self.auth = auth

    class _Mutations:
        def __init__(self, client: object) -> None:
            built.append(client)

        def reconcile_lead_status(self, lead_id: object) -> Any:
            raise _Stop()

    class _Auth:
        def __init__(self, *a: object, **k: object) -> None: ...

    class _Control:
        def __init__(self, *a: object, **k: object) -> None: ...

    monkeypatch.setattr(server_module, "_reject_tenantcloud_origin_overrides", lambda: None)
    monkeypatch.setattr(
        server_module,
        "_load_tenantcloud_modules",
        lambda _dir: (
            type("A", (), {"HttpRunnerControl": _Control, "TenantCloudAuth": _Auth}),
            type("C", (), {"TenantCloudClient": _Client}),
            type("M", (), {"TenantCloudMutations": _Mutations}),
        ),
    )
    for name, value in (
        ("TENANTCLOUD_RUNNER_CONTROL_URL", "http://127.0.0.1:6202"),
        ("TENANTCLOUD_RUNNER_BEARER_FILE", __file__),
        ("TENANTCLOUD_RUNNER_NEXT_BEARER_FILE", __file__),
        ("TENANTCLOUD_MODULE_DIR", "/repo/scripts"),
    ):
        monkeypatch.setenv(name, value)

    adapter = server_module._build_tenantcloud_adapter()
    _drive(adapter)
    _drive(adapter)

    assert len(built) == 2, (
        "each operation must construct its own TenantCloudClient; "
        f"got {len(built)} for 2 operations"
    )
    assert built[0] is not built[1], "operations shared one client (and one auth budget)"
