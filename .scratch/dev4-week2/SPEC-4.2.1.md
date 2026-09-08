# SPEC — 4.2.1 Dual-Path Speed-to-Lead Ingest & 30-min SLA

Locked via grilling 2026-09-07. Covers map tickets 05 (schema), 07 (orchestrator),
08 (SLA response). Branch: `feature/week2-4.2.1-speed-to-lead-ingest`.

## 1. `inbound_messages` schema (ticket 05) — ✅ FINALIZED (Dev 2 relay received 2026-09-07)

New **tenant-bearing** table → register in `config/tenant_policies.py` + push through
`apply_rls_policies.py`. Migration `migrations/apply_inbound_messages.py`.

Superset table: all Dev 4 ingest + all Dev 2 triage columns at CREATE TABLE time (nullable
where not required by both). Dev 2 confirmed this approach — supersedes their three incremental
ALTER TABLE migration files; those must NOT run on a DB where this migration has already run.

**Dev 2 relay answers (2026-09-07):**
- `sla_due_at` is Dev 2's rep-claim SLA (HOT_LEAD/WHALE_OWNER = +15 min, else +60 min).
  Dev 4's ingest SLA uses separate column `lead_sla_due_at` (received_at + 30 min) — two different
  business clocks, two different sweeps, no shared column.
- `source_channel` is Dev 4's column; triage doesn't use it.
- Dev 2 classifier must filter `WHERE channel = 'EMAIL'` to skip non-email inbound rows.

| Column | Owner | Notes |
|---|---|---|
| `message_id` (UUID PK) | shared | |
| `client_id` (FK clients) | shared | NOT NULL — RLS direct mode |
| `contact_id` (FK contacts) | shared | nullable if unresolvable |
| `channel` | Dev 4 | EMAIL / WEBHOOK — Dev 2 filters on this |
| `source_channel` | Dev 4 | WEBSITE_FORM / LISTING_PORTAL / APM / … |
| `raw_payload` | Dev 4 | as received |
| `cleaned_body` | Dev 4 | parsed |
| `received_at` | Dev 4 | |
| `send_at` | Dev 4 | now if in-hours, else next business-open |
| `lead_sla_due_at` | Dev 4 | received_at + 30 min |
| `ack_latency_seconds` | Dev 4 | measured on response dispatch |
| `dedupe_key` (UNIQUE per client) | Dev 4 | Path A external_id / Path B Mailgun Message-Id |
| `utm` (JSONB) | Dev 4 | |
| `status` | shared | RECEIVED/RESPONDED/SUPPRESSED/DEFERRED (Dev 4) + PENDING/PROCESSING/ROUTED/ESCALATED/REALLOCATED/FAILED (Dev 2) |
| `requires_human_review` | shared | |
| `intent` | Dev 2 | HOT_LEAD/QUESTION/OBJECTION/LATER/NURTURE/UNSUBSCRIBE/COMPLAINT/LEGAL_GRIEF/WHALE_OWNER/PARTNER |
| `intent_confidence` NUMERIC(4,3) | Dev 2 | |
| `classified_at` | Dev 2 | |
| `classification_meta` JSONB | Dev 2 | reasoning, objection_subtype, path |
| `sla_due_at` | Dev 2 | rep-claim deadline (set at classification) |
| `card_posted_at` | Dev 2 | |
| `card_ts`, `card_channel_id` | Dev 2 | Slack card tracking |
| `claimed_at`, `claimed_by` | Dev 2 | rep claim |
| `escalation_level` SMALLINT | Dev 2 | 0–3 |

## 2. Dual-path ingest orchestrator (ticket 07)

Both paths → one orchestrator → one `inbound_messages` write + `inbound_lead_received` event.

**Path A — webhook** `POST /api/v1/webhooks/inbound-lead`
- Auth (Q8): per-client shared secret (header or path token) → resolves `client_id`. Fail closed.
- Payload (Q10): `{client_secret, prospect_name, email, phone, property_address, inquiry_text,
  source (WEBSITE_FORM|LISTING_PORTAL), utm?}`. **422 if neither email nor phone.**
- Budget: 2s.

**Path B — email parse** `leads@{client-subdomain}.getblackink.com`
- Transport: **Mailgun Routes** (ticket 06). Verify HMAC signature BEFORE any DB write.
- `client_id` from subdomain slug → clients lookup (Q2). Reject if unresolved.
- Budget: 5s. (Portal-specific parsers = 4.2.3, out of this subtask.)

**Client resolution & RLS (PR-review fix):** `clients` is RLS-scoped, so an
unscoped session sees zero rows. Both paths resolve `client_id` through a
`SECURITY DEFINER` function (`resolve_client_by_webhook_secret` /
`resolve_client_by_subdomain`, apply_clients_stl_fields.py) — same pre-tenant
pattern as `resolve_calendar_connection`. Then open `session_scope(client_id)`.

**Entity modeling (PR-review fix):** an inbound prospect is a renter/owner
inquiring — NOT a PM-firm prospect — so nothing is written to
`companies`/`contacts` (those require `domain` + `county_slug` and model
outbound prospect firms). Prospect name/email/phone/address live directly on
`inbound_messages`; `contact_id` stays NULL for inbound leads.

**Shared pipeline (both paths):**
1. Resolve `client_id` via SECURITY DEFINER fn (reject if unresolved — Q2).
2. Dedupe on `dedupe_key` (Q5) — duplicate = no-op, no second response.
   Path A fallback key is deterministic (hash of tenant+source+contact+content),
   NOT a random UUID, so a source retry collapses onto the same row.
3. Non-poach gate (Q4, advisory): look up an EXISTING company by sender email
   domain; `is_claimed_by_other_client` → on match write `non_poach_suppressed`,
   status=SUPPRESSED, **no response**. No existing company → proceed. Never
   creates a company.
   *(Open: is inbound non-poach strict-block or advisory? confirm.)*
5. Write `inbound_messages` + `inbound_lead_received` event (carries `source_channel`).
6. Fire closer-alert Slack card to `#blackink-setter` **immediately** (Q12): prospect name,
   company if any, inquiry text, source, local time, client_id. Post-only, no buttons.
7. Schedule the auto-response (see §3).

## 3. 30-min SLA auto-response (ticket 08)

- Business hours (Q6c): **fixed ET**, e.g. 08:00–18:00 Mon–Fri. Single config constant, seam
  to go per-client later.
- `send_at` (Q7a): immediate if within hours, else next business-open. A sweep/worker
  (reuse `sequence_sweep` pattern) dispatches due responses. On dispatch set
  `ack_latency_seconds`, status=RESPONDED, emit `speed_to_lead_response_sent`.
- `sla_due_at = received_at + 30 min` (business hours).
- Content (Q9a): **static per-client template + merge fields** (name, property) + booking link
  via `booking_link.py`. No LLM in the SLA path. **Email only — no SMS.**

## 4. Events to register in `events.py` (Q11)

- `inbound_lead_received` — both paths, `source_channel` in payload.
- `non_poach_suppressed` — on gate match.
- `speed_to_lead_response_sent` — on auto-reply dispatch.
- closer alert — reuse existing Slack-card event if present, else add `closer_alert_posted`.
- (No overlap with Dev 2's `inbound_reply_classified` — that's replies to outbound, not fresh leads.)

## Build order note
4.2.1 lands the `inbound_messages` schema + orchestrator that **4.2.2 and 4.2.3 depend on** —
merge 4.2.1 before branching those.
