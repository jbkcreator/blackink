# S-8 (tracking pixel/click/email_replied) + S-11 (reply-send + Book Meeting)

**Status:** task-analysis output. No source file changed.

## Shared blocking prerequisite (found during research, not in either task's original scope)

`src/services/inbound_ingest.py::ingest_inbound_reply()` — the ONLY function
that both S-8 (`email_replied` producer) and S-11 (reply-send reads the
inbound row) must build on — writes and reads column names that **do not
exist** on the real `inbound_messages` table:

| Code uses (`inbound_ingest.py`) | Real column (`apply_inbound_messages.py:35-68`, wins — runs first) |
|---|---|
| `message_id` | `original_message_id` |
| `from_address` | `sender_email` |
| `to_alias` | `destination_address` |
| `raw_body` | `body_text` |

Verified directly (not just cited from research): grepped both the winning
`CREATE TABLE` and the function's actual `INSERT`/`SELECT` text — confirmed
identical to what's described above. `INSERT INTO inbound_messages
(id, message_id, client_id, ...)` would raise `UndefinedColumn` against any
real Postgres today. The function's own comment (`inbound_ingest.py:79-83`)
admits this: *"the cold-reply persistence below ... targets an older
inbound_messages shape and is independently broken on the current deployed
schema."* This is a real, currently-shipped defect, unrelated to S-8/S-11's
original ask, that happens to sit directly in both tasks' path.

**Resolution:** fix as a small, mechanical **Step 0** shared by both plans —
rename the 4 mismatched identifiers in `inbound_ingest.py` to the real
column names (`original_message_id`/`sender_email`/`destination_address`/
`body_text`), not a schema migration (the table already has the right
shape; the code is wrong, not the DB). No new open question — this is a
factual correctness fix, not a business decision.

---

## S-8 — tracking pixel, click-wrap, `email_opened`/`email_clicked`/`email_replied`

### Scope resolution (real contradiction found, resolved via existing code)

The digest's own denominator (`daily_digest.py:83-87`) is
`outbound_touch_dispatched WHERE payload->>'channel'='email'` — written only
by `log_touch_dispatched()`, called only from the **cold 5-touch sequence**
(`sequence_orchestrator.py`). A second, unrelated email stack
(`email_dispatch.py`'s `EmailProvider`, used for booking confirmations/
show-rate reminders) exists but never feeds this denominator. **Scope is the
cold sequence only** — existing code (the digest's own SQL) settles this,
matching the blueprint's own citation (Touch 1, §3.1.4).

### Design

| Piece | Decision |
|---|---|
| Token module | New `src/services/email_tracking.py`, mirroring `email_unsubscribe.py`'s mint/verify/url triple exactly (PyJWT, HS256, `_TOKEN_TYPE` discriminator so a pixel token can never be replayed as a click token or vice versa). New dedicated secret `EMAIL_TRACKING_SECRET` in `config/settings.py` — CLAUDE.md's own stated reason for `EMAIL_UNSUBSCRIBE_SECRET` being separate from `ADMIN_JWT_SECRET` applies identically here ("unrelated token families must be able to rotate independently"). Click tokens carry the destination URL **inside the signed token itself** (chosen at send time, by us) — the click endpoint never takes an attacker-supplied redirect target, closing the obvious open-redirect risk without needing a DB lookup. |
| Pixel endpoint | New `GET /api/v1/public/pixel/{token}` in a new `src/api/email_tracking_router.py`, modeled on `unsubscribe_router.py`'s no-auth/signed-token-is-the-auth/generic-response shape. Returns a static 1x1 GIF regardless of token validity (an invalid/expired token must not error visibly — a broken pixel is itself a signal to a sender-reputation-scanning recipient). |
| Click endpoint | New `GET /api/v1/public/click/{token}` — verify, log, then `302` to the embedded URL. Invalid/expired token → generic small error page (same posture as unsubscribe). |
| Idempotency | **Both events are deduped per `dispatch_id`** (at most one `email_opened` and one `email_clicked` row per dispatch, via a `SELECT ... WHERE event_type=... AND entity_id=... AND payload->>'dispatch_id'=...` guard before insert). This is a factual correctness call, not a business one: the digest computes `open_rate_pct` as `COUNT(email_opened) / COUNT(dispatched)`, which only means "percent of sent emails opened" if opens are deduped per send — undeduped, a single recipient opening the same email 5 times would silently push the reported rate past 100%. |
| Rate limiting | **None** — matches `unsubscribe_router.py`'s own posture (the closest sibling public endpoint), not `public_landing_router.py`'s (a lead-gen form with a real spam/cost incentive to abuse; a pixel/redirect hit has no such incentive). |
| Email body change | `sequence_content.py`'s touch bodies are plain text today, no HTML at all, no links besides the plain-text unsubscribe footer line. Minimal, scope-disciplined approach: generate a bare HTML variant (line breaks → `<br>`, no redesign — touch-copy/branding is dev item S-5's job, not this one) with the pixel `<img>` appended, and pass `html_body=` to `EmailSender.send()` (currently omitted — `sequence_orchestrator.py` never passes it). |
| `email_replied` producer | Inside `ingest_inbound_reply()` (post Step-0 fix), write `email_replied` **only when Tier-1 attribution matched** (`result.run_id`/`touch_step` present) — that's the only case with a resolvable `dispatch_id` (via `sequence_touch_dispatches WHERE run_id=... AND touch_step=...`). A Tier-2 (sender-email-only) match still posts to `#sales-replies` as today, just doesn't count toward the digest's reply-rate — stated explicitly, not silently dropped. |
| Evidence Packet §2 | `evidence.py:151`'s gap line becomes conditional: query `events` for the transaction's contacts' `email_opened`/`email_clicked`/`email_replied` rows (same `company_id` join already used at lines 127-134); print real fields when present, keep the `DATA GAP` line only when genuinely absent. |
| Tenant isolation | No new table — `events` is already tenant-registered (`tenant_policies.py:41`). Nothing to add to `TENANT_POLICIES`. |

