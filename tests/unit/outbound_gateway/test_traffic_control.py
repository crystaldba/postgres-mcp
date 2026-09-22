from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

from postgres_mcp.outbound_gateway.traffic_control import InFlightAction
from postgres_mcp.outbound_gateway.traffic_control import NewerActivity
from postgres_mcp.outbound_gateway.traffic_control import TrafficVerdict
from postgres_mcp.outbound_gateway.traffic_control import check_traffic

NOW = datetime(2026, 8, 27, 12, 5, tzinfo=timezone.utc)
WATERMARK = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
LOGGER = logging.getLogger("test.traffic")


class FakeProbe:
    def __init__(self, *, in_flight=(), newer=None, watermark=WATERMARK, raises=None):
        self._in_flight = list(in_flight)
        self._newer = newer
        self._watermark = watermark
        self._raises = raises

    async def in_flight_actions(self, recipient_key, exclude_action_id):
        if self._raises == "in_flight":
            raise RuntimeError("db down")
        return self._in_flight

    async def newest_activity_after(self, recipient_key, channel_id, watermark, exclude_action_id):
        if self._raises == "staleness":
            raise RuntimeError("db down")
        return self._newer

    async def context_watermark(self, wakeup_event_id):
        if self._raises == "watermark":
            raise RuntimeError("db down")
        return self._watermark


def _kwargs(**over):
    base = dict(
        recipient_key="prospect:email:melody@example.com",
        channel_id=676079,
        wakeup_event_id=25789,
        action_id=uuid4(),
        override=False,
        logger=LOGGER,
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_pass_when_quiet():
    verdict = await check_traffic(FakeProbe(), **_kwargs())
    assert verdict == TrafficVerdict(allowed=True, reason="pass", detail="", check_failed=False)


@pytest.mark.asyncio
async def test_lease_held_blocks():
    other = InFlightAction(uuid4(), "email.send", "dispatching", NOW, "Hi Melody...")
    verdict = await check_traffic(FakeProbe(in_flight=[other]), **_kwargs())
    assert not verdict.allowed
    assert verdict.reason == "lease_held"
    assert "email.send" in verdict.detail and "Hi Melody" in verdict.detail


@pytest.mark.asyncio
async def test_own_action_is_excluded_by_probe_contract():
    # exclude_action_id is forwarded so a retry of the same action never self-blocks
    action_id = uuid4()
    probe = FakeProbe()
    seen = {}
    orig = probe.in_flight_actions

    async def spy(recipient_key, exclude_action_id):
        seen["exclude"] = exclude_action_id
        return await orig(recipient_key, exclude_action_id)

    probe.in_flight_actions = spy
    await check_traffic(probe, **_kwargs(action_id=action_id))
    assert seen["exclude"] == action_id


@pytest.mark.asyncio
async def test_stale_context_blocks_with_detail():
    newer = NewerActivity("inbound", "zoho_mail", NOW, "Melody withdrew their application", 670826, None)
    verdict = await check_traffic(FakeProbe(newer=newer), **_kwargs())
    assert not verdict.allowed
    assert verdict.reason == "stale_context"
    assert "withdrew" in verdict.detail and "override" in verdict.detail


@pytest.mark.asyncio
async def test_override_skips_staleness_not_lease():
    newer = NewerActivity("outbound", "quo", NOW, "already replied", None, uuid4())
    assert (await check_traffic(FakeProbe(newer=newer), **_kwargs(override=True))).allowed
    other = InFlightAction(uuid4(), "quo.sms.send", "dispatching", NOW, "sending...")
    verdict = await check_traffic(FakeProbe(in_flight=[other], newer=newer), **_kwargs(override=True))
    assert not verdict.allowed and verdict.reason == "lease_held"


@pytest.mark.asyncio
async def test_missing_watermark_fails_open():
    verdict = await check_traffic(FakeProbe(watermark=None), **_kwargs())
    assert verdict.allowed and verdict.check_failed and verdict.reason == "gate_check_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["in_flight", "staleness", "watermark"])
async def test_probe_exception_fails_open(stage, caplog):
    with caplog.at_level(logging.WARNING):
        verdict = await check_traffic(FakeProbe(raises=stage), **_kwargs())
    assert verdict.allowed and verdict.check_failed
    assert any("traffic control check failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_own_row_excluded_from_staleness_query():
    """CRITICAL 1: execute() persists the action's own durable row (via
    create_or_load) *before* the traffic gate runs, so with no exclusion
    the ledger query would find that just-created row as "newer outbound
    activity" and self-block every real send. exclude_action_id must reach
    newest_activity_after, not just in_flight_actions."""
    action_id = uuid4()
    probe = FakeProbe()
    seen = {}
    orig = probe.newest_activity_after

    async def spy(recipient_key, channel_id, watermark, exclude_action_id):
        seen["exclude"] = exclude_action_id
        return await orig(recipient_key, channel_id, watermark, exclude_action_id)

    probe.newest_activity_after = spy
    await check_traffic(probe, **_kwargs(action_id=action_id))
    assert seen["exclude"] == action_id


@pytest.mark.asyncio
async def test_override_with_newer_activity_logs_audit_warning(caplog):
    """IMPORTANT 4: override used to short-circuit before ever fetching
    newer activity, so an overridden staleness block left nothing
    auditable. Now it still fetches (for the audit trail only -- this
    must remain log-only, no ledger schema change) and logs a WARNING
    naming what got overridden."""
    newer = NewerActivity("inbound", "zoho_mail", NOW, "Melody withdrew their application", 670826, None)
    with caplog.at_level(logging.WARNING):
        verdict = await check_traffic(FakeProbe(newer=newer), **_kwargs(override=True))
    assert verdict.allowed
    messages = [r.getMessage() for r in caplog.records]
    assert any("override bypassed staleness" in m and "message 670826" in m and "prospect:email:melody@example.com" in m for m in messages)


@pytest.mark.asyncio
async def test_override_without_newer_activity_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        verdict = await check_traffic(FakeProbe(), **_kwargs(override=True))
    assert verdict.allowed
    assert not caplog.records
