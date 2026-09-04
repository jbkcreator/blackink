# Task 3.1 — Multi-Touch Outbound Sequencer & Interim Reply Bridge

Labels: `wayfinder:map`

Tracker: local markdown. Tickets are `issues/NN-<slug>.md` in this directory.
A ticket is **open** unless its `Status:` line says `closed`. A ticket is
**claimed** when its `Assignee:` line is non-empty. A ticket is **unblocked**
when every ticket named on its `Blocked by:` line is closed. The **frontier**
is the open, unblocked, unclaimed tickets.

## Destination

Every blocking decision for Task 3.1 resolved and recorded, so all three
subtasks (3.1.1 email sequencer, 3.1.2 phone/LinkedIn bridge, 3.1.3 interim
reply bridge) can be implemented against a settled contract — no unresolved
architecture, no unratified event names, no missing upstream dependency.

Reaching the destination does **not** mean the sequencer is built. It means
nothing is left to decide before someone builds it.

## Notes

- **Domain:** Blackink, a multi-tenant B2B lead-gen platform for property
  management firms. Dev 3 owns outbound sequencing and booking.
- **⚠⚠ Sources replaced again, 2026-09-03 (later).** `docs/` was reorganised;
  read `docs/README.md` first. The authoritative pair is now
  **`docs/Week1_Tasks_Dev_Split_v2.md`** (the sprint plan) and
  **`docs/Implementation_Blueprint_v2.md`** — the Source of Truth *applied*.
  The old Dev 3 reference moved to `docs/archive/`; unqualified `§` references
  in these tickets still mean that archived file, which remains accurate for
  sequencer mechanics but wrong on Touch content.

  **v2 resolves five things this map had open or wrong:**

  | | v2 says |
  |---|---|
  | Owner Visibility Score owner | **Dev 2**, Task 2.1 — not unowned. This was the flagged September blocker; it isn't one |
  | Touch 1 report format (ticket 11) | **PDF attachment**, not a hosted page |
  | Touch 2 timing (ticket 14) | 60 seconds from the **Approve click** — the Day 1–2 vs 60s conflict dissolves |
  | Skip event name (ticket 03) | **`touch_skipped_compliance`**, not the proposed `touch_skipped` |
  | Card expiry (ticket 15) | **Required** — shared DoD item 5 says "verify expired-card rejection". The `slack_message_ts` approach still applies |

  Also: Touch 5's scarcity angle **survives** as "County Visibility Rank &
  Scarcity" (ticket 19 assumed it died with the metro layer); approval cards
  go to **`#blackink-setter`**; calling hours are **8 AM–9 PM recipient
  local**; autonomy is earned after **50 consecutively approved clean sends**.

- **⚠ Source of truth changed 2026-09-03 (earlier).**
  `docs/archive/Blackink_Source_of_Truth.md` now controls, and it **supersedes large
  parts** of the Dev 3 task reference. Read
  `docs/archive/SOURCE_OF_TRUTH_RECONCILIATION.md` first — it lists what is cancelled,
  what is confirmed, and what changed. Headlines: the **ghost shopper is
  prohibited**, **video/Sendspark is held**, **SMS and Twilio are cancelled
  for the year**, **the metro layer is abolished (county is the unit)**, and
  **every early-client send needs human approval**.
  Unqualified § references in these tickets still point at
  `docs/archive/Blackink_Dev3_Task_3.1_Complete_Reference.md`, which remains useful
  for the sequencer mechanics it describes — but where the two disagree, the
  Source of Truth wins.
- **Repo invariants:** read `CLAUDE.md` first, every session. Tenant
  isolation via `TENANT_POLICIES` + RLS is non-negotiable; any new
  tenant-bearing table must be registered there and pushed through
  `migrations/apply_rls_policies.py`. `sqlalchemy.text()` with named binds,
  never the ORM query API. Idempotent `migrations/apply_<name>.py`, no
  Alembic. Settings via `get_settings()`, never `os.environ`.
