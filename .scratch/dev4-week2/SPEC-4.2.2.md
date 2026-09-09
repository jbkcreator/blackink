# SPEC — 4.2.2 Six-Attempt Inbound Cadence (Speed-to-Lead follow-up)

Locked via grilling 2026-09-08. Builds on 4.2.1 (dual-path ingest + 30-min
SLA auto-response). Depends on the deployed `inbound_messages` table and the
`agent_work_orders` scheduling engine. Suggested branch:
`feature/week2-4.2.2-stl-cadence`.

## 0. Source-of-truth reconciliation (read first)

The Week2 task-split doc specifies "5 follow-up touches via GHL sequence,
no human approval." Both halves conflict with the client blueprint v2, which
is authoritative:

- **GHL is booking-only in v2.** v2 mentions GoHighLevel *only* as a calendar
  fallback (onboarding State 6; booking-flow diagram) for a client with
  neither Google nor Microsoft calendar. v2 never describes GHL as an
  outbound sequence/enrollment tool, and there is **no outbound GHL API
  client anywhere in the repo** (every GHL path is inbound-webhook-only).
  → **GHL-arming is OUT OF SCOPE.** Cadence touches send through the same
  internal SMTP path 4.2.1 already uses (`mailbox_dispatcher` +
  `email_sender`), "from the client's configured domain."
- **v2 requires human approval per send in early client phase** (pricing/tier
  tables: "Human approval required for each send in early client phase"). The
  task doc's "no approval for cadence touches" is overridden. At the
  September pilot every founding client *is* in early phase and none graduates
  within this build's horizon → **ship the human-approval path only** this
  year. Cadence touches post approval cards via the existing
  `sequence_sweep` work-order card mechanism, exactly like cold-outbound
  touches. A `clients.respond_early_phase` column is reserved for a future
  auto-send path but is **not wired** now.

Also note: the six-attempt cadence itself is **not** in v2 — v2's
Speed-to-Lead recipe is only the 30-min auto-response + Slack closer alert.
The cadence exists solely in the task-split doc, so it is treated as an
internal follow-up on top of the client-sanctioned auto-response, not a
client-mandated GHL flow.

## 1. Domain model (see `CONTEXT.md`)

- **Message-anchored cadence.** An STL lead has **no `contacts` row** and no
  `contact_id` (prospects are renters/owners; 4.2.1 stores everything inline
  on `inbound_messages`, `contact_id` conceptually NULL — the column does not
  exist). The cadence's durable identity is **`inbound_messages.id`**, never a
  `contact_id`. Precedent: the Win-Back sequencer keys on `winback_row_id`,
  the existing non-contact cadence; the contact-keyed `sequence_runs` /
  `sequence_touch_dispatches` engine is **not** reused.
- **Cadence** = the initial 60-second acknowledgement (the 4.2.1
  auto-response) plus, if the lead is quiet 24h, five daily follow-up touches
  (Day 1–5, one per day).
- **Stop** = termination on any inbound reply (any sentiment), booking, or
  opt-out. Recorded as a stop **event** + a **latch** on `inbound_messages`.

## 2. Schema changes (one additive migration)

`migrations/apply_stl_cadence.py` — idempotent `ADD COLUMN IF NOT EXISTS` /
`CREATE TABLE IF NOT EXISTS`, runs after `apply_inbound_messages_lead_fields.py`
and before `apply_rls_policies.py`. Add to CLAUDE.md migration block and to
both migration-running CI workflows (`tests.yml`,
`tenant_leakage_nightly.yml`) — see `test_migration_coverage.py`.

### 2a. Stop latch on `inbound_messages`
```
ADD COLUMN IF NOT EXISTS cadence_state       VARCHAR(20)   -- NULL | ARMED | STOPPED | COMPLETED
ADD COLUMN IF NOT EXISTS stopped_at          TIMESTAMPTZ
ADD COLUMN IF NOT EXISTS stop_reason         VARCHAR(20)   -- REPLY | BOOKED | OPT_OUT
```
`cadence_state` distinguishes "arm-check not yet run / lead still in ack
window" (NULL) from ARMED (touches scheduled), STOPPED (latched), COMPLETED
(all 5 touches done). The sweep and arm-check read these; they are cheap and
indexed.
```
CREATE INDEX IF NOT EXISTS ix_inbound_messages_cadence_active
  ON inbound_messages (cadence_state) WHERE cadence_state = 'ARMED';
```

