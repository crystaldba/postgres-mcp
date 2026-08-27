"""Parameterized database reads for immutable outbound event context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Protocol

from postgres_mcp.sql import SafeSqlDriver


@dataclass(frozen=True)
class WakeEventRecord:
    wakeup_event_id: int
    event_source: str
    source_event_id: str
    event_created_at: datetime
    message_id: int
    canonical_message_id: int | None
    message_source: str
    source_message_id: str
    message_sent_at: datetime
    message_updated_at: datetime
    subject: str | None
    body: str | None
    user_account_id: str | None
    channel_id: int
    source_channel_id: str
    channel_type: str
    channel_name: str | None
    sender_participant_id: int | None
    participant_type: str | None
    participant_key: str | None
    display_name: str | None
    envelope: dict[str, Any]
    raw_payload: dict[str, Any]
    tenantcloud_claim_id: int | None = None
    tenantcloud_claim_family: str | None = None
    tenantcloud_claim_state: str | None = None
    tenantcloud_action_owner: str | None = None
    tenantcloud_entity_scope_key: str | None = None


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_watermark: int
    latest_message_id: int
    latest_sent_at: datetime


@dataclass(frozen=True)
class AliasResolution:
    canonical_subject: str | None
    ambiguous: bool = False


class ContextRepository(Protocol):
    async def load_wake_event(self, wakeup_event_id: int) -> WakeEventRecord | None: ...

    async def load_conversation_snapshot(self, channel_id: int) -> ConversationSnapshot: ...

    async def resolve_canonical_subject(
        self,
        aliases: tuple[str, ...],
        property_scope: str,
    ) -> AliasResolution: ...


class OutboundGatewayRepository:
    """SQL-only repository. Derivation and policy stay in separate modules."""

    def __init__(self, driver: Any):
        self._driver = driver

    async def load_wake_event(self, wakeup_event_id: int) -> WakeEventRecord | None:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT
                event_row.id AS wakeup_event_id,
                event_row.source AS event_source,
                event_row.source_event_id,
                event_row.created_at AS event_created_at,
                message_row.id AS message_id,
                message_row.canonical_message_id,
                message_row.source AS message_source,
                message_row.source_message_id,
                message_row.sent_at AS message_sent_at,
                message_row.updated_at AS message_updated_at,
                message_row.subject,
                message_row.body,
                message_row.user_account_id,
                channel_row.id AS channel_id,
                channel_row.source_channel_id,
                channel_row.channel_type,
                channel_row.name AS channel_name,
                participant_row.id AS sender_participant_id,
                participant_row.participant_type,
                participant_row.participant_key,
                participant_row.display_name,
                event_row.envelope,
                coalesce(raw_row.payload, '{{}}'::jsonb) AS raw_payload,
                event_row.tenantcloud_claim_id,
                claim_row.event_family AS tenantcloud_claim_family,
                claim_row.claim_state AS tenantcloud_claim_state,
                claim_row.action_owner AS tenantcloud_action_owner,
                claim_row.entity_scope_key AS tenantcloud_entity_scope_key
            FROM hermes_wakeup_events AS event_row
            JOIN messages AS message_row ON message_row.id = event_row.message_id
            JOIN channels AS channel_row ON channel_row.id = message_row.channel_id
            LEFT JOIN participants AS participant_row
              ON participant_row.id = message_row.sender_participant_id
            LEFT JOIN raw_events AS raw_row ON raw_row.id = message_row.raw_event_id
            LEFT JOIN tenantcloud_event_claims AS claim_row
              ON claim_row.claim_id = event_row.tenantcloud_claim_id
            WHERE event_row.id = {}
            """,
            [wakeup_event_id],
        )
        if not rows:
            return None
        return WakeEventRecord(**rows[0].cells)

    async def load_conversation_snapshot(self, channel_id: int) -> ConversationSnapshot:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT
                coalesce(max(id), 0) AS conversation_watermark,
                coalesce(max(id), 0) AS latest_message_id,
                coalesce(max(sent_at), '-infinity'::timestamptz) AS latest_sent_at
            FROM messages
            WHERE channel_id = {}
            """,
            [channel_id],
        )
        if not rows:
            raise LookupError(f"conversation channel {channel_id} is unavailable")
        return ConversationSnapshot(**rows[0].cells)

    async def resolve_canonical_subject(
        self,
        aliases: tuple[str, ...],
        property_scope: str,
    ) -> AliasResolution:
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            SELECT
                count(DISTINCT canonical_subject)::integer AS subject_count,
                min(canonical_subject) AS canonical_subject
            FROM outbound_action_subject_aliases
            WHERE alias_key = ANY({})
              AND scope_key IN ('', {})
            """,
            [list(aliases), property_scope],
        )
        if not rows:
            return AliasResolution(canonical_subject=None)
        cells = rows[0].cells
        return AliasResolution(
            canonical_subject=cells.get("canonical_subject"),
            ambiguous=int(cells.get("subject_count") or 0) > 1,
        )
