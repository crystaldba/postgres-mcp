# Archived Provider-Policy Layer

## Purpose

This layer implemented staged-rollout blast-radius control keyed by inbound provider. It maintained separate allowlists of enabled operations and intents per inbound message provider (zillow, hotpads, quo, tenantcloud) to permit gradual feature rollout without affecting all providers simultaneously.

The policy matrices (`DEFAULT_ENABLED_OPERATIONS_BY_PROVIDER`, `DEFAULT_ENABLED_INTENTS_BY_PROVIDER`) gated outbound action execution: RoutingPolicy held the parsed env values, the action context loader checked them against the inbound provider, and the service rejected operations/intents not in the allowlist with `provider_operation_disabled`.

## Observed Failure (Wake 25789, 2026-08-24)

**Applicant:** Melody Haddix (Zillow)
**Scenario:** Application completed notice arrived from `no-reply@comet.zillow.com` via `zoho_mail` (Zoho's mail routing), with no convo.zillow.com proxy address in the message headers.

**Root cause chain:**
1. The `_provider()` derivation in context.py (still live) fell through and returned `"zoho_mail"` (the inbound relay provider).
2. The provider→operations map had no entry for `"zoho_mail"` → empty allowlist.
3. The internal escalation action (role: `internal_notification`, to Cliq) checked the allowlist, found no operations for `"zoho_mail"`, and rejected it with `provider_operation_disabled`.
4. The escalation failure left no other fallback (the primary action had already committed as failed).
5. The wake was silently quarantined (`effect_quarantined`), and the applicant withdrew 2 minutes later with no response ever sent.

**Impact:** Silent escalation suppression, applicant response lost forever.

## Revival Requirements

If the policy layer is ever needed again, it must address these gaps:

1. **Key by recipient class, not inbound provider:** Operations and intents should be keyed by the intended recipient role (e.g., "prospect_reply", "internal_notification") or by the outbound target provider (agent-email, quo, cliq, tenantcloud), not the inbound provider.

2. **Explicit default for unmapped entries:** The default fallback must be explicit and tested, not an empty allowlist.

3. **Exemption for `internal_notification`:** Internal escalations should always be attempted, regardless of provider, or a separate gate should cover only prospect-facing actions.

4. **Alerting on empty-allowlist rejection:** Any rejection due to an empty allowlist should be logged as an error (not silently quarantined) with the provider and operation details for operator visibility.

## Environment Variables (Last Production Values)

The following env vars fed the policy layer. Last observed production values are captured below (as of 2026-08-27, before archival):

```
OUTBOUND_ENABLED_INTENTS_JSON=["inquiry_reply","showing_offer","tenantcloud_lead_status","tenantcloud_maintenance_create","tenantcloud_maintenance_status","lead_alert","manual_review_alert","showing_create","showing_update","showing_delete"]
OUTBOUND_PROVIDER_OPERATIONS_JSON={"zillow":["email.send","quo.sms.send","cliq.channel.post","cliq.chat.post","calendar.create","calendar.update","calendar.delete"],"hotpads":["email.send","cliq.channel.post","cliq.chat.post","calendar.create","calendar.update","calendar.delete"],"quo":["quo.sms.send","cliq.channel.post","cliq.chat.post","calendar.create","calendar.update","calendar.delete"],"tenantcloud":["tenantcloud.message.send","tenantcloud.lead.status.update","tenantcloud.maintenance.create","tenantcloud.maintenance.status.update","cliq.channel.post","cliq.chat.post","calendar.create","calendar.update","calendar.delete"]}
OUTBOUND_PROVIDER_INTENTS_JSON={"zillow":["inquiry_reply","showing_offer","lead_alert","manual_review_alert","showing_create","showing_update","showing_delete"],"hotpads":["inquiry_reply","showing_offer","lead_alert","manual_review_alert","showing_create","showing_update","showing_delete"],"quo":["inquiry_reply","showing_offer","lead_alert","manual_review_alert","showing_create","showing_update","showing_delete"],"tenantcloud":["inquiry_reply","tenantcloud_lead_status","tenantcloud_maintenance_create","tenantcloud_maintenance_status","lead_alert","manual_review_alert","showing_create","showing_update","showing_delete"]}
```

These env vars are **no longer read** by the gateway as of 2026-08-27. Gateway request validation now depends on pydantic shape validation (`ExecuteRequest` validates that `arguments` matches the shape its `operation` requires -- it does not cross-check `action_role`/`operation`/`intent_kind` against each other) and the kill switch (`FeaturePolicy.kill_switch`) rather than runtime policy matrices. Fine-grained traffic control (recipient lease + context-staleness gating) shipped in Task 6 (2026-08-27).
