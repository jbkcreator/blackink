# Ratify the full event taxonomy for Task 3.1

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: — (02 closed)

## Resolution (2026-09-03) — ground-truth checked

### 1. `outbound_touch_dispatched` wins. Settled.

The ground truth contradicts *itself*, and the shape of the contradiction
decides it:

- Blueprint p7, `001_core_spine.sql`: `'touch_sent'` appears in an
  **illustrative comment** on the `event_type` column, alongside
  `reply_received`, `audit_generated`, `meeting_booked`.
- Blueprint p11: the **worked example of an actual dispatched touch** uses
  `"event_type": "outbound_touch_dispatched"`.

A concrete payload example outranks a comment listing sample values. Dev 4's
`REQUIRED_PAYLOAD_FIELDS` and the Week 1 split independently agree. Three to
one. **Keep the discrepancy documented** so nobody "corrects" it back.

### 2. The blueprint's events DDL is historical, not the schema

| Blueprint p7 | Real (`apply_events.py`) | Mapping |
|---|---|---|
| `event_id UUID` | `id BIGSERIAL` | — |
| `source` NOT NULL | `actor` | **`actor = 'cold_outbound_sequencer'`** |
| `occurred_at` | `created_at` (insert time) | **carry occurrence time in payload** |
| `owner_id` / `property_id` / `campaign_id` | `entity_type` / `entity_id` | one generic pair |
| `value_cents` | — | not needed by 3.1 |

Two consequences worth stating plainly:

- The example's `"source": "cold_outbound_sequencer"` maps cleanly onto
  `actor`, which `log_event` already accepts.
- Ticket 02's "carry our own timestamp" is **not a workaround we invented** —
  the blueprint specified `occurred_at` and the implementation dropped it.
  We are restoring a specified field. Use payload key **`occurred_at`** to
  match the original name.

Note the blueprint DDL also carries `market_metro NOT NULL` and
`dnc_clean BOOLEAN DEFAULT FALSE`, both of which the real schema correctly
departs from (§2.1 rules the metro text historical; NULL-able `dnc_clean` is
the tri-state the gate needs). Further evidence this DDL is not authority.

### 3. Entity convention: **contact**

`entity_type='contact'`, `entity_id=str(contact_id)`. The blueprint's example
carries *both* `company_id` and `contact_id` at top level; the real schema has
exactly one entity pair, so **`company_id` goes in the payload**. This matches
what the audit reader and Slack listeners already assume, and the six required
keys are all contact-centric.

### 4. The taxonomy

**Only `outbound_touch_dispatched`'s six keys are client-mandated** (§27.2).
Everything marked *proposed* below is our design and is negotiable.

| Event | Required payload | Entity | Actor | Subtask |
|---|---|---|---|---|
| `outbound_touch_dispatched` | **mandated:** `touch_step`, `channel`, `recipient_email`, `template_version`, `sending_domain`, `mailbox_id` · *proposed:* `occurred_at`, `sequence_run_id`, `company_id` | contact | `cold_outbound_sequencer` | 3.1.1 |
| `email_opened` | *proposed:* `touch_step`, `occurred_at` | contact | `email_tracking` | 3.1.1 |
| `email_clicked` | *proposed:* `touch_step`, `occurred_at`, `url` | contact | `email_tracking` | 3.1.1 |
| `touch_skipped` | *proposed:* `touch_step`, `skip_reason`, `occurred_at` | contact | `cold_outbound_sequencer` | 3.1.1 / 3.1.2 |
| `linkedin_task_created` | *proposed:* `linkedin_url`, `occurred_at` | contact | `cold_outbound_sequencer` | 3.1.2 |
| `inbound_reply_received` | **required by §18:** raw message body, detected `channel` · *proposed:* `occurred_at` | contact | `reply_bridge` | 3.1.3 |
| `opt_out_recorded` | *proposed:* `source_channel`, `occurred_at` | contact | Slack user id | 3.1.3 |

**`channel` must be exactly lowercase `"email"`** (§27.2) — downstream SQL
compares the JSON value case-sensitively. Enforce at write time, not by
convention.

**Dead fields:** `ghost_shopper_speed_sec` and `sendspark_video_id` from the
blueprint example are **not carried** — the ghost shopper is prohibited (§1.8)
and video is held (§1.5). If Touch 1 needs a proof reference it will be an
Owner Visibility Score reference; add it here once ticket 19 settles.

**`email_opened` / `email_clicked` names are exact and load-bearing** (§27.1):
the daily digest queries those literal strings, so a wrong name yields a
silent 0% rather than an error.

### 5. Skip events: `touch_skipped_compliance`

**Name corrected 2026-09-03** — `Week1_Tasks_Dev_Split_v2.md` fixes it in
Subtask 3.1.2's DoD: "opted-out contact between Touch 3 and Touch 4 skips
Touch 4 and logs a **`touch_skipped_compliance`** event". Use that verbatim,
not the `touch_skipped` originally proposed here.

Note the name scopes it to *compliance* skips. A skip for a non-compliance
reason — no mailbox available under the cap (ticket 08), for instance — is
arguably a different event. Either add `touch_skipped_capacity` or widen the
reason enum under the existing name; **v2 only names the compliance case**, so
this needs a decision rather than an assumption.