- **Skills to consult:** `/grilling` and `/domain-modeling` by default;
  `/research` for the AFK research tickets; `/prototype` where the question
  is "what shape should this be".
- **Plan, don't do.** Tickets produce decisions, not implementations. The
  exception is `wayfinder:task` tickets, which do manual work that unblocks
  a decision.
- **Work the 3.1.1 end first** (user directive). 3.1.2 and 3.1.3 are in
  scope but mostly still fog until the event and sender contracts settle.
- **[CLIENT-ASKS.md](CLIENT-ASKS.md) is a living companion doc** — every
  dependency Dev 3 cannot resolve alone: client decisions, accounts and
  budget, content and assets, cross-dev blockers. **Every grilling session
  must update it**: add asks the grilling surfaces, and move anything the
  user answers into its Answered section with the date. Read it before
  choosing a ticket — an ask may already block the ticket you were about to
  take.

### Baseline: what `main` @ 8974773 actually has

Established by survey, 2026-09-03. Re-verify before relying on any line.

| Area | State |
|---|---|
| Compliance gate | **Exists.** `evaluate_compliance_gate(...)`, `EMAIL_COLD_ELIGIBLE` genuinely checked |
| Mailbox dispatcher | **Partial.** LRU rotation, tenant-isolated, `FOR UPDATE SKIP LOCKED`. No send cap, no domain join |
| Slack hub | **Exists.** 6 channels, SHA-256 payload binding, action cards, modals |
| Agent framework | **Exists.** `agent_work_orders` (idempotency, autonomy bands), Cora Redis Streams queue |
| EventLogger | **Missing entirely.** Only a private direct INSERT in Slack listeners |
| Email sender | **Missing.** `InstantlyService` is analytics-only; nothing transmits mail |
| Sequence state | **Missing.** No run table, no scheduling, no Message-ID, no cooling stamp |
| Per-contact scheduler | **Missing.** compose uses `while true; sleep N` fleet sweeps |
| Sendspark | **Missing.** Zero code, no account. Vendor confirmed capable — see ticket 10 |
| Audit PDF | **Partial.** Metrics reader + two section builders marked "Week 2"; no renderer |

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [RESEARCH: can Instantly's API deliver per-message threading and tracking?](issues/04-research-instantly-threading-capability.md)
  — **No. Instantly is eliminated as the sender**: cannot set `In-Reply-To`,
  cannot attach files, no general send endpoint. Only its open/click
  tracking webhooks are salvageable. Hands ticket 05 a new deciding axis:
  who mints the `Message-ID`.
- [RESEARCH: what does Sendspark actually give us?](issues/10-research-sendspark-capability.md)
  — Technically fit for purpose (record-once-render-N, hosted GIF
  `thumbnailUrl`). **Commercially moot** — the Source of Truth holds video
  for September and says no Sendspark account was supplied. Findings retained
  for if the hold lifts.
- [Resolve the `market_metro` / `{city}` gap](issues/13-market-metro-gap.md)
  — **Do not add the column.** County is the unit everywhere; there is no
  metro layer. Repoint `{city}` to a county display name. Decided by the
  Source of Truth §1.1/§2.1, not by grilling.
- [Reconcile the per-touch compliance re-check with the 14-day cooldown](issues/01-compliance-recheck-vs-14-day-cooldown.md)
  — **Split the gate into two stages.** An **enrollment gate** (full checks +
  14-day fatigue cooldown + active-sequence lock) and a **per-touch gate**
  (everything except the cooldown), as two named entry points rather than a
  boolean flag. The cooldown is a deliberate *platform-wide* contact-fatigue
  guard, not a bug — so it moves to enrollment rather than being weakened:
  14 days gates entry, 30 days gates re-entry, and a **cadence floor** stops a
  configurable cadence being configured past the policy. On DNC: a known hit
  blocks email, a **missing phone does not** (nothing for the registry to say),
  and clearance moves from the monthly Tracerfy batch to **promotion time** —
  a monthly-only sweep made "Touch 1, Day 0" impossible.

- [Who builds the shared EventLogger, and what is in its first version?](issues/02-eventlogger-ownership-and-scope.md)
  — **Dev 4 already built it.** `src/services/events.py` on
  `origin/feature/week1-metrics-and-sandbox`, with `log_event()`,
  `MalformedEventError`, the 1000-event buffer, and
  `outbound_touch_dispatched` already registered with the right six keys.
  Dev 3 integrates, passing `session=` so the event is atomic with the
  dispatch. Four gaps: the buffer is process-local and lost on restart,
  `flush_pending()` must be called by the sequencer's own loop, buffered
  events lose their occurrence time (so **3.1 carries its own timestamp in
  the payload**), and the `#blackink-qa` data-loss alert is unimplemented.
  **Knock-on: ticket 08's send cap cannot count events** — losses and
  re-timestamping both push permissive.
- **Tenancy split for the cross-client fatigue guard** (grilling, 2026-09-03).
  The sequencer runs as the **client-scoped** role; a **narrow privileged
  helper** performs the cross-client `last_outbound_touch_at` and
  active-sequence lookups and returns only a verdict, never rows. Open
  refinements in ticket 07: the verdict must be **sanitized** before it
  reaches `compliance_gate_checks` (that table is tenant-bearing, and the
  current detail string would leak another client's touch timing), and the
  active-sequence lock must be a **partial unique index** rather than a
  check, since a verdict from a separate transaction is inherently racy —
  same pattern `county_allocations` already uses for one-active-row-per-county.

- [Where does the sequencer live, and how do per-contact due times fire?](issues/06-sequencer-architectural-seat-and-scheduling.md)
  — **`agent_work_orders`.** The seam was reserved by name: `action_class`
  defaults to `DISPATCH_EMAIL_TOUCH` and the `noop` dispatcher's docstring
  holds the slot for "the Week 1 Campaign Agent". Task 3.1 inherits hash-bound
  approval cards, idempotency, atomic claim, autonomy bands and correct
  tenancy. Scheduling is **day-grain**, so no real scheduler — add a
  **`due_batch()`** over the existing `(status, due_at)` index rather than
  reusing SNOOZED, and gate card posting on `due_at`. **Operating it is ours:
  add compose services** for the sweep and the Cora worker, since nothing runs
  them today. The sequencer must supply its own natural idempotency key —
  the default is hour-bucketed.

- [Design the sequence state model](issues/07-sequence-state-model.md)
  — **A separate `sequence_runs` table**, one row per enrollment, with a
  **partial unique index `UNIQUE (contact_id) WHERE status='ACTIVE'`** —
  deliberately cross-client, since indexes ignore RLS and that is the only way
  to get the global guarantee ticket 01's fatigue lock needs. Endorsed by §5.2
  ("an ordinary non-unique index is not an exactly-once constraint") and §2.7
  ("a lease does not itself prove a send is unique"). **All five touches
  enqueued upfront** with `due_at` and explicit cancellation, because
  sequential enqueue fails invisibly — matching §2.4's "every completion event
  cancels the relevant chase" and §1.13's transition-never-delete rule.
  `Message-ID` gets its own column (Touch 3's threading is a DoD line).
  30-day cooling lives on the run, 14-day fatigue stays on the contact —
  one column, one meaning.

- [Ratify the full event taxonomy for Task 3.1](issues/03-event-taxonomy-for-task-3.1.md)
  — **`outbound_touch_dispatched` wins**: the blueprint's `touch_sent` is an
  illustrative *comment* on a DDL column, while its worked example of a real
  dispatch uses `outbound_touch_dispatched`. Entity is **contact**, with
  `company_id` in the payload. `actor='cold_outbound_sequencer'` (mapping the
  blueprint's dropped `source` field), and **`occurred_at` in the payload** —
  restoring a field the blueprint specified and the real schema lost, which
  independently justifies ticket 02's timestamp workaround. One
  **`touch_skipped`** event with a `skip_reason` enum, not a type per reason.
  Corrections use a **superseding-event pattern**, keeping append-only
  literally true per §1.13/O-17. `ghost_shopper_speed_sec` and
  `sendspark_video_id` are dropped as dead.

- [BLOCKER: stale-executing reclaim can double-send a cold email](issues/22-execution-lease-double-send.md)
  — **At-most-once**, guaranteed by `UNIQUE (sequence_run_id, touch_step)`
  rather than a lease (§2.7: "a lease… does not itself prove a send is
  unique"). Insert `SENDING` → send → update `SENT` with the `Message-ID`;
  a unique violation aborts the send. Rows stuck in `SENDING` **alert to
  `#blackink-qa`** rather than auto-retrying. **Do not shorten the 30-minute
  reclaim window** — a shorter one reclaims live dispatches more often and
  makes duplicates *more* likely. Also captures three bugs, two of which
  turned out to be **ours, not Dev 2's**: the unchecked
  `record_execution_result` return at `work_orders/__main__.py:193`, and the
  never-wired `notify_approval_resolved()` that leaves Cora permanently
  auto-paused at 50 drafts.

- [Make `sending_domain` reachable from mailbox assignment](issues/09-sending-domain-reachability.md)
  — **Two live bugs, not a design question.** The sentinel quarantines
  `sending_domains` but the dispatcher only checks `mailboxes`, so a
  quarantined domain **keeps sending** — violating blueprint p16's "the
  Sentinel trips an automatic quarantine: **pauses outbound dispatch**". And
  its reserve-promotion query requires a same-cluster reserve, while the three
  supplied reserves belong to no cluster, so **nothing is promoted**. One join
  on `mailboxes.domain_id` fixes the quarantine check *and* yields the
  `sending_domain` payload key.
- [Choose the counting substrate for the 24h send cap](issues/08-24h-send-cap-counting-substrate.md)
  — **Dedicated table, rolling 24h, enforced inside
  `get_active_mailbox_for_client`, defer rather than DLQ when capped.** Redis
  rejected because a flush silently resets the cap *and the DoD test would
  still pass*. Rolling is the only reading satisfying both "daily ceiling"
  (blueprint p16) and "50 in a 24-hour window" (DoD). A capped mailbox is not
  a failure, so §2.8's DLQ rule does not apply — but split
  `NoMailboxAvailable` so "all capped" is distinguishable from "none
  provisioned", and bound the deferral.
- [Define `template_version`](issues/12-define-template-version.md)
  — Copy is **LLM-generated**, so `template_version` is the variant `name`
  (`t1_v1_speed_evidence`), sourced from one function. **Both Touch 1 variants
  are dead content** — built on `{audit_speed}`, `{video_url}` and the
  Sendspark video; rewrite belongs to ticket 19. Gap it cannot close: the
  **Revise** action lets a human edit a draft, so the variant names the
  generator, not what shipped — carry `was_revised` and capture the revision
  reason, which §2.7 already requires ("counterfactual/revision reasons",
  "experiment registry").
- [Add 24-hour card expiry to the Slack payload-hash layer](issues/15-slack-card-24h-expiry.md)
  — **The ground truth does not require expiry at all.** §2.2 and blueprint
  p10 define "stale" as *content drift*, not age, so `payload_hash.py` already
  satisfies the client requirement. Where the task DoD still wants expiry, use
  **`slack_message_ts`** (already stored, set at post time) — **no
  `HASH_VERSION` bump**, no preimage change, no invalidating other devs' cards.
  `Mark Opt-Out` must not expire.

- [Choose the email sender](issues/05-choose-the-email-sender.md)
  — **Direct SMTP per warmed Google Workspace / Outlook mailbox.** v2's shared
  DoD names the mailboxes as real seats, so SMTP is available: `Message-ID`
  and `In-Reply-To` controllable, attachments work (v2 requires two), replies
  land in monitored inboxes for 3.1.3, and our dispatcher keeps mailbox
  selection. Instantly stays only as warmup telemetry. **We build open/click
  tracking ourselves** — a redirect service and pixel endpoint on neither the
  brand domain nor a cold-outreach domain.
- [What do Touches 1 and 5 actually say now?](issues/19-owner-visibility-score-touch-content.md)
  — **Dev 2 owns the Owner Visibility Score** (Task 2.1) and the Fee-Stack
  one-pager (2.2). Dev 3 consumes, builds neither. Touch 1 = score **PDF
  attached** + "Reply YES to see where you rank in [County]"; Touch 3 =
  Fee-Stack one-pager; Touch 5 = **County Visibility Rank & Scarcity** —
  scarcity survives, rebased on rank rather than seats. Open: what Touch 1
  says to a firm outside the county top 25 (D16), and the fetch interface with
  Dev 2.
- [Touch 1 content dependencies](issues/11-touch-1-content-dependencies.md)
  — Report is a **PDF attachment**, built by Dev 2; no renderer for Dev 3.
  **`assert_audit_complete` comes out of the dispatch path** — §2.1 says
  *remove* the readiness prerequisite, not rebuild it on the score. The score
  is content, not a gate. `email_status` remains the real blocker (A13).
- [Resolve the Touch 2 timing contradiction](issues/14-touch-2-timing-contradiction.md)
  — **Dissolved.** v2: the card appears within 60 seconds of the **Approve
  click**; Day 1–2 is when the *call* happens. No sub-minute scheduler needed,
  so ticket 06's day-grain sweep stands. Surfaced a live contradiction inside
  v2 itself — the 8 AM–9 PM window vs a DoD asserting red at 8 PM (D15).
- [RESEARCH: what copy-to-clipboard affordance does Slack Block Kit offer?](issues/29-research-slack-clipboard-affordance.md)
  — **None natively.** No button can write the user's clipboard — a click is a
  backend round-trip. Slack's *client* renders a hover copy-icon on **code
  blocks** (desktop only); a **`url`-type button** opens a link with no
  round-trip; a **prefilled modal** gives selectable text cross-client. Touch 4
  should use a `url` button for the LinkedIn link + a code block for the note +
  a modal fallback for mobile. **Unblocks ticket 26.**
- [Touch 2 dial-task card — trigger, contents, calling-hours freshness](issues/25-touch-2-dial-task-card.md)
  — **Event-driven post** off Touch 1's Approve (60s SLA rules out the sweep),
  fired from `_finalize_terminal_decision`; the DIAL_TASK `due_at` is only a
  "call after" hint. **Informational card + one "Mark Called" → DONE** (no
  approve gate — nothing sends). Drop the two dead fields; render Dev-2
  score/rank as "pending" and still post. Calling-hours indicator **computed at
  post time, stamped, conservative default** (8 PM = red until D15 confirmed).
  **Hardcode ET** for September. New ask C9 (capture call outcome?) deferred.
- [Touch 4 LinkedIn card — URL, note, clipboard](issues/26-touch-4-linkedin-card.md)
  — `url` button for the search link + note in a code block (desktop
  hover-copy) + a **modal fallback** (prefilled `plain_text_input`) for mobile;
  no fake clipboard button. **Stored `linkedin_url` else people-search URL.**
  Score-free **placeholder note** for September (C7 copy still owed; 300-char
  cap). **Day-grain sweep** trigger (no 60s SLA); reuse `evaluate_touch_gate`,
  skip+log `touch_skipped_compliance` on opt-out.
- [Mark Opt-Out halt mechanics](issues/27-mark-opt-out-halt-mechanics.md)
  — One atomic `halt_sequence_for_contact(contact_id)`: add a **`HALTED`**
  terminal state to `sequence_runs` (**no cooling** — permanence comes from
  `is_opted_out`, which `may_enroll` must also honor), **cancel** the pending
  QUEUED/SNOOZED touches (belt) while the per-touch gate stays the suspenders,
  **opt-out is global** (flag on contact, gate refuses all future clients).
  Button must **not expire** (ticket 15). **Buildable now** — the only 3.1.3
  piece not blocked on ticket 20.

## Charting note — 3.1.2 / 3.1.3 fog graduated + resolved (2026-09-03)

3.1.1's contracts settled (sender = direct SMTP, event taxonomy,
`sequence_runs` state model, per-touch gate), so the fog these two subtasks
waited on graduated into tickets 25–29. Tickets 25/26/27/29 were then
**resolved in one pass by user directive "go with your leans"** — the human
accepted the recommended answer on each rather than grilling one at a time.
Only ticket 28 remains open, blocked on the client's alias scheme (ticket 20).

**Implementation-ready after this pass:** Touch 2 (25), Touch 4 (26), the
opt-out halt *service* (27). Reply *card* (28) and all 3.1.3 ingestion wait on
ticket 20 (client input).

**Update (2026-09-03) — 3.1.3 partly unblocked by the client "Part 14"
comment.** The client supplied the inbound forwarding/return-path model,
resolving four of ticket 20's six sub-questions (receiving domain, onboarding
verification, BCC-loop dedup, no-backfill thread history) and confirming
ticket 28's storage assumptions. Ticket 20 stays open on **only** the alias
string scheme (naming/collision/lifecycle) and attribution (recovering
firm+contact from a forwarded envelope). So 3.1.3 moves from *fully blocked* to
*blocked on the alias naming convention + attribution*. Note: the client's
"Lead agent" reply templates are the **Week-2 Reply Triage Agent** spec — the
automated successor to 3.1.3's interim human bridge, not Week-1 Dev 3 scope.
The "Reply-To + BCC the client" rule is an **outbound-sender** requirement
already implemented in `SmtpEmailSender` (`email_reply_to` / `email_bcc`,
§768).

## Not yet specified

In scope, but not yet sharp enough to ticket. Graduates as the frontier advances.

- **Open/click tracking wiring** — the handoff (§27.1) fixes the event names
  `email_opened` / `email_clicked` exactly, because the daily digest queries
  those strings. But whether tracking is provider-hosted or self-hosted, and
  what the webhook receiver looks like, hangs on the sender decision.
  Ticket 04 sharpened the stakes: dropping Instantly may mean building the
  redirect service and pixel endpoint ourselves.
- **A single inbound webhook surface** — three separate vendors now want to
  POST to us: the email provider (open/click), Sendspark (watch events), and
  Twilio (inbound SMS). Whether these share one authenticated receiver with
  per-vendor signature verification, or each gets its own route, is worth
  deciding once rather than three times. Not sharp until the sender is
  chosen.
- **Touch 1 payload assembly end-to-end** — the order in which PDF, GIF,
  Sendspark URL and merge tags get composed depends on both the sender and
  the Sendspark answers.
- ~~3.1.2 Touch 2 card contents~~ — **graduated to ticket
  [25](issues/25-touch-2-dial-task-card.md).** The two dead fields, the
  timezone source, the calling-hours freshness and O-07 all live there now.
- **Ten reply classes for 3.1.3** — §2.3 names `HOT_LEAD`, `QUESTION`,
  `OBJECTION`, `LATER`, `NURTURE`, `UNSUBSCRIBE`, `COMPLAINT`, `LEGAL_GRIEF`,
  `WHALE_OWNER`, `PARTNER`. The Dev 3 reference's 3.1.3 has no classification
  at all, only three buttons. Opt-out and legal/complaint handling stay
  deterministic. Not sharp until ticket 20 settles ingestion.
- **Immutable events with versioned corrections** — §1.13 and decision
  register **O-17** require raw events preserved immutably *and* correctable
  via versioning. Folded into ticket 03 as a design constraint, but the
  retention/deletion policy half is a missing input.
- ~~3.1.2 Touch 4 clipboard mechanics~~ — **research done (ticket 29),
  graduated to prototype ticket [26](issues/26-touch-4-linkedin-card.md).**
  No native clipboard button; use `url` button + code block + modal fallback.
- ~~3.1.3 inbound ingestion~~ — **graduated to ticket
  [20](issues/20-inbound-alias-scheme.md).** IMAP/mailbox OAuth is forbidden
  (§1.6); ingestion is client forwarding to a Blackink alias, and the alias
  scheme is a named missing input (O-05/O-06).
- ~~3.1.3 reply card + thread history~~ — **graduated to ticket
  [28](issues/28-reply-thread-storage-model.md)** (blocked by 20). New
  `inbound_messages` table, "last 3 / no backfill" degrade, BCC-loop dedup.
- ~~Opt-out halt wiring~~ — **graduated to ticket
  [27](issues/27-mark-opt-out-halt-mechanics.md).** The sequence state model
  (ticket 07) settled, so the halt is now specifiable; frontier, not blocked.
- ~~Interim bridge date window~~ — **resolved.** The Source of Truth §2.8
  dates the milestones: marketing demonstration **Sep 11**, assisted pilot
  **Sep 16–18**, portal/preflight **Sep 25**, full chain **Sep 30 2026**. The
  bridge window is genuinely next week and brackets the demo and pilot.
  3.1.3 is urgent, not stale.
- **Skip-event logging** — §24 lists "skip-related event logging where
  required", but no skip event name is fixed anywhere.

## Out of scope

- **All SMS, including the conditional engaged nudge** (§22) — **cancelled
  for the year** by the Source of Truth §1.5 ("No SMS this year"), which also
  replaces Twilio with Telnyx and forbids using a click, video view, or
  positive reply to re-enable the flow (§2.2). This also removes 3.1.3's
  Twilio inbound webhook: **the reply bridge is email-only.**
- **Sendspark / personalized video** — held for September (§1.5). Only a
  video trigger hook and a disabled provider row are in scope, folded into
  ticket [19](issues/19-owner-visibility-score-touch-content.md). Tickets
  [17](issues/17-provision-sendspark-workspace.md) and
  [18](issues/18-verify-sendspark-watch-webhooks.md) closed out of scope —
  **do not purchase a plan.**
- **The ghost shopper** — "do not build the ghost shopper or submit pretext
  inquiries" (§1.8). Not merely out of scope: prohibited. Touch 1's proof
  becomes the Owner Visibility Score. Building the score itself is a
  data-acquisition workstream, almost certainly not Dev 3's — see ticket 19.
- **Task 3.2 booking / show-rate work** (§27.4, §27.5) — the
  `open_meeting_outcome_modal` handoff and the no-show pause are explicitly
  Task 3.2. Noted here only because 3.1.3's reply bridge is adjacent. The
  one exception folded into this map is the *no-Calendly* amendment, since
  it changes a 3.1.3 button.

  **Dev 4 handoff specifics, recorded 2026-09-03 so 3.2 need not rediscover
  them** (verified against `feature/week1-metrics-and-sandbox` @
  `ec7e71d`):

  - `open_meeting_outcome_modal` **does exist** on that branch — an earlier
    survey of `main` reported it missing, which was wrong for the branch.
    Call it with `trigger_id`, `contact_id` (string), `meeting_occurred_at`
    (ISO 8601). **Do not pass `client_id`** — the handler resolves tenancy
    itself. Both args are validated before the modal opens; submission is
    idempotent (resubmit updates, never duplicates) and writes
    `contacts.prospect_objections` for the future Owner Score engine.
  - **No-show pause is Subtask 3.2.3 and is not wired.** Dev 4's handler only
    posts a notice to `#blackink-setter`. Search
    `src/services/slack/listeners.py` for the comment "outbound sequence pause
    is Dev 3's no-show handler (Subtask 3.2.3), not yet wired here" and add
    the real call. This depends on ticket 07's sequence state model — pausing
    is a state transition on `sequence_runs`.
  - ⚠ **Raised back to Dev 4 — tenant resolution is time-varying.** The
    handler resolves tenancy via a contacts → companies lookup reading
    `owning_client_id`. That column is "a materialized reflection of the
    county's current allocation, refreshed by
    `src/tasks/county_allocation_reassessment.py`" (`CLAUDE.md`), and
    allocations reassess on a **30-day window**. A meeting held under client A
    and logged after a reallocation resolves to **client B, who never had the
    meeting** — and appointment outcomes feed billing and dispute evidence.
    Tenancy should be resolved from the booking record, which captured
    `client_id` at creation, not from current allocation.