### 2b. At-most-once send guard: `stl_cadence_dispatches`
Mirrors `sequence_touch_dispatches` (the winback/cold-outbound pattern), but
keyed on the message, not a run/contact.
```
CREATE TABLE IF NOT EXISTS stl_cadence_dispatches (
  id           BIGSERIAL PRIMARY KEY,
  client_id    VARCHAR(40) NOT NULL,
  message_id   BIGINT      NOT NULL REFERENCES inbound_messages(id),
  touch_step   SMALLINT    NOT NULL,          -- 1..5
  status       VARCHAR(20) NOT NULL,          -- SENDING | SENT | FAILED
  mailbox_id   BIGINT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (message_id, touch_step)
);
```
Claim = `INSERT ... ON CONFLICT (message_id, touch_step) DO NOTHING` before
send (at-most-once). **Tenant-bearing** → register in
`config/tenant_policies.py` (join-scoped via `message_id → inbound_messages`,
or direct `client_id` — use direct `client_id` for a simple RLS policy) and
push through `apply_rls_policies.py`.

### 2c. Reserved (not wired)
```
ALTER TABLE clients ADD COLUMN IF NOT EXISTS respond_early_phase BOOLEAN NOT NULL DEFAULT TRUE;
```
Placeholder for the future auto-send-vs-approval switch. This build treats
every client as early-phase (approval always required); nothing reads this
column yet.

## 3. Arm: deferred +24h check

On `inbound_lead_received` (4.2.1 orchestrator), enqueue **one** deferred
`agent_work_orders` row:
- `entity_type='inbound_message'`, `entity_id=message_id`
- `due_at = received_at + INTERVAL '24 hours'`
- `idempotency_key = f"stl-cadence-arm:{message_id}"`
- a distinct order kind/type marking it a cadence arm-check.

The existing `wo.due_batch()` / cadence sweep picks it up when due. Its
handler (`src/services/stl_cadence.py::run_arm_check`) runs under
`get_db_context(client_id=...)` and:
1. Re-reads the lead. If `cadence_state='STOPPED'` already, or a stop event
   exists (reconciliation backstop, §5), or the lead never reached RECEIVED
   → no-op, mark the arm-check order DONE.
2. **Stop reconciliation scan** (arm-check only): query `events` for
   `inbound_reply_received` / `meeting_booked` on
   `(entity_type='inbound_message', entity_id=message_id)` in the 24h window;
   if any, latch STOPPED and no-op. This is the belt to §5's suspenders — it
   catches a stop that fired before any latch-writer ran.
3. Otherwise set `cadence_state='ARMED'` and enqueue **5** touch work orders
   (`stl-cadence-touch`), `due_at = received_at + N days` for N=1..5,
   `idempotency_key = f"stl-cadence:{message_id}:touch:{N}"`.

Enqueue-upfront (all 5 at arm time) mirrors the cold sequencer; per-touch
pre-send re-checks (§4) handle stops that fire mid-cadence.

## 4. Dispatch: human-approved, internal SMTP

Cadence touch work orders flow through the **existing `sequence_sweep`
approval-card path** — no new autonomous sweep this year (v2 §0). For each due
`stl-cadence-touch` order:
1. `sequence_sweep` posts an approval card to Slack (same mechanism as
   cold-outbound touches).
