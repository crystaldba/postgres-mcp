"""Current database evidence for outbound safety preflight."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Mapping

from postgres_mcp.sql import SafeSqlDriver

from .context import ActionContext
from .preflight import CalendarDependencyState
from .preflight import PreflightEvidence
from .preflight import RefreshEvidence
from .preflight import RefreshStatus


class DatabasePreflightEvidenceLoader:
    """Loads message/dependency facts only. Calendar owns slot selection."""

    def __init__(self, driver: Any):
        self._driver = driver

    async def load(self, context: ActionContext) -> PreflightEvidence:
        provider_family = "zillow" if context.source in {"hotpads", "zillow", "zumper"} else context.source
        recipient_phone = "".join(character for character in str(context.recipient_phone or "") if character.isdigit())
        equivalent_inbound_ids = sorted({context.source_message_id, *context.cross_channel_duplicate_message_ids})
        certified_older_message_ids = list(context.certified_older_message_ids)
        rows = await SafeSqlDriver.execute_param_query(
            self._driver,
            """
            WITH related_messages AS (
                SELECT
                    message_row.id,
                    message_row.canonical_message_id,
                    message_row.source_message_id,
                    message_row.sent_at,
                    message_row.direction,
                    message_row.source,
                    raw_row.payload,
                    participant_row.participant_type,
                    participant_row.participant_key
                FROM messages AS message_row
                LEFT JOIN raw_events AS raw_row ON raw_row.id = message_row.raw_event_id
                LEFT JOIN participants AS participant_row
                  ON participant_row.id = message_row.sender_participant_id
                WHERE (
                    {} = 'zillow'
                    AND (
                        lower(coalesce(
                            raw_row.payload->>'proxy_email',
                            raw_row.payload->>'zillow_proxy_email',
                            raw_row.payload->>'relay_email',
                            CASE
                                WHEN lower(coalesce(participant_row.participant_type, ''))
                                         IN ('email', 'email_address')
                                  AND split_part(
                                        lower(coalesce(participant_row.participant_key, '')),
                                        '@', 2
                                      ) = 'convo.zillow.com'
                                THEN participant_row.participant_key
                            END,
                            ''
                        )) = lower({})
                        OR EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                CASE
                                    WHEN jsonb_typeof(raw_row.payload->'participants') = 'array'
                                    THEN raw_row.payload->'participants'
                                    ELSE '[]'::jsonb
                                END
                            ) AS recipient(value)
                            WHERE lower(coalesce(recipient.value->>'kind', '')) = 'to'
                              AND lower(coalesce(recipient.value->>'address', '')) = lower({})
                        )
                    )
                ) OR (
                    {} = 'quo'
                    AND lower(message_row.source) IN ('quo', 'openphone')
                    AND lower(coalesce(
                        raw_row.payload#>>'{{data,object,phoneNumberId}}',
                        raw_row.payload#>>'{{data,object,phone_number_id}}',
                        ''
                    )) = lower({})
                    AND (
                        lower(coalesce(
                            raw_row.payload#>>'{{data,object,conversationId}}',
                            raw_row.payload#>>'{{data,object,conversation_id}}',
                            ''
                        )) = lower({})
                        OR (
                            nullif(coalesce(
                                raw_row.payload#>>'{{data,object,conversationId}}',
                                raw_row.payload#>>'{{data,object,conversation_id}}',
                                ''
                            ), '') IS NULL
                            AND {} LIKE 'line:%'
                        )
                    )
                    AND (
                        (
                            lower(coalesce(
                                message_row.direction,
                                raw_row.payload->>'direction',
                                raw_row.payload#>>'{{data,object,direction}}',
                                ''
                            )) IN ('inbound', 'incoming', 'received')
                            AND regexp_replace(
                                coalesce(raw_row.payload#>>'{{data,object,from}}', ''),
                                '[^0-9]', '', 'g'
                            ) = {}
                        ) OR (
                            lower(coalesce(
                                message_row.direction,
                                raw_row.payload->>'direction',
                                raw_row.payload#>>'{{data,object,direction}}',
                                ''
                            )) IN ('outbound', 'outgoing', 'sent')
                            AND regexp_replace(
                                coalesce(raw_row.payload#>>'{{data,object,to}}', ''),
                                '[^0-9]', '', 'g'
                            ) = {}
                        )
                    )
                ) OR (
                    {} NOT IN ('zillow', 'quo')
                    AND message_row.channel_id = {}
                )
            ), conversation AS (
                SELECT
                    max(related.id) FILTER (
                        WHERE (related.sent_at, related.id) > ({}::timestamptz, {})
                          AND NOT (
                              coalesce(related.canonical_message_id, related.id)
                              = ANY({}::bigint[])
                          )
                          AND NOT (
                              lower(related.source) = 'zillow_rm_web_extract'
                              AND related.id = ANY({}::bigint[])
                          )
                          AND lower(coalesce(
                              related.direction,
                              related.payload->>'direction',
                              related.payload#>>'{{data,object,direction}}',
                              ''
                          )) IN ('inbound', 'incoming', 'received', 'prospect')
                    ) AS later_inbound_message_id,
                    max(related.sent_at) AS latest_sent_at
                FROM related_messages AS related
            ), verified_outbound AS (
                SELECT
                    related.id AS verified_outbound_message_id,
                    coalesce(
                        related.payload->'provider_ids'->>'message',
                        related.payload#>>'{{data,object,id}}',
                        related.payload->>'provider_request_ref',
                        related.payload->>'request_ref',
                        related.payload->>'provider_message_id',
                        related.payload->>'message_id',
                        related.source_message_id
                    ) AS verified_outbound_request_ref
                FROM related_messages AS related
                WHERE (related.sent_at, related.id) > ({}::timestamptz, {})
                  AND (
                    (
                        {} = 'zillow'
                        AND lower(related.source) IN ('zoho_mail', 'nigel_mail')
                        AND lower(coalesce(related.direction, '')) = 'outbound'
                        AND replace(lower(coalesce(
                            related.payload->>'source_folder', ''
                        )), '_', '-') IN ('sent', 'sent-mail', 'outbox')
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(related.payload->'participants')
                                AS sender_participant(value)
                            WHERE lower(coalesce(
                                sender_participant.value->>'kind', ''
                            )) = 'from'
                              AND split_part(lower(coalesce(
                                  sender_participant.value->>'address', ''
                              )), '@', 2) = 'pfg.io'
                        )
                    ) OR (
                        {} = 'quo'
                        AND lower(coalesce(
                            related.direction,
                            related.payload->>'direction',
                            related.payload#>>'{{data,object,direction}}',
                            ''
                        )) IN ('outbound', 'outgoing', 'sent')
                    ) OR (
                        {} NOT IN ('zillow', 'quo')
                        AND lower(coalesce(related.direction, '')) = 'outbound'
                    )
                  )
                  AND nullif(btrim(coalesce(
                      related.payload->'provider_ids'->>'message',
                      related.payload#>>'{{data,object,id}}',
                      related.payload->>'provider_request_ref',
                      related.payload->>'request_ref',
                      related.payload->>'provider_message_id',
                      related.payload->>'message_id',
                      related.source_message_id
                  )), '') IS NOT NULL
                ORDER BY related.sent_at DESC, related.id DESC
                LIMIT 1
            ), dependency AS (
                SELECT CASE
                    WHEN {} NOT IN (
                        'showing_confirmation', 'showing_reschedule',
                        'showing_cancellation'
                    ) THEN 'not_required'
                    WHEN EXISTS (
                        SELECT 1 FROM outbound_actions
                        WHERE wakeup_event_id = {}
                          AND action_role = 'calendar_mutation'
                          AND state = 'completed'
                    ) THEN 'completed'
                    WHEN EXISTS (
                        SELECT 1 FROM outbound_actions
                        WHERE wakeup_event_id = {}
                          AND action_role = 'calendar_mutation'
                          AND state IN (
                              'rejected', 'definitive_failed', 'dead_letter',
                              'manual_review'
                          )
                    ) THEN 'failed'
                    ELSE 'pending'
                END AS calendar_dependency_state
            )
            SELECT
                conversation.later_inbound_message_id,
                verified_outbound.verified_outbound_message_id,
                verified_outbound.verified_outbound_request_ref,
                coalesce(conversation.latest_sent_at, {}::timestamptz) AS latest_sent_at,
                dependency.calendar_dependency_state,
                false AS calendar_already_applied
            FROM conversation
            CROSS JOIN dependency
            LEFT JOIN verified_outbound ON true
            """,
            [
                provider_family,
                context.target.target_id,
                context.target.target_id,
                provider_family,
                context.provider_account,
                context.thread_identity,
                context.thread_identity,
                recipient_phone,
                recipient_phone,
                provider_family,
                context.channel_id,
                context.source_sent_at,
                context.source_message_id,
                equivalent_inbound_ids,
                certified_older_message_ids,
                context.source_sent_at,
                context.source_message_id,
                provider_family,
                provider_family,
                provider_family,
                context.intent_kind.value,
                context.wakeup_event_id,
                context.wakeup_event_id,
                context.source_sent_at,
            ],
        )
        if not rows:
            raise LookupError("preflight evidence query returned no row")
        cells = rows[0].cells
        verified_id = cells.get("verified_outbound_message_id")
        verified_ref = cells.get("verified_outbound_request_ref")
        latest_sent_at = cells.get("latest_sent_at") or context.source_sent_at
        return PreflightEvidence(
            current_recipient_id=context.target.target_id,
            current_property_id=context.property_id,
            current_appointment_slot=context.appointment_slot,
            later_inbound_message_id=cells.get("later_inbound_message_id"),
            verified_outbound_message_id=verified_id,
            verified_outbound_request_ref=verified_ref,
            verified_outbound_covers_source=bool(verified_id and verified_ref),
            calendar_dependency=CalendarDependencyState(str(cells["calendar_dependency_state"])),
            calendar_already_applied=bool(cells.get("calendar_already_applied")),
            calendar_context_changed=False,
            overlapping_showing_prospect_ids=(),
            refresh_required_through=latest_sent_at,
            refresh=self._refresh(context.refresh_evidence),
        )

    @staticmethod
    def _refresh(value: Mapping[str, Any]) -> RefreshEvidence | None:
        if not value:
            return None
        try:
            status = RefreshStatus(str(value["status"]))
            covered_through = _datetime(value.get("covered_through"))
            thread = str(value["covered_thread_identity"])
            attempts = int(value["attempt_count"])
        except (KeyError, TypeError, ValueError):
            return RefreshEvidence(
                status=RefreshStatus.FAILED,
                covered_through=None,
                covered_thread_identity="",
                attempt_count=0,
                identity_resolved=False,
                thread_resolved=False,
                property_resolved=False,
            )
        return RefreshEvidence(
            status=status,
            covered_through=covered_through,
            covered_thread_identity=thread,
            attempt_count=attempts,
            identity_resolved=bool(value.get("identity_resolved", True)),
            thread_resolved=bool(value.get("thread_resolved", True)),
            property_resolved=bool(value.get("property_resolved", True)),
        )


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
