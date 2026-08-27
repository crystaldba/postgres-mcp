"""Agent Email calendar adapters."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Mapping
from uuid import UUID

from ..context import ActionContext
from ..models import Operation
from ..provider_client import McpCallResult
from ..provider_client import McpProviderClient
from .base import ProviderDisposition
from .base import ProviderObservation
from .base import ProviderReceipt
from .base import ProviderRequest
from .base import accepted_observation
from .base import initial_observation
from .base import json_objects
from .base import receipt_from_observation
from .base import request_ref
from .base import terminal_content

_UID = re.compile(r"(?:^|\n)UID:\s*(\S+)", re.IGNORECASE)
_URL = re.compile(r"(?:^|\n)URL:\s*(\S+)", re.IGNORECASE)


def _rfc3339(value):
    return value.isoformat().replace("+00:00", "Z")


class CalendarAdapter:
    def __init__(self, *, account_by_calendar: Mapping[str, str], duration_minutes: int = 30):
        self._accounts = dict(account_by_calendar)
        if not 1 <= duration_minutes <= 480:
            raise ValueError("calendar duration must be between 1 and 480 minutes")
        self._duration = timedelta(minutes=duration_minutes)

    def validate(self, context: ActionContext) -> None:
        if context.operation not in {Operation.CALENDAR_CREATE, Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE}:
            raise ValueError("calendar adapter requires a calendar operation")
        if context.target.kind != "calendar" or context.target.target_id not in self._accounts:
            raise ValueError("calendar account is not configured")
        if context.operation in {Operation.CALENDAR_UPDATE, Operation.CALENDAR_DELETE} and (
            not context.calendar_event_uid or not context.calendar_event_url or not context.calendar_event_etag
        ):
            raise ValueError("calendar update/delete requires exact UID, URL, and etag")

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest:
        self.validate(context)
        calendar = context.target.target_id
        common = {"account_id": self._accounts[calendar], "calendar": calendar}
        if context.operation is Operation.CALENDAR_CREATE:
            assert context.appointment_slot is not None
            arguments = {
                **common,
                "uid": str(action_uid),
                "summary": f"Tour — {context.prospect_name or context.prospect_id}",
                "description": context.arguments.get("description"),
                "location": context.property_label,
                "start": _rfc3339(context.appointment_slot),
                "end": _rfc3339(context.appointment_slot + self._duration),
                "all_day": False,
            }
            tool = "calendar_create_event"
        elif context.operation is Operation.CALENDAR_UPDATE:
            assert context.appointment_slot is not None
            arguments = {
                **common,
                "event_url": context.calendar_event_url,
                "etag": context.calendar_event_etag,
                "description": context.arguments.get("description"),
                "location": context.property_label,
                "start": _rfc3339(context.appointment_slot),
                "end": _rfc3339(context.appointment_slot + self._duration),
                "all_day": False,
            }
            tool = "calendar_update_event"
        else:
            arguments = {
                **common,
                "event_url": context.calendar_event_url,
                "etag": context.calendar_event_etag,
            }
            tool = "calendar_delete_event"
        return ProviderRequest("agent-email", tool, {key: value for key, value in arguments.items() if value is not None})

    async def invoke(self, client: McpProviderClient, request: ProviderRequest) -> ProviderObservation:
        return self._parse(await client.call(request.server_name, request.tool, request.arguments))

    async def poll(self, client: McpProviderClient, observation: ProviderObservation) -> ProviderObservation:
        if not observation.provider_request_ref:
            return ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_request_ref_missing")
        result = await client.call("agent-email", "request_status", {"request_id": observation.provider_request_ref})
        return self._parse(result, prior_ref=observation.provider_request_ref)

    def parse_receipt(self, context: ActionContext, observation: ProviderObservation) -> ProviderReceipt | None:
        self.validate(context)
        return receipt_from_observation(observation)

    async def reconcile(
        self,
        client: McpProviderClient,
        context: ActionContext,
        action_uid: UUID,
        observation: ProviderObservation,
    ) -> ProviderObservation:
        if observation.provider_request_ref:
            polled = await self.poll(client, observation)
            if polled.disposition is not ProviderDisposition.AMBIGUOUS:
                return polled
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "calendar_reconciliation_inconclusive",
            provider_request_ref=observation.provider_request_ref,
        )

    @staticmethod
    def _parse(result: McpCallResult, *, prior_ref: str | None = None) -> ProviderObservation:
        common = initial_observation(result)
        if common is not None:
            if prior_ref and common.provider_request_ref is None:
                return ProviderObservation(common.disposition, common.detail_code, provider_request_ref=prior_ref)
            return common
        payload = terminal_content(result.structured_content)
        ref = request_ref(result.structured_content) or prior_ref
        # Agent Email's async queue surfaces the created/updated event as a
        # structured object (data.event), not as formatted text. Prefer
        # reading the event fields directly out of the payload -- walking
        # nested objects the way CliqAdapter does -- before falling back to
        # regexing the human-formatted status text below.
        for item in json_objects(payload):
            event_uid = item.get("uid")
            if not isinstance(event_uid, str) or not event_uid.strip():
                continue
            event_uid = event_uid.strip()
            evidence = {"kind": "calendar_uid", "calendar_event_uid": event_uid}
            event_url = item.get("event_url")
            if isinstance(event_url, str) and event_url.strip():
                evidence["event_url"] = event_url.strip()
            etag = item.get("etag")
            if isinstance(etag, str) and etag.strip():
                evidence["calendar_event_etag"] = etag.strip()
            return accepted_observation(request_ref_value=ref, message_id=event_uid, evidence=evidence)
        # calendar_delete_event's payload has no event object and no `uid` --
        # only a deletion receipt (verified live: {"deleted": true, "event_url": ...}).
        # Derive the uid from the URL basename, the same CalDAV convention used
        # elsewhere (.../events/<uid>.ics -> <uid>).
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping) and data.get("deleted") is True:
                event_url = data.get("event_url")
                if isinstance(event_url, str) and event_url.strip():
                    event_url = event_url.strip()
                    basename = event_url.rstrip("/").rsplit("/", 1)[-1]
                    if basename.lower().endswith(".ics"):
                        basename = basename[: -len(".ics")]
                    if basename:
                        return accepted_observation(
                            request_ref_value=ref,
                            message_id=basename,
                            evidence={
                                "kind": "calendar_uid",
                                "calendar_event_uid": basename,
                                "event_url": event_url,
                            },
                        )
        text = result.text or ""
        if payload:
            data = payload.get("data")
            if isinstance(data, Mapping):
                content = data.get("content")
                if isinstance(content, list):
                    text = "\n".join(str(item.get("text")) for item in content if isinstance(item, Mapping) and item.get("type") == "text")
        uid = _UID.search(text)
        url = _URL.search(text)
        if uid:
            message_id = uid.group(1)
            return accepted_observation(
                request_ref_value=ref,
                message_id=message_id,
                evidence={"kind": "calendar_uid", "calendar_event_uid": message_id, **({"event_url": url.group(1)} if url else {})},
            )
        if text.startswith("**Event Deleted**") and url:
            return accepted_observation(
                request_ref_value=ref,
                message_id=url.group(1),
                evidence={"kind": "calendar_delete", "event_url": url.group(1)},
            )
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "malformed_provider_success",
            provider_request_ref=ref,
        )
