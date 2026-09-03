# Task 3.1 — What we need from the client

Companion to [MAP.md](MAP.md). Everything here is a dependency Dev 3 **cannot
resolve alone** — a decision only the client can make, an account only they
can pay for, or an asset only they can supply.

**Living document.** Grilling sessions will add, sharpen, and answer items
here. When an item is answered, move it to [Answered](#answered) with the
answer and the date — do not delete it, since the reasoning matters later.

Status key: 🔴 blocking now · 🟡 needed soon · ⚪ confirm before build

Last updated: 2026-09-03 — revised against `docs/archive/Blackink_Source_of_Truth.md`.
See `docs/archive/SOURCE_OF_TRUTH_RECONCILIATION.md`. Several asks below were
**answered or cancelled** by that document rather than by the client
directly.

---

## 1. Decisions and clarifications

Questions where the source documents contradict themselves or go silent, and
only the client can rule.

### ⛔ D1 — ~~Does the audit score have to be inside the GIF?~~ MOOT

Cancelled by the Source of Truth: there is no GIF (video held, §1.5) and no
audit score (ghost shopper prohibited, §1.8). Superseded by **D10**.

<details><summary>Original question (kept for reasoning trail)</summary>

§4.1 says the animated GIF previews the prospect's website **and audit
score**. Sendspark supports exactly three in-video variables — First Name,
Company Name, Job Title — and no custom or numeric ones, so the score cannot
be rendered into the video or its GIF.

Ask: is "score in the GIF" a hard requirement, or is score-adjacent-to-GIF
in the email body acceptable? If hard, we must fetch, composite and re-host
the GIF ourselves, which forfeits Sendspark hosting and adds real work.

→ ticket [11](issues/11-touch-1-content-dependencies.md)

</details>

### 🟡 D15 — Calling-hours window contradicts its own test

`Week1_Tasks_Dev_Split_v2.md` 3.1.2 states the indicator is "green if
currently in valid calling hours, **red if outside 8 AM–9 PM** recipient local
time" — then its own DoD asserts "indicator shows **red** for a test contact
whose local time is **8 PM**".

8 PM is *inside* 8 AM–9 PM, so it should be green. Either the window is wrong
or the test is. Ask before building; a compliance indicator that is wrong in
the permissive direction is the worse failure.

**Ticket 25 ships a conservative default** (8 PM = red, effective window
8 AM–8 PM) so we don't under-restrict calls before the client rules. The
cutoff is a one-line constant — flip it when D15 is answered. Still blocking
*correctness*, not the build.

→ tickets [14](issues/14-touch-2-timing-contradiction.md),
[25](issues/25-touch-2-dial-task-card.md)

### 🟡 D16 — What does Touch 1 say to a firm outside the county top 25?

Rank is publishable for the **top 25 per county only, never a bottom list**
(§1.8). But Touch 1's micro-ask promises "Reply YES to see where you rank in
[County]" and Touch 5's entire angle is County Visibility Rank. For a firm
ranked 40th there is no publishable rank.

Does Touch 1 fork into ranked and unranked variants? Is an unrankable firm
mailable at all? v2 does not address it. Interacts with `template_version`
(ticket 12) since a fork doubles the variant set.

→ ticket [19](issues/19-owner-visibility-score-touch-content.md)

### ⛔ D10 — ~~What does Touch 1 say, and who builds the score?~~ ANSWERED

**Dev 2 owns it** — Task 2.1, subtasks 2.1.1 (scraper + calculator), 2.1.2
(county rank + benchmarking), 2.1.3 (PDF compiler), plus Task 2.2 (Fee-Stack
one-pager). Touch 1 attaches the **score PDF**; Touch 3 attaches the
**Fee-Stack one-pager**; Touch 5's angle is **County Visibility Rank &
Scarcity**. No GIF, no video link.

**This was flagged as the September blocker. It is not one.** Remaining work
is an interface agreement with Dev 2 — see ticket 19. Residual copy question
moved to D16.

<details><summary>Original ask</summary>

Replaces D1. The ghost-shopper audit is prohibited and the video is held, so
Touch 1's proof is now the **Owner Visibility Score** (§1.8) — 100 points over
public observables. But:

- §1.8's presentation order (three lowest scores first, then score, then rank)
  was written for the *report*. Does the cold email open the same way? Leading
  with three criticisms of the recipient is a strong choice.
- Rank is publishable for the **top 25 per county only, never a bottom list**.
  Most recipients are unrankable in copy. Does Touch 1 fork into ranked and
  unranked variants?
- Below the data floor: "insufficient data", no rank. Is such a firm mailable
  at all? That is an ICP filter, not a copy detail.
- **Who computes the score?** It needs business profiles, website crawls, and
  state licence rolls. That is a data workstream, almost certainly not Dev 3's.
  **If it does not exist, it — not the sequencer — is the September blocker.**
- Decision register **O-14**: weights are given but the interpolation, data
  floor, missing-data denominator, tie-breaks and licence treatment are
  **not supplied** and must not be invented.

→ ticket [19](issues/19-owner-visibility-score-touch-content.md)

</details>

### ⛔ D11 — ~~Approval granularity: per send, or per batch?~~ ANSWERED

**Per send.** v2 3.1.1: "the sequencer queues a draft Slack card in
`#blackink-setter` containing the **full email preview (subject, body,
recipient)**. Email is NOT dispatched until a human rep clicks the Approve
button. Reject and Snooze buttons do not send."

One card per email, with the rendered preview. Autonomy is earned after
**50 consecutively approved clean sends** per template class.

Consequence stands: a 100-address campaign means 100 approval cards. Ticket 21
should raise the operational load with the client, but the *design* question
is closed.

<details><summary>Original ask</summary>

§1.6 and §2.7 require **every early-client send to be human-reviewed**. Link 2
of the acceptance contract needs a campaign to 100 addresses. Taken literally
that is 100 approvals for Touch 1 alone.

Is a batch card showing N rendered emails with one Approve-All acceptable, or
must each be individually judged? This is a promise about review, so it is the
client's call, not an ergonomics decision. Also needed: **who** approves, and
the fallback when they are unavailable (decision register **O-07** — business
hours, timezone, holidays and approver fallback are all missing).

→ ticket [21](issues/21-every-send-approval-workflow.md)

</details>

**Still open from D11:** *who* approves and the fallback when they are away
(**O-07**). With per-send cards and a 100-address campaign, an unavailable
approver stalls the entire sequence — this is now an operational risk, not a
footnote.

### 🟡 D14 — Outbound email send window: whose hours, what hours, what holidays?

**Not a requirement we are satisfying — a new decision.** Cross-checked
2026-09-03: the ground truth contains **no business-hours rule for outbound
email**. Every time-of-day rule in it is about a different channel or
direction:

- SMS quiet hours 9 PM–8 AM recipient local — SMS is cancelled this year
- "local calling hours enforced" / "state-specific calling hours" — Touch 2
  phone only
- "30 minutes in business hours" — the Respond **inbound** SLA, a different
  product

Decision register **O-07** names it as missing: *"Business hours, timezone,
holidays, approver fallback — SLA defined at high level only; named
approver/channel/fallback and schedule missing."*

Working decisions (2026-09-03): **day-grain tolerance** ✓ consistent with the
ground truth, and **send during business hours**. Needs from the client:

1. **Whose hours** — recipient-local, client-local, or Blackink ET?
2. **What hours**, and does it exclude weekends?
3. **Which holiday calendar**, if any?

**Note the conflict to resolve:** calendar-day offsets + business-hours-only
sending produces business-day behaviour by the back door (a Day 4 landing on
Sunday slips to Monday). Cleaner to choose one model outright — recommended:
business-day offsets with business-hours sends, since that is what the
combination actually yields.

**September simplification:** all ten launch counties (Hillsborough, Pinellas,
Orange, Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole) are **Eastern**,
so recipient-local and Blackink-local coincide. The timezone question can be
deferred to national rollout. Related gap: `contacts` has no timezone column,
and Touch 2's card (§11.3) requires "local timezone" and "current local time".

→ tickets [06](issues/06-sequencer-architectural-seat-and-scheduling.md),
[14](issues/14-touch-2-timing-contradiction.md)

### ⛔ D13 — ~~Who hosts the sending mailboxes, can we get SMTP?~~ ANSWERED

**Google Workspace / Outlook seats.** v2 shared DoD item 1: "email dispatches
from warmed **Google Workspace / Outlook inboxes** across dedicated domains
with verified SPF/DKIM/DMARC."

Real mailbox seats → **SMTP available** → `Message-ID` and `In-Reply-To`
controllable → Touch 3's threading DoD satisfiable, attachments work (v2
requires two), and replies land in monitored inboxes for 3.1.3. **Ticket 05
resolved: direct SMTP.**

