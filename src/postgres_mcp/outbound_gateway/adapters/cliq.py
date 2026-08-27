"""Agent Email Cliq adapters."""

from __future__ import annotations

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


class CliqAdapter:
    def __init__(self, operation: Operation):
        if operation not in {Operation.CLIQ_CHANNEL_POST, Operation.CLIQ_CHAT_POST}:
            raise ValueError("Cliq adapter requires a Cliq operation")
        self._operation = operation

    def validate(self, context: ActionContext) -> None:
        if context.operation is not self._operation or not context.target.verified:
            raise ValueError("Cliq operation or target mismatch")

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest:
        self.validate(context)
        channel = self._operation is Operation.CLIQ_CHANNEL_POST
        return ProviderRequest(
            server_name="agent-email",
            tool="cliq_channel_bot_post" if channel else "cliq_chat_post",
            arguments={
                "channel_unique_name" if channel else "chat_id": context.target.target_id,
                "text": str(context.arguments["text"]),
                "sync_message": True,
            },
        )

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
            "cliq_reconciliation_inconclusive",
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
        for item in json_objects(payload):
            status = item.get("status")
            message_id = item.get("provider_message_id")
            if status == "sent" and isinstance(message_id, str) and message_id.strip():
                return accepted_observation(request_ref_value=ref, message_id=message_id.strip())
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "malformed_provider_success",
            provider_request_ref=ref,
        )