Keep the payload design: a `skip_reason` enum rather than a distinct event
type per reason. The digest can group by reason, and a proliferation of event types
makes `REQUIRED_PAYLOAD_FIELDS` unwieldy. Reasons at minimum: `OPTED_OUT`,
`SUPPRESSED`, `INELIGIBLE`, `REPLY_RECEIVED`, `DNC_LISTED`, `HALTED`,
`NO_MAILBOX_AVAILABLE`.

### 6. Corrections: superseding-event pattern

§1.13 and **O-17** require raw events immutable *and* correctable. Choose the
**superseding-event pattern** — emit a correction event carrying a reference
to the corrected event's id — over a `correction_version` column.

Rationale: it keeps append-only literally true rather than nearly true, needs
no migration to Dev 4's table, and matches §1.13's framing ("preserve raw
events immutably **and apply** versioned corrections") better than mutating a
version counter in place. Cost: readers must resolve supersessions, so any
query over events needs a canonical "latest view" helper — write that once,
with Dev 4, rather than letting each consumer reimplement it.

### 6b. Confirmed by Dev 4's handoff (2026-09-03, commit `ec7e71d`)

Dev 4 independently verified the above against their shipped code and added
three specifics:

- **The consumer is `src/tasks/daily_digest.py`.** Open rate and click rate key
  off `email_opened` / `email_clicked` **verbatim**. Wrong names → both metrics
  sit at 0% forever with no error.
- **The channel predicate is `payload->>'channel' = 'email'`** — exact and
  case-sensitive. Wrong casing does **not** raise; it silently undercounts.
  This is the easier of the two to miss, since the six-key check *does* raise
  `MalformedEventError` at the call site.
- **Dev 4 has offered to adapt the digest query** if Dev 3 uses different
  names. So the names are a negotiable convention, not a hard constraint —
  but the default is to match theirs, and any divergence must be told to them
  explicitly rather than discovered.

### 7. Coordination

`REQUIRED_PAYLOAD_FIELDS` lives in Dev 4's `src/services/events.py`. Every
*proposed* row above is an entry Dev 3 needs added there. Raise as one batch
rather than piecemeal, together with ticket 02's `#blackink-qa` data-loss
alert gap.

## Question

Fix, exactly and in writing, every event Task 3.1 emits. Four sub-questions,
all of which have a visible conflict or gap in the source.

**1. The name clash.** The Week 1 task split and the shared logger contract
require `outbound_touch_dispatched`. The Campaign Agent narrative in the
blueprint says `touch_sent` (§8.2, §26.1). The reference doc deliberately
refuses to reconcile these. Ratify one, and record that the other exists so
nobody "fixes" it back later.

**2. `entity_type` / `entity_id`.** Both columns are NOT NULL in
`migrations/apply_events.py`, but the six-key payload contract (§8) never
mentions them — so the documented contract, followed literally, produces a
failing INSERT. The audit reader and the Slack listeners already assume
`entity_type='contact'` with `entity_id=str(contact_id)`. Ratify that, or
choose otherwise, for each of Task 3.1's event types.

**3. The six required keys and their casing.** §27.2 fixes them as
`touch_step`, `channel`, `recipient_email`, `template_version`,
`sending_domain`, `mailbox_id`, and requires `channel` to be exactly
lowercase `"email"` because downstream SQL compares the JSON payload
case-sensitively. Confirm this is enforced at write time, not by convention.
Decide whether the blueprint's extra fields (`ghost_shopper_speed_sec`,
`sendspark_video_id`) are optional-but-expected or genuinely optional.

**4. The unnamed events.** §24 requires "skip-related event logging" but
never names it. 3.1.2's DoD requires an opt-out between Touch 3 and Touch 4
to "log the skip". Name these. Also confirm `email_opened` / `email_clicked`
(§27.1) carry no required payload beyond identity — the digest queries them
by name for open-rate and click-rate, and a wrong name yields a silent 0%
rather than an error.

Produce a single table: event name → required payload keys → entity_type →
emitting subtask. That table is the answer.

## Update from the Source of Truth (2026-09-03)

- The blueprint's optional payload fields `ghost_shopper_speed_sec` and
  `sendspark_video_id` (§8.1 of the Dev 3 ref) are **dead** — the ghost
  shopper is prohibited (§1.8) and video is held (§1.5). Do not carry them.
  If Touch 1 needs a proof reference, it is an Owner Visibility Score
  reference — define it here once ticket 19 settles what that is.
- **No SMS events this year** (§1.5). `inbound_reply_received` is
  email-only; its `channel` payload key will only ever be `"email"` in 2026.
  Keep the key — the schema outlives the restriction — but do not build an
  SMS branch.
- §2.1 makes this ticket foundational, not incidental: "**Generic events and
  outcomes are built first** and serve audits, messages, disputes, offer
  triggers, and future learning."
- §2.8 reinforces the logger's failure semantics: "Stale or missing required
  data returns `UNKNOWN`/`ABSTAIN`; cached degraded results are labelled,
  never falsely fresh." And "retry transient failures with backoff, at most
  **three attempts** before DLQ/incident" — a concrete retry bound for
  ticket 02's buffering design.
- §1.13 adds a constraint the Dev 3 ref never mentions: "Preserve raw events
  immutably and apply **versioned corrections**; append-only history must not
  make false facts impossible to correct." So the events table needs a
  correction mechanism — a superseding-event pattern or a correction version
  column. Decide it here, because retrofitting it later means rewriting
  history semantics. See also decision register **O-17**.

## Why it matters

These names are cross-developer integration surface. A mismatch does not
throw — it silently reports zero.