2. On human approval, the work-order dispatcher runs the STL touch send:
   - **Pre-send stop re-check:** if `cadence_state='STOPPED'` (or stop event
     present) → cancel this touch order (`CANCELLED`), do not send.
   - Claim `stl_cadence_dispatches (message_id, touch_step)` via
     `INSERT ... ON CONFLICT DO NOTHING`; if the row already exists (SENT),
     skip (at-most-once).
   - Pick mailbox via `get_active_mailbox_for_client` (same cap-counting,
     domain-quarantine, LRU as 4.2.1). `AllMailboxesCapped` → DEFER (push
     `due_at` forward), not dead-letter.
   - Send via `email_sender` with the mandatory one-click unsubscribe
     (CLAUDE.md hard requirement) — the token encodes `inbound_messages.id`
     (§6).
   - On success: `stl_cadence_dispatches.status='SENT'`; log the touch event
     (§7). After touch 5 sends, set `cadence_state='COMPLETED'`.
   - Post-send-failure discipline (same class as 4.2.1's SENT_UNCONFIRMED):
     a failure *after* SMTP send must not re-send — mark the dispatch row
     terminal for reconciliation, never re-claim.

STL cadence touches count against the same per-mailbox rolling-24h cap as the
4.2.1 auto-response and the cold sequencer (already summed in the
`mailbox_dispatcher` capacity subquery — extend it to include
`stl_cadence_dispatches` SENT rows in the 24h window, or count via events).

## 5. Stop: latch + writer (authoritative for the sweep)

Latch is what every sweep/dispatch tick reads. A small **stop-writer** sets it
the moment a stop signal fires for the message:
- **Reply:** `inbound_reply_received` for the message → latch
  `stopped_at=NOW(), stop_reason='REPLY', cadence_state='STOPPED'`.
- **Booking:** `meeting_booked` for the message → `stop_reason='BOOKED'`.
- **Opt-out:** unsubscribe click (§6) → `stop_reason='OPT_OUT'`.

On latching STOPPED, cancel all still-`QUEUED` `stl-cadence-touch` work orders
for the message (§8). The arm-check's ledger scan (§3.2) is the reconciliation
backstop for a stop that beat the writer.

The stop-writer hooks the same points that already emit these events
(`inbound_ingest` for replies, `booking_ingest` for bookings) — extended to
also latch the message when the entity is an STL `inbound_message`. No new
event-consumer daemon.

## 6. Opt-out for a contactless lead

Cadence emails carry the mandatory one-click unsubscribe. The unsubscribe
handler normally sets `contacts.is_opted_out` — but an STL lead has **no
contacts row**. So the cadence-touch unsubscribe token encodes the
`inbound_messages.id`; clicking it:
- writes the stop latch (`stopped_at`, `stop_reason='OPT_OUT'`,
  `cadence_state='STOPPED'`) on that message row, and
- emits a `speed_to_lead_opted_out` event
  (`entity_type='inbound_message'`, `entity_id=message_id`).

No `contacts` row is created (that would reopen 4.2.1's domain+county NOT-NULL
problem); no shared suppression table this task. The mechanism differs from
cold-outbound unsubscribe only in *which* row it latches.

## 7. Event logging shape

`events` has no `campaign_type`/`touch_step` columns (generic polymorphic
ledger). Each dispatched touch logs:
- `event_type='outbound_touch_dispatched'` (column)
- `entity_type='inbound_message'`, `entity_id=message_id`
- `payload={ "campaign_type": "SPEED_TO_LEAD_CADENCE", "touch_step": N,
  "message_id": message_id }`

Matches how `sequence_sweep` already stores `touch_step` in payload. Covered
by `idx_events_entity (entity_type, entity_id)`.

## 8. Cancel semantics

Stop between touches → remaining touch work orders transition to
`status='CANCELLED'` (rows persist for audit), **not** hard-deleted. The DoD's
"job-queue inspection showing removed entries" is satisfied by their absence
from the active/due-queue query (`QUEUED AND due_at <= now`); a delete would
destroy the record that a cadence was armed and why it stopped — every other
job table in the repo uses terminal-status transitions.

## 9. Out of scope

- **GHL sequence-API arming** — no client mandate (v2 = booking-only), no
  existing outbound client. Revisit only if a client lacks sending domains.
- **Autonomous (no-approval) cadence dispatch** — deferred behind the reserved
  `clients.respond_early_phase` column; every client is early-phase this year.
- **A `contacts` row / shared email-suppression table for STL leads** — opt-out
  latches the message instead.
- **SMS** — none anywhere (v2: no SMS this year).

## 10. Definition of Done (mapped to task doc)

- [ ] Cadence arms 24h after receipt with no reply → 5 `stl-cadence-touch`
      work orders scheduled (job-queue inspection).
- [ ] Touch 1 (Day 1) dispatched from the client's configured domain via
      `mailbox_dispatcher`; event `outbound_touch_dispatched` with
      `payload.campaign_type='SPEED_TO_LEAD_CADENCE'`.
- [ ] Reply logged between touch 2 and 3 → touches 3–5 CANCELLED (absent from
      active queue).
- [ ] Opt-out (unsubscribe click) at any point → `cadence_state='STOPPED'`,
      `stop_reason='OPT_OUT'` on the message; remaining touches CANCELLED.
- [ ] GHL sequence ID NOT hardcoded — N/A (GHL out of scope; internal SMTP).
- [ ] No SMS in any touch — code review.
- [ ] Human approval required per touch (v2) — touches post approval cards,
      send only on click.
