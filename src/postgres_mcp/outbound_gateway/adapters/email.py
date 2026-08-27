"""Agent Email adapter."""

from __future__ import annotations

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
from .base import mcp_text_parts
from .base import receipt_from_observation
from .base import request_ref
from .base import terminal_content


class EmailAdapter:
    def __init__(
        self,
        *,
        sender_domains: Mapping[str, str],
        cc_by_source: Mapping[str, str] | None = None,
        reconciliation_poll_attempts: int = 3,
    ):
        self._sender_domains = dict(sender_domains)
        self._cc_by_source = dict(cc_by_source or {})
        self._reconciliation_poll_attempts = max(
            1,
            min(reconciliation_poll_attempts, 5),
        )

    def validate(self, context: ActionContext) -> None:
        if context.operation is not Operation.EMAIL_SEND:
            raise ValueError("email adapter requires email.send")
        if context.target.kind != "email_thread" or not context.target.verified:
            raise ValueError("email adapter requires a verified email target")
        if context.provider_account not in self._sender_domains:
            raise ValueError("email sender domain is not configured")

    def build_request(self, context: ActionContext, action_uid: UUID) -> ProviderRequest:
        self.validate(context)
        subject = context.source_subject or "Rental inquiry"
        if not subject.casefold().startswith("re:"):
            subject = f"Re: {subject}"
        arguments = {
            "account_id": context.provider_account,
            "to": [{"address": context.target.target_id}],
            "subject": subject,
            "text": str(context.arguments["text"]),
            "outbound_action_uid": str(action_uid),
        }
        copy_address = self._cc_by_source.get(context.source)
        if copy_address:
            arguments["cc"] = [{"address": copy_address}]
        return ProviderRequest(
            server_name="agent-email",
            tool="email_send",
            arguments=arguments,
        )

    async def invoke(self, client: McpProviderClient, request: ProviderRequest) -> ProviderObservation:
        return self._parse(await client.call(request.server_name, request.tool, request.arguments))

    async def poll(self, client: McpProviderClient, observation: ProviderObservation) -> ProviderObservation:
        if not observation.provider_request_ref:
            return ProviderObservation(ProviderDisposition.AMBIGUOUS, "provider_request_ref_missing")
        result = await client.call(
            "agent-email",
            "request_status",
            {"request_id": observation.provider_request_ref},
        )
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
        domain = self._sender_domains[context.provider_account]
        message_id = f"<outbound-action-{action_uid}@{domain}>"
        lookup = await client.call(
            "agent-email",
            "email_get_thread",
            {"account_id": context.provider_account, "messageId": message_id, "folder": "Sent"},
        )
        first = initial_observation(lookup)
        for _ in range(self._reconciliation_poll_attempts):
            if not first or first.disposition is not ProviderDisposition.PENDING:
                break
            if not first.provider_request_ref:
                break
            lookup = await client.call(
                "agent-email",
                "request_status",
                {"request_id": first.provider_request_ref},
            )
            first = initial_observation(lookup)
        if any(part.lstrip().startswith("**Thread:**") for part in mcp_text_parts(lookup)):
            return accepted_observation(
                request_ref_value=observation.provider_request_ref,
                message_id=message_id,
                detail_code="email_reconciled_by_message_id",
                evidence={"kind": "exact_message_id", "provider_message_id": message_id},
            )
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "email_reconciliation_inconclusive",
            provider_request_ref=observation.provider_request_ref,
        )

    @staticmethod
    def _parse(result: McpCallResult, *, prior_ref: str | None = None) -> ProviderObservation:
        common = initial_observation(result)
        if common is not None:
            if prior_ref and common.provider_request_ref is None:
                return ProviderObservation(
                    common.disposition,
                    common.detail_code,
                    provider_request_ref=prior_ref,
                    provider_call_id=common.provider_call_id,
                    message_id=common.message_id,
                    accepted_at=common.accepted_at,
                    category=common.category,
                    retryable=common.retryable,
                    evidence=common.evidence,
                )
            return common
        payload = result.structured_content
        content = terminal_content(payload)
        message_id = content.get("provider_message_id") if content else None
        status = content.get("status") if content else None
        ref = request_ref(payload) or prior_ref
        if status == "success" and isinstance(message_id, str) and message_id.strip():
            return accepted_observation(request_ref_value=ref, message_id=message_id.strip())
        return ProviderObservation(
            ProviderDisposition.AMBIGUOUS,
            "malformed_provider_success",
            provider_request_ref=ref,
        )