### Requirement → trace → code → test

| # | Requirement | Trace | Code | Test |
|---|---|---|---|---|
| 1 | Touch 1 email carries a working tracking pixel | dispatch_touch → HTML body w/ pixel `<img>` → recipient's mail client fetches it → pixel endpoint → `email_opened` written (deduped) | `sequence_content.py`, `sequence_orchestrator.py`, `email_tracking.py`, `email_tracking_router.py` | Unit: pixel endpoint returns a GIF for a valid token and writes exactly one `email_opened` event; a second hit with the same token writes zero more. Integration: real Postgres, real dispatch row, full round trip. |
| 2 | Click-wrapped links produce `email_clicked` | any link in a touch body → wrapped at send time → click endpoint → 302 + event | `email_tracking.py` (wrap helper), `email_tracking_router.py` | Unit: wrap→redirect→event, deduped same as #1. **Caveat, stated not hidden**: no touch body today contains a real link besides the plain-text (unwrapped, correctly so — see below) unsubscribe footer — S-4/S-5 (PDF attachment, micro-ask copy) are separate, not-yet-built dev items that would be this mechanism's first real caller. This task builds and tests the mechanism itself; it does not fabricate a link to wrap. |
| 3 | `email_replied` written on attributed replies | prospect replies → `ingest_inbound_reply` → Tier-1 match → `email_replied` | `inbound_ingest.py` (post Step-0 fix) | Unit + integration: Tier-1 match writes the event with correct `dispatch_id`; Tier-2 match does not. |
| 4 | Digest's 3 dead metrics come alive | 24h window, real events exist | unchanged `daily_digest.py` | Re-run existing digest tests with seeded events; confirm non-null rates. |
| 5 | Evidence Packet §2 stops always claiming a gap | txn's contacts have tracking events | `evidence.py` | Unit: gap line absent when events exist, present when they don't. |

### Open item (not blocking, explicitly not click-wrapping the unsubscribe link)

The unsubscribe URL is deliberately **not** click-wrapped — RFC 8058 /
Gmail-Yahoo bulk-sender rules govern that link's behavior, and routing it
through a tracking redirect adds a hop with no benefit and a small risk of
breaking one-click compliance if the redirect ever misbehaves. Stated as a
design decision, not an oversight.

---

## S-11 — reply-send path + Book Meeting

### The one genuine open question (Step 6 — real, not filler)

**Does a reply-send need to pass `compliance_gate.py`, or at minimum an
opt-out check?** Traced fully: the only existing reply-shaped send in this
repo (`speed_to_lead_sweep._send_response()`) calls neither
`evaluate_touch_gate`/`evaluate_enrollment_gate` **nor** even a bare
opt-out check — and does so with no comment explaining why. `compliance_gate.py`'s
own gated field, `compliance_eligibility == 'EMAIL_COLD_ELIGIBLE'`, reads as
cold-outbound-specific, which would argue a reply is legitimately exempt
from the *full* gate (DNC/non-poach/cooldown don't obviously apply to
answering someone who just emailed us) — but the same Slack card sitting
right next to "Reply in Thread" has its own "Mark Opt-Out" button, meaning
opt-out is a live, acknowledged concern for these exact contacts. Sending a
reply to someone who has since opted out (e.g., opted out via a prior
touch, or the win-back suppression list) without checking would be a real
compliance gap.

**No source resolves this — genuinely ambiguous, escalating per Step 6.**
Recommended default (stated, not silently assumed): check
`contacts.is_opted_out`/global suppression before sending a reply (cheap,
uncontroversial), but skip the full cold-outbound gate (DNC/non-poach/
cooldown) — replying to an active conversation isn't a new cold touch. Will
proceed on this default unless corrected, and flag it plainly in the PR.

### Design

