# Design the dual-path ingest orchestrator

Type: grilling
Status: resolved
Blocked by: 05, 06

## Answer

Design locked via grilling 2026-09-07 — see `.scratch/dev4-week2/SPEC-4.2.1.md` §2.
Dual-path (webhook + Mailgun `leads@`) → one orchestrator → `inbound_messages` +
`inbound_lead_received`. Path A auth = per-client secret (fail closed); payload requires
email or phone (422 else). Shared pipeline: resolve client_id → dedupe → upsert contact →
non-poach gate → write + event → immediate closer card → schedule response. Events:
`inbound_lead_received`, `non_poach_suppressed`, `speed_to_lead_response_sent`.

## Question

Both inbound paths must land in one orchestrator writing `inbound_messages` + an
`inbound_lead_received` event (`source_channel` field; event type undefined today — must be
added to `events.py` registry).

Decide: Path A webhook endpoint (`/api/v1/webhooks/inbound-lead`) — auth/signature,
required-field validation (phone/email), 2s processing budget; Path B intake shape from the
chosen transport (5s budget); where the non-poach gate runs (reuse
`compliance_gate.is_claimed_by_other_client`) and what `non_poach_suppressed` blocks; how
`client_id` is resolved from an inbound payload/subdomain; idempotency for duplicate
deliveries. Output: the orchestrator seam both paths share.
