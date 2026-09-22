"""Per-recipient traffic control: in-flight lease + context-staleness watermark.

Pure decision logic over a small probe interface. Fail-open on probe
malfunction: a broken check must never stop outbound traffic (the kill
switch is the only fail-closed control). A check that RUNS and FIRES
blocks normally."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class InFlightAction:
    action_id: UUID
    operation: str
    state: str
    created_at: datetime
    preview: str


@dataclass(frozen=True)
class NewerActivity:
    direction: str
    source: str
    occurred_at: datetime
    preview: str
    message_id: int | None
    action_id: UUID | None


@dataclass(frozen=True)
class TrafficVerdict:
    allowed: bool
    reason: str  # pass | lease_held | stale_context | gate_check_failed
    detail: str
    check_failed: bool


class TrafficProbe(Protocol):
    async def in_flight_actions(
        self, recipient_key: str, exclude_action_id: UUID
    ) -> list[InFlightAction]: ...

    async def newest_activity_after(
        self, recipient_key: str, channel_id: int, watermark: datetime, exclude_action_id: UUID
    ) -> NewerActivity | None: ...

    async def context_watermark(self, wakeup_event_id: int) -> datetime | None: ...


# Single source of truth for the three operating modes (service.py and
# server.py both validate against this instead of each keeping their own
# copy of the literal set).
VALID_TRAFFIC_MODES = frozenset({"off", "shadow", "enforce"})


_PASS = TrafficVerdict(allowed=True, reason="pass", detail="", check_failed=False)
_FAIL_OPEN = TrafficVerdict(
    allowed=True, reason="gate_check_failed", detail="", check_failed=True
)


async def check_traffic(
    probe: TrafficProbe,
    *,
    recipient_key: str,
    channel_id: int,
    wakeup_event_id: int,
    action_id: UUID,
    override: bool,
    logger: logging.Logger,
) -> TrafficVerdict:
    try:
        in_flight = await probe.in_flight_actions(recipient_key, action_id)
    except Exception:
        logger.warning(
            "traffic control check failed (in_flight) for %s", recipient_key, exc_info=True
        )
        return _FAIL_OPEN
    if in_flight:
        other = in_flight[0]
        return TrafficVerdict(
            allowed=False,
            reason="lease_held",
            detail=(
                f"Another send to this recipient is in flight: action {other.action_id} "
                f"({other.operation}, state {other.state}, started {other.created_at.isoformat()}): "
                f"\"{other.preview}\". Wait for it to reach a terminal state, then re-check "
                f"the thread before sending."
            ),
            check_failed=False,
        )
    if override:
        # IMPORTANT 4: override intentionally bypasses a staleness block,
        # but that must still be auditable -- silently short-circuiting here
        # (the old behavior) meant nothing was ever logged about *what* got
        # overridden. Still fetch newest_activity_after purely for the audit
        # trail; a probe failure here must not degrade the override itself
        # (fail-open, log-only, no ledger schema change), so any exception
        # is swallowed after logging.
        try:
            watermark = await probe.context_watermark(wakeup_event_id)
            if watermark is not None:
                newer = await probe.newest_activity_after(recipient_key, channel_id, watermark, action_id)
                if newer is not None:
                    ref = f"message {newer.message_id}" if newer.message_id else f"action {newer.action_id}"
                    logger.warning(
                        "override bypassed staleness: wake=%s, recipient=%s, overrode %s at %s",
                        wakeup_event_id,
                        recipient_key,
                        ref,
                        newer.occurred_at.isoformat(),
                    )
        except Exception:
            logger.warning(
                "traffic control check failed (staleness, override audit) for %s",
                recipient_key,
                exc_info=True,
            )
        return _PASS
    try:
        watermark = await probe.context_watermark(wakeup_event_id)
        if watermark is None:
            logger.warning(
                "traffic control check failed (no watermark) for wake %s", wakeup_event_id
            )
            return _FAIL_OPEN
        newer = await probe.newest_activity_after(recipient_key, channel_id, watermark, action_id)
    except Exception:
        logger.warning(
            "traffic control check failed (staleness) for %s", recipient_key, exc_info=True
        )
        return _FAIL_OPEN
    if newer is None:
        return _PASS
    ref = f"message {newer.message_id}" if newer.message_id else f"action {newer.action_id}"
    return TrafficVerdict(
        allowed=False,
        reason="stale_context",
        detail=(
            f"New {newer.direction} activity since your context was built: {ref} via "
            f"{newer.source} at {newer.occurred_at.isoformat()}: \"{newer.preview}\". "
            f"Re-read the thread and skip if your message is now redundant, or resend "
            f"with override=true if it is still needed."
        ),
        check_failed=False,
    )