Residual, moved to A14: do the seats exist, who pays, and how do we get
credentials for up to 34 of them?

<details><summary>Original ask</summary>

**The single deciding fact for the sender choice, and it is not ours to
decide.**

Ticket 04 eliminated Instantly as the *sender* — it cannot set `In-Reply-To`,
cannot attach files, and has no general send endpoint. But re-reading the
ground truth shows the blueprint used Instantly as the **mailbox and warmup
platform**, not as the sender:

- onboarding "assigns pre-warmed sending domains, **Instantly
  sub-workspaces**" (blueprint p30)
- Vera line-items "**Instantly mailbox costs**" (p33)
- the sentinel: "**Instantly quarantines** degraded domain; routes traffic to
  warmed backup domain pool" (p39)

The repo agrees — `InstantlyService` is warmup/analytics only, never a sender.
Those two roles are separable, so eliminating Instantly as sender does not
eliminate it as host.

Meanwhile the blueprint names the mailboxes as real seats: dispatch from
"warmed **Google Workspace/Outlook inboxes**" (p11), onboarding allocates
"6 **Google Workspace** mailboxes" (p22).

**Ask:**

1. Are the 34 sending mailboxes real **Google Workspace / Microsoft 365
   seats** we control, or are they hosted inside an **Instantly sub-workspace**?
