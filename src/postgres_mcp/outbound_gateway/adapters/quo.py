"""Quo SMS adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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


class QuoSmsAdapter:
    def __init__(self, *, user_id: str):
        if not user_id.strip():
            raise ValueError("Quo user ID is required")
        self._user_id = user_id

    def validate(self, context: ActionContext) -> None:
        if context.operation is not Operation.QUO_SMS_SEND:
            raise ValueError("Quo adapter requires quo.sms.send")
        if context.target.kind != "quo_conversation" or not context.target.verified:
            raise ValueError("Quo adapter requires a verified conversation")
        if not context.recipient_phone or not context.provider_account:
            raise ValueError("Quo line and recipient phone are required")

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest:
        self.validate(context)
        return ProviderRequest(
            server_name="quo",
            tool="send_message",
            arguments={
                "phone_number_id": context.provider_account,
                "to": context.recipient_phone,
                "user_id": self._user_id,
                "content": str(context.arguments["text"]),
            },
        )

    async def invoke(self, client: McpProviderClient, request: ProviderRequest) -> ProviderObservation:
        return self._parse(await client.call(request.server_name, request.tool, request.arguments))

    async def poll(self, client: McpProviderClient, observation: ProviderObservation) -> ProviderObservation:
        return observation

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
        result = await client.call(
            "quo",
            "list_messages",
            {
                "phone_number_id": context.provider_account,
                "participant": context.recipient_phone,
                "max_results": 50,
            },
        )
        if initial_observation(result) is not None:
            return ProviderObservation(
                ProviderDisposition.AMBIGUOUS,
                "quo_reconciliation_inconclusive",
                provider_request_ref=observation.provider_request_ref,
            )
        expected_text = str(context.arguments["text"])
        for item in json_objects(result.structured_content):
            if not self._matches(item, context, expected_text):
                continue
            message_id = self._message_id(item)
            if message_id:
                return accepted_observation(
                    request_ref_value=observation.provider_request_ref,
                    message_id=message_id,
                    detail_code="quo_reconciled_by_exact_tuple",
                    evidence={"kind": "exact_target_content_time", "provider_message_id": message_id},
                )
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "quo_reconciliation_inconclusive",
            provider_request_ref=observation.provider_request_ref,
        )

    @staticmethod
    def _message_id(payload: Mapping[str, Any]) -> str | None:
        for key in ("message_id", "id", "provider_message_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _matches(payload: Mapping[str, Any], context: ActionContext, expected_text: str) -> bool:
        direction = str(payload.get("direction") or payload.get("type") or "").casefold()
        if direction and direction not in {"outgoing", "outbound", "sent"}:
            return False
        target = payload.get("to") or payload.get("participant") or payload.get("phone_number")
        content = payload.get("content") or payload.get("text") or payload.get("body")
        if target != context.recipient_phone or content != expected_text:
            return False
        timestamp = payload.get("created_at") or payload.get("sent_at") or payload.get("timestamp")
        if not isinstance(timestamp, str):
            return False
        try:
            sent_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        return sent_at >= context.source_sent_at

    @classmethod
    def _parse(cls, result: McpCallResult) -> ProviderObservation:
        common = initial_observation(result)
        if common is not None:
            return common
        payload = result.structured_content
        ref = request_ref(payload)
        for item in json_objects(payload):
            message_id = cls._message_id(item)
            status = str(item.get("status") or "").casefold()
            if message_id and status in {"sent", "success", "accepted", "completed", ""}:
                return accepted_observation(request_ref_value=ref, message_id=message_id)
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "malformed_provider_success",
            provider_request_ref=ref,
        )