| Piece | Decision |
|---|---|
| Modal data plumbing | `sales_reply_content_blocks()` already puts `{inbound_id, contact_id, client_id}` in the button's `value`, but `handle_reply_in_thread()` (the modal-opener) currently ignores it and only threads `{channel, ts}` into `private_metadata`. Fix: also parse `action["value"]` and merge into `private_metadata`, mirroring the LinkedIn-note modal's already-correct pattern of passing its `value` through. |
| Sending | Reuse existing primitives as-is — no new sender abstraction. `get_active_mailbox_for_client()` (mailbox_dispatcher.py) picks the sending mailbox; `EmailSender.send(from_address=mailbox.mailbox_address, to_address=<inbound sender_email>, ..., in_reply_to=<inbound original_message_id>, list_unsubscribe_url=...)`. One-click-unsubscribe **is** wired in (CLAUDE.md states it's mandatory for every outbound email, no exceptions listed for replies; `speed_to_lead_sweep`'s omission is treated as a pre-existing gap in that other function, not a precedent to repeat here — out of scope to fix there, but not repeated here). |
| Mailbox capacity accounting | `mailbox_dispatcher.py`'s rolling-24h cap subquery already folds in `sequence_touch_dispatches`, `inbound_messages.status='RESPONDED'`, `stl_cadence_dispatches` — a reply-send needs to count too, or an operator's mailbox could silently exceed its real send volume. Simplest correct fix: after a successful send, `UPDATE inbound_messages SET status='RESPONDED', responded_at=NOW() WHERE id=...` (the column already exists, per the lead-fields migration) — this reuses the SAME status value the capacity query already recognizes, no new dispatch table needed. |
| Idempotency | Double-click guard: before sending, check `inbound_messages.status` — if already `RESPONDED`, no-op with an ephemeral "already replied" message (same double-tap pattern as `_finalize_terminal_decision`'s `record_decision` guard elsewhere in this file). |
| Audit | `log_event(client_id, "outbound_touch_dispatched", entity_type="contact", entity_id=..., payload={"channel":"email","touch_step": None or a reply-specific marker, ...})` — **decision needed at build time, not a client question**: should a manual reply count toward the digest's `cold_emails_dispatched`/rate denominators? No — those are cold-sequence-specific per S-8's own scope resolution above. Log it as its own distinct event (e.g. `sales_reply_sent`) via the existing `log_event()` path, not `outbound_touch_dispatched`, to avoid polluting the cold-sequence digest metrics with manual replies. New `REQUIRED_PAYLOAD_FIELDS` entry in `events.py`. No new table. |
| Book Meeting button | Add the button to `sales_reply_content_blocks()` (currently absent). Handler: `resolve_booking_link()` (internal-sales-demo variant — this is Blackink's own sales pipeline, not a client's owner-booking flow) → compose a short message containing the link → send via the same `EmailSender`/mailbox path as the reply, following `speed_to_lead_sweep._send_response()`'s inline compose-then-send shape (not itself reusable as a function; a new small compose helper needed here). If `resolve_booking_link()` returns `None` (no default sales-booking connection flagged — a pre-existing, separate gap this repo already documents), the button must fail visibly (ephemeral Slack error), never silently no-op. |
| Tenant isolation | No new table. `inbound_messages` already registered (`tenant_policies.py:98`). The new `sales_reply_sent` event rides on `events`, already registered. |

### Requirement → trace → code → test

| # | Requirement | Trace | Code | Test |
|---|---|---|---|---|
| 1 | Reply-in-Thread actually emails the prospect | rep clicks → modal (now carrying real context) → submit → mailbox lookup → send → thread post + `inbound_messages.status='RESPONDED'` + `sales_reply_sent` event | `listeners.py`, `mailbox_dispatcher.py`, `email_sender.py`, `events.py` | Unit: modal submit with a fake sender asserts `.send()` called with correct `to_address`/`in_reply_to`; asserts `RESPONDED` status set; asserts event written. Integration: real DB, `StubEmailSender`. |
| 2 | Double-click is a no-op, not a double-send | second submit on an already-`RESPONDED` row | `listeners.py` | Unit: two submits, one send call. |
| 3 | Opt-out/suppressed contacts are not emailed a reply | `is_opted_out`/suppression check before send | `listeners.py` (or a thin call into `compliance_gate.py`'s deterministic-column check only) | Unit: opted-out contact → send blocked, ephemeral message shown. **Depends on the open question above being answered — flagged, not assumed silently.** |
| 4 | Book Meeting sends a real link or fails visibly | click → resolve link → compose → send, or ephemeral error if unresolved | `listeners.py`, `booking_link.py` | Unit: both branches (link resolved / `None`). |
| 5 | Manual replies don't pollute cold-sequence digest metrics | distinct `sales_reply_sent` event type, not `outbound_touch_dispatched` | `events.py` | Structural: grep/assert the reply-send path never calls `log_touch_dispatched`. |

---

## Sequencing recommendation

Step 0 (schema-mismatch fix) → S-8 (self-contained, no open questions) →
S-11 (needs the opt-out question answered before the send-blocking logic is
final, though the rest can build in parallel).