2. Either way, **can we obtain SMTP credentials** for them?
3. Who pays for the seats, and do they exist yet?

**Why it decides everything downstream:** with SMTP we control `Message-ID`
and `In-Reply-To`, so Touch 3's threading DoD is satisfiable, and replies land
in real monitored inboxes — which cold outbound needs, since Touch 1 asks for
a reply and 3.1.3 must catch it. Without SMTP we inherit Instantly's header
limitations and **Touch 3's threading requirement becomes unmeetable**, at
which point either the mailboxes move or the DoD is renegotiated with the
client.

→ ticket [05](issues/05-choose-the-email-sender.md)

</details>

### 🟡 D12 — Inbound alias scheme (forwarding model ANSWERED; string scheme + attribution still owed)

Named as a missing input by the Source of Truth itself (**O-05**, **O-06**).
Mailbox OAuth is forbidden; clients forward to a Blackink alias.

**Answered 2026-09-03 by the client "Lead agent + Part 14" comment** — the
forwarding/return-path *mechanism*: delegated subdomain receives (separate from
the cold-outreach pool), one DNS record + a client forwarding rule at
onboarding, **Reply-To = client's own address**, **BCC the client on every
send**, thread history from first contact forward (no backfill), and the
loop-break (dedup on Message-ID + direction, since our BCC echoes back).

