# Design the inbound_messages schema

Type: grilling
Status: RESOLVED (Dev 2 relay received 2026-09-07)
Blocked by: —

## Question

Speed-to-Lead (4.2) has no `inbound_messages` table today — it's the foundational store for
every inbound path (webhook, email parse, pay-per-lead portals) and the cadence trigger.
It is a new **tenant-bearing** table → MUST be registered in `config/tenant_policies.py`
and pushed through `apply_rls_policies.py`, or it gets neither RLS nor leakage-test coverage.

Decide the schema: fields (`message_id`, `client_id`, `contact_id`, `channel`,
`source_channel` enum — `WEBSITE_FORM/LISTING_PORTAL/APM/MANAGE_MY_PROPERTY/THUMBTACK/...`,
`raw_payload`, `cleaned_body`, `sla_due_at`, `ack_latency_seconds`, `status`, `received_at`,
`requires_human_review`), which are NOT NULL, the `source_channel` allowed-value set, how
`sla_due_at`/`ack_latency_seconds` are computed and stored, and the RLS mode (direct
`client_id` vs join). Note Dev 2's triage spec also wants this table (`detected_intent`,
`confidence_score`) — reconcile ownership so the two devs don't collide on one migration.

## Progress (2026-09-07)

Column set designed — see `.scratch/dev4-week2/SPEC-4.2.1.md` §1. Decision: **one superset
table** owned by this migration (`apply_inbound_messages.py`), Dev 2's `detected_intent` /
`confidence_score` included as nullable. Tenant-bearing → register in `TENANT_POLICIES` + RLS
(direct `client_id`).

**PARKED on Dev 2 relay** before the migration is finalized:
1. OK for me to own the migration with your columns nullable up front?
2. Your full triage column list + types.
3. Who writes `sla_due_at` — my 30-min lead SLA, your reply SLA, or both?
4. Does `source_channel` need reply-channel values too?