**Still owed:** (1) the actual **alias string scheme** — per-client address
format, uniqueness/collision, offboarding lifecycle (O-18: the client's forward
can't be revoked remotely, so the alias must be killable on our side); and
(6) **attribution** — how a forwarded message recovers the originating firm +
contact. These two keep 3.1.3 blocked — but it's now "blocked on the naming
convention + attribution", not "fully blocked".

→ ticket [20](issues/20-inbound-alias-scheme.md) (partial resolution),
[28](issues/28-reply-thread-storage-model.md)

### 🔴 D2 — Touch 2: Day 1–2, or within 60 seconds?

The sequence table says Touch 2 is Day 1–2. Subtask 3.1.2's DoD says the
Slack card must appear **within 60 seconds of Touch 1 dispatch**. The likely
reconciliation is that the *card* appears immediately and the *call* happens
Day 1–2 — but the 60-second figure is a testable acceptance line, and taken
literally it constrains the scheduler design for all of Task 3.1.

Ask: confirm the reconciliation, and confirm 60s is about card creation, not
call time.

→ ticket [14](issues/14-touch-2-timing-contradiction.md)

### 🔴 D3 — What is the 14-day cooldown protecting against?

`compliance_gate._check_cooldown` blocks any send within 14 days of the last
outbound touch. Touch 3 is Day 4 and Touch 5 is Day 10, so the documented
"re-check compliance before every touch" rule makes the sequence impossible.

Ask: was the 14-day rule a client compliance commitment, a deliverability
heuristic, or a placeholder? The answer decides whether we carve out an
intra-sequence exemption or change the cadence. **We must not quietly
weaken a real compliance commitment.**

→ ticket [01](issues/01-compliance-recheck-vs-14-day-cooldown.md)

### 🟡 D4 — Is the daily cap 30 or 50?

§7.2 states 30–50 per mailbox per day; the acceptance test asserts 50.
Ask: is the operative number fixed, per-client configurable, or ramped with
mailbox warmup age? Also confirm whether `clients.daily_send_ceiling`
(exists, unused) is the same concept.

→ ticket [08](issues/08-24h-send-cap-counting-substrate.md)

### 🟡 D5 — Booking: which calendar first, and does 3.2 own it?

The no-Calendly amendment says Google Calendar + Microsoft Graph, with
GoHighLevel as fallback. Ask:

- Which do actual customers use? Supporting both doubles the OAuth surface —
  is that real demand or speculative?
- What is GHL the fallback *for* — an outage, a client with neither, or
  clients already on GHL?
- Does the prospect self-serve a time (needs a hosted booking page — is that
  GHL?), or does the rep pick from their own free slots?
- **Does Task 3.2's booking engine own this**, leaving 3.1.3's button only to
  invoke it? If so this shrinks dramatically. Check first.

→ ticket [16](issues/16-book-meeting-without-calendly.md)

### 🟡 D6 — Is Touch 1/3/5 copy LLM-generated or a fixed template?

`config/prompt_variants.py` holds champion/challenger LLM *prompts*, implying
generated-per-prospect copy. `outbound_templates.py` implies fixed templates
with merge tags. The blueprint's Cora "drafts dynamic payloads", which reads
as generation. These are materially different products.

Ask: does the client expect every prospect's email to be individually
LLM-written, and if so who approves it before send? §23's Band 1 says drafts
need human approval until 50 clean reviews — is that the intended workflow
for Week 1, and who does the reviewing?

→ ticket [12](issues/12-define-template-version.md)

### 🟡 D9 — Is `market_metro`'s removal safe elsewhere?

D8 is answered (see below), but confirm nothing outside Task 3.1 depends on a
metro grouping before the `{city}` tag is repointed — §1.1 warns that postal
and property-level filters "must not silently recreate metro/ZIP-zone
commercial exclusivity."

---

## 2. Accounts, credentials and budget

Nothing here can be built against until it exists.

| # | What | For | Cost | Status |
|---|---|---|---|---|
| A1 | **Email sending provider** — decision pending (SMTP vs Postmark/SES/Resend/SendGrid) | All of 3.1.1. Instantly is eliminated | varies | 🔴 |
| A2 | **Sending domains + mailboxes** — list supplied 2026-09-03, see [SENDING-DOMAINS.md](SENDING-DOMAINS.md). **3 domains short** of §7.3 if the target is 5 clients; Pool C names 3 clients but holds only 6 domains (2 clusters) | §7.3 tenant isolation | domain reg | 🟡 partial |
| A3 | **DNS + warmup** — SPF/DKIM/DMARC per domain, mailbox warmup. **Not in Dev 3's scope and not a code deliverable**; blueprint p6 has DNS access *delegated* and warmup schedules initialized *ahead of campaign launch*, p22 allocates *from the pre-warmed pool*. We only read `warmup_status` / `quarantine_state` and verify at GATE-08. **But it is a hard prerequisite** — no warm domain, no cold send | Deliverability | time (weeks) | 🔴 external |
| A14 | **Mailbox seats + SMTP credentials** — hosting answered (Google Workspace / Outlook, D13). Still needed: do up to 34 seats exist, who pays, and how do we obtain credentials (app passwords vs OAuth2 SMTP)? | Ticket 05 | seat licensing | 🔴 |
| A4 | ~~Sendspark~~ — **DO NOT PURCHASE.** Video held for September (§1.5); no account was supplied. Trigger hook + disabled provider row only | — | $0 | ⛔ cancelled |
| A5 | ~~DNC registry vendor~~ — **contracted: Tracerfy.** `src/tasks/dnc_refresh.py` is a real integration. `CLAUDE.md`'s "no vendor contracted yet" is **stale**. Still needs `DNC_VENDOR_API_KEY` set, and the gate's own `DncProvider` remains a stub — see D9 | Compliance gate | ~$0.02/phone | 🟡 key needed |
| A6 | **Email verification vendor** | §4.2 "verified corporate email only". Also stubbed | ? | 🔴 |
| A7 | ~~Twilio~~ — **cancelled.** No SMS this year (§1.5); Telnyx is the selected carrier and the doc says remove Twilio assumptions. 3.1.3 is **email-only** | — | $0 | ⛔ cancelled |
| A12 | **Inbound alias domain** — a receiving domain for client forwarding, kept separate from both the brand domain and the cold-outreach pool | 3.1.3 (D12) | domain reg | 🔴 |
| A13 | **Email verification provider** — Link 2 needs 100 *verified* addresses; nothing currently sets `email_status = VERIFIED`. §4.2 names Hunter/Anymail/Tracerfy but warns no contracts are proven | Gate + Link 2 | ? | 🔴 |
| A8 | **Google Workspace OAuth app** | Booking (D5) | — | 🟡 |
| A9 | **Microsoft Graph app registration** | Booking (D5) | — | 🟡 |
| A10 | **GoHighLevel** | Booking fallback (D5) | ? | ⚪ |
| A11 | **Instantly Hypergrowth** — *only* if we keep it for warmup analytics or tracking after dropping it as sender | Optional | ~$97/mo | ⚪ |

**A5 and A6 are the quiet ones.** Both compliance providers are stubs today.
Cold outbound against unverified emails and an unchecked DNC list is a
compliance exposure, not a missing feature — raise it explicitly rather than
letting it ship as a stub.

**On A1/A2:** whichever provider is chosen must send as *our own* warmed
domains, not a shared vendor pool — §7.3 forbids pooling sending reputation
across clients, and that is the entire reason for the per-client cluster.

---

## 3. Content and assets

| # | What | For | Status |
|---|---|---|---|
| C1 | ~~Sendspark base video~~ — **cancelled**, video held for September | — | ⛔ |
| C2 | **Touch 1/3/5 approved copy** — or approval of the LLM-generation workflow (D6) | 3.1.1 | 🔴 |
| C3 | **Owner Visibility Score report** — replaces the Speed Loss PDF. Shows score + data coverage, three lowest observations with named comparisons, then county rank. Still no renderer; decide PDF vs hosted page (D10) | Touch 1 proof | 🔴 |
| C4 | ~~Ghost-shopper audit data~~ — **prohibited** (§1.8 "do not build the ghost shopper or submit pretext inquiries"). Replaced by C3 | — | ⛔ |
| C5 | **ADD-8-Lite Fee-Stack material** — lease renewal fees, maintenance markups, tenant setup, pet rent, resident benefits, Ryse/utility partners | Touch 3 angle | 🟡 |
| C6 | ~~Metro Speed Index~~ — **cancelled**, no metro layer (§1.1). Touch 5 becomes a county/public-observation angle; the seat-scarcity claim also goes (county seats out of September, §1.9). What Touch 5 now says is open — see D10 | Touch 5 angle | 🔴 |
| C7 | **LinkedIn connection-note template** — ≤300 chars, merge tags first-name/company/county. Ticket 26 ships a score-free placeholder for September; swap in client copy when supplied (no code change) | Touch 4 | 🟡 needed soon |
| C8 | **Calling-hours policy** — what counts as valid local calling hours, and any per-state variation | Touch 2 indicator (DoD tests 8 PM → red) | 🟡 |
| C9 | **Touch 2 call-outcome capture** — should the "Mark Called" button capture an outcome (connected / voicemail / no-answer), or is a plain DONE enough? Ticket 25 ships plain DONE for September; richer capture is a later ask (and may overlap Dev 4's post-meeting modal) | Touch 2 card | ⚪ confirm |

**C3 and C4 are sequencing risks.** Touch 1's DoD requires the PDF attached,
and `assert_audit_complete` implies no audit means no Touch 1. If the PDF
renderer is genuinely Week 2 work, Touch 1 cannot pass its DoD in Week 1 —
that needs flagging now, not at test time.

---

## 4. Internal / cross-dev (not client, but blocking)

Recorded here because they block the same tickets and need the same chasing.

- **EventLogger ownership.** §24 describes shared infrastructure every Week 1
  component writes through. It does not exist. Confirm with the team whether
  Dev 3 builds it or waits — other devs have active branches
  (`week1-subtask-1.2.x`, `feature/week1-metrics-and-sandbox`) where it
  may already be landing. → ticket [02](issues/02-eventlogger-ownership-and-scope.md)
- **Event name ratification** — `outbound_touch_dispatched` vs `touch_sent`.
  A cross-developer integration contract; a mismatch reports 0% silently
  rather than erroring. → ticket [03](issues/03-event-taxonomy-for-task-3.1.md)
- **Slack `HASH_VERSION` bump** for 24h card expiry touches other devs'
  cards, not just ours. → ticket [15](issues/15-slack-card-24h-expiry.md)
- ✅ ~~**Task 3.2 boundary** — `open_meeting_outcome_modal` is cited in the
  handoff as callable but does not exist.~~ **Resolved 2026-09-03: it does
  exist**, on `feature/week1-metrics-and-sandbox` @ `ec7e71d`. The
  earlier survey checked only `main`. Task 3.2, not 3.1 — call signature and
  a tenant-resolution race raised back to Dev 4 are recorded in the map's
  Out of scope section.
- **Event names are negotiable, not fixed** — Dev 4 has offered to adapt the
  daily-digest query if Dev 3 uses names other than `email_opened` /
  `email_clicked`. Default is to match theirs; any divergence must be told to
  them explicitly rather than discovered at 0%.

---

## Answered

<!-- Move items here as they are resolved. Keep the question, add the answer
     and the date. Format:

### ✅ D0 — <the question>  (answered YYYY-MM-DD)
<the answer, and anything it changed>
-->

### ✅ D7 — Is the Sept 11–16 interim-bridge window still correct? (2026-09-03)

**Yes, and it is next week.** Source of Truth §2.8 dates the milestones:
marketing demonstration **Sep 11**, assisted pilot **Sep 16–18**,
portal/preflight **Sep 25**, full chain and two-tenant zero-code repeat
**Sep 30 2026**. The bridge window brackets the demo and the pilot.
**3.1.3 is urgent, not stale** — and it is blocked on D12, which is a missing
input the client still owes.

### ✅ D8 — Where does metro data come from? (2026-09-03)

**Nowhere — there is no metro layer.** §1.1: "The county is the unit
everywhere: data, ranking, contract, and seat. There is no metro layer."
§2.1: "Replace metro-dependent fields/algorithms with county contracts.
Preserve old `market_metro` schema text as historical, not current
authority."

Do **not** add the column. Repoint `{city}` to a county display name — and
note a slug is not a display string, so "Hillsborough County" needs a source.
Launch counties are Hillsborough and Pinellas first, then Orange, Duval, Polk,
Pasco, Lee, Brevard, Volusia, Seminole. Closed ticket 13.

### ✅ DNC and email — does a DNC hit block Touch 1? (2026-09-03, grilling)

**Yes for a hit; no for a missing phone.** A known DNC hit blocks an email
touch — a cross-channel courtesy beyond what CAN-SPAM requires, since DNC
governs calls. But a **missing phone number is not a hit**: there is no number
for the registry to be silent about, so it must not block. Without this
distinction, a phoneless prospect was permanently unmailable.

**Clearance moves to promotion time.** The Tracerfy sweep is monthly, and the
gate's live `DncProvider` is a stub that ABSTAINs — so a freshly-ingested
prospect was blocked for up to 31 days, making "Touch 1, Day 0" impossible.
Scrubbing at promotion preserves the FTC 31-day safe-harbor design and puts
the ~$0.02/phone cost at ingest. Requires a change to Dev 1's
`promotion_sweep`.

Still open on that thread: the unchecked and stale cells, and the 14-day
cooldown itself — ticket 01 remains open.
