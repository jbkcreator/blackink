# Source of Truth — reconciliation against existing docs and code

Validates `Blackink_Source_of_Truth.md` (compiled 3 Sep 2026) against
`Blackink_Dev3_Task_3.1_Complete_Reference.md`, `CLAUDE.md`, the repo on
`main` @ 8974773, and the Task 3.1 wayfinder map in
`.scratch/task-3.1-outbound-sequencer/`.

**The Source of Truth wins.** Its own authority rule: comments override
blueprint, blueprint overrides business doc, unified spec is supplementary.
The Dev 3 Task 3.1 reference is downstream of the blueprint, so **wherever
the two disagree, the Source of Truth controls.**

Written 2026-09-03. Verify before acting — this is a reading of the document,
not a client confirmation.

---

## Summary

Task 3.1's five-touch structure **survives**. Its *content* largely does not.

Three of the pillars the Dev 3 reference builds Touch 1 on — the ghost-shopper
audit, the Sendspark video, and the metro rank — are each explicitly
cancelled or held. Task 3.1.3's SMS half is cancelled outright. The sequencer
engine, mailbox pool, pacing, compliance gating, threading, and event ledger
are all untouched and still needed.

---

## 1. Cancelled — do not build

### 1.1 🔴 Ghost shopper — explicitly prohibited

> "**Do not build the ghost shopper or submit pretext inquiries.**" (§1.8)
> "Remove the old readiness prerequisite requiring a ghost-shopper score or
> video ID." (§2.1)
> "Replace ghost-shopper claims, live video, metro ranks, and active seat
> scarcity with permitted public evidence." (§2.3)
> "Do not activate deprecated ghost-shopper calculations." (§5.2)

**This removes Touch 1's entire premise.** The Dev 3 reference §2.3, §4.1 and
§4.4 build Touch 1 around `audit_speed_score_sec`, audit response latency, and
a Speed & Revenue Loss PDF derived from a pretext inquiry.

Replaced by the **Owner Visibility Score** (§1.8) — a 100-point score over
*public observables* (owner page, contact form, mobile/HTTPS, review count and
recency, response rate, rating, after-hours route, response lag, licence
tenure). Explicitly renamed from "Owner Response Score" and explicitly *not*
secretly tested response performance.

Repo impact:

- `src/services/audit_report.py` reads `ghost_shopper_audit` events. That event
  source is now deprecated, and its `{audit_speed}` / `{loss_dollars}` context
  must be rebuilt on the Owner Visibility Score and the new loss model.
- **`assert_audit_complete` must be REMOVED from the dispatch path, not
  rebuilt.** *(Corrected 2026-09-03 — an earlier revision of this document said
  "rebuild", contradicting the §2.1 line quoted above.)* §2.1 is explicit:
  "**Remove** the old readiness prerequisite requiring a ghost-shopper score or
  video ID. Both underlying production features are held; **otherwise the old
  gate would block all legitimate September campaigns**."

  So the score is Touch 1 **content**, not a **gate**. Touch 1 may fire for a
  contact with no score — §1.8 only requires showing "insufficient data" and
  no rank below the data floor; it never says such a firm is unmailable.
  §5.2 flags the blueprint's own `evaluate_campaign_readiness`
  (`src/compliance/gate_evaluator.py`, SQL/PLpgSQL, "requires held audit/video
  inputs and uses old field names") as the same problem — **audit the repo for
  a port of it and remove that too.**
- Loss-report model changes: no longer latency-derived. New defaults are
  **8% management fee, 30-month average owner tenure, ~$100/door/month**
  (§1.8), stored per client, assumptions shown on the report, labelled as
  estimates. "Do not reuse an unobserved ghost-shopper latency as though it
  were measured."
- Touch 2's card field "ghost-shopper response latency in hours/minutes"
  (Dev 3 ref §11.3) has no source. Replace with score/public observations.
- Score display requires **data coverage** alongside it ("76/100, 94% data
  coverage"), lead with the **three lowest-scoring observations**, then score,
  then county rank. Publish **top 25 per county only, never a bottom list**.
  Below the data floor: "insufficient data", no rank.

### 1.2 🔴 Sendspark / personalized video — held for September

> "September includes a video trigger hook and disabled/configured provider
> row only, **not the video generation/delivery flow**. **No Sendspark account
> was supplied.**" (§1.5)
> "If video is later hosted, use `watch.getblackink.com`."

**Directly contradicts the work done today.** Ticket 10's research concluded
Sendspark was fit for purpose, and ticket 17 was opened to provision a $99/mo
Growth workspace. The Source of Truth says **do not** — build the trigger hook
and a disabled provider row only.

Consequences:

- Ticket **17 (provision Sendspark) → out of scope.** Do not buy the plan.
- Ticket **10's findings remain useful** as future reference but must not
  drive September build.
- Ticket **18 (verify watch webhooks) → moot.**
- The Touch 1 GIF thumbnail, `{video_url}` merge tag, and video watch
  percentage are all **held**.
- Touch 2 card field "Sendspark video watch percentage" (Dev 3 ref §11.3) and
  the 3.1.3 reply-card equivalent (§16) have no data source. Remove.
- The spec conflict I raised in ticket 11 (audit score cannot go inside a
  Sendspark video) is **moot** — there is no video.

### 1.3 🔴 SMS and Twilio — cancelled for the year

> "**No AI voice this year**, outbound or callback. **No SMS this year** is
> stated in W1-4." (§1.5)
> "**Telnyx is the selected carrier**… **remove conflicting Twilio
> assumptions**." (§1.5)
> "A click, video view, or positive email is not to be used to re-enable the
> cancelled SMS flow." (§2.2)

- Task **3.1.3's Twilio inbound SMS webhook is cancelled**, along with its
  DoD line and the `#sales-replies` SMS card path. 3.1.3 becomes
  **email-only**.
- The conditional engaged-SMS nudge (Dev 3 ref §22) — already out of scope on
  the map — is now definitively dead, and the "unlock after engagement" logic
  is explicitly forbidden as a re-enable route.
- `src/services/cold_sms_gate.py` and `src/services/sms_quiet_hours.py` remain
  correct as *blocks* but have nothing to gate this year.
- 10DLC: "not a September SMS/billing dependency" (§2.2), though the landing
  page material should still be submitted.

### 1.4 🔴 Metro — abolished; county is the unit

> "The **county is the unit everywhere**: data, ranking, contract, and seat.
> **There is no metro layer.**" (§1.1)
> "Replace metro-dependent fields/algorithms with county contracts. Preserve
> old `market_metro` schema text as **historical, not current authority**."
> (§2.1)

**This resolves ticket 13 outright: do not add `market_metro`.** Remap the
`{city}` merge tag to county. `companies.county_slug` already exists and
`county_allocations` already makes county first-class — the schema was right
and the merge tag was wrong.

Also: "Postal addresses and property-level filters remain useful data, but
must not silently recreate metro/ZIP-zone commercial exclusivity."

### 1.5 🔴 Mailbox OAuth — do not build

> "**Do not build Gmail or Microsoft Graph mailbox access.** Calendar OAuth
> remains. Clients forward their owner-inquiry address to a Blackink
> address." (§1.6)

Task 3.1.3 offers "email webhook **or IMAP polling**" (Dev 3 ref §15.1).
IMAP/OAuth against a *client's* mailbox is out. Ingestion is **forwarding to a
Blackink alias**. Note §1.6 also says "Build thread history from the first
forwarded contact; whole-inbox coverage and preexisting history are not
available" — which constrains 3.1.3's "last 3 messages" card requirement
(§16): there may be no prior history to show.

O-05 in the decision register flags the alias scheme, uniqueness, collision
policy and lifecycle as **missing inputs**. 3.1.3 cannot be fully specified
until that lands.

---

## 2. Confirmed — the map was right

| Item                                                                                                                          | Source of Truth      | Map status                                                             |
| ----------------------------------------------------------------------------------------------------------------------------- | -------------------- | ---------------------------------------------------------------------- |
| No Calendly; Google Calendar + MS Graph, GHL fallback                                                                         | §1.5, §2.4 state 6 | ✅ ticket 16 correct                                                   |
| LinkedIn manual deep links only, no scraping/browser automation                                                               | §2.3                | ✅ Dev 3 ref §12.3 holds                                              |
| 30–50 cold emails per mailbox/day                                                                                            | §2.2                | ✅ ticket 08                                                           |
| Bounce >3% / complaints >0.08% rolling 48h → quarantine + warmed reserve                                                     | §2.2                | ✅                                                                     |
| 20 domains / 40 mailboxes is**"planned allocation, not verified inventory"**                                            | §2.2                | ✅ matches the −3 shortfall in SENDING-DOMAINS.md                     |
| Slack cards bind exact payload + recipient + config to SHA-256; reject stale approvals                                        | §2.2                | ✅ ticket 15                                                           |
| Suppression works "across clients and sending domains**at dispatch time, including updates during a running campaign**" | §2.1                | ✅**validates the per-touch re-check** in ticket 01              |
| Stale/missing required data →`UNKNOWN`/`ABSTAIN`; cached degraded results labelled, never falsely fresh                  | §2.8                | ✅ validates the gate's ABSTAIN philosophy                             |
| `company_id` SHA-256 vs UUID conflict must be resolved explicitly                                                           | §2.1, §5.2         | ✅`CLAUDE.md` already chose SHA-256                                  |
| Tracerfy named as a provider                                                                                                  | §4.2                | ✅ confirms DNC vendor;`CLAUDE.md`'s "no vendor contracted" is stale |
| DNC / non-poach / quiet-hours / rate caps / persistent halt stay deterministic                                                | §2.2                | ✅                                                                     |
| Relay persistent halt: "a TTL or restart must never re-arm a paused campaign"                                                 | §1.4                | ✅                                                                     |
| Cold-outreach domains are separate from the brand domain `getblackink.com`                                                  | §1.5                | ✅ the 20 supplied domains are the outreach pool                       |

---

## 3. Changed — the sequence survives, reframed

> "The older Day 0, Days 1–2, Day 4, Day 7, Day 10 sequence remains a
> **configurable starting cadence**: initial proof email, human dial task,
> fee-stack follow-up, manual LinkedIn follow-up, **final county/public-
> observation angle**, then cooling." (§2.3)

| Touch        | Dev 3 reference                                | Source of Truth                                                                                                                                |
| ------------ | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 (Day 0)    | Speed Loss Audit + Sendspark video + Reply-YES | **Initial proof email** — Owner Visibility Score from public evidence. No video, no GIF, no ghost-shopper latency                       |
| 2 (Day 1–2) | Phone task w/ audit latency + watch %          | **Human dial task** — context from public score, not audit/video                                                                        |
| 3 (Day 4)    | Fee-Stack Opportunity                          | **Fee-stack follow-up** — unchanged                                                                                                     |
| 4 (Day 7)    | LinkedIn deep-link                             | **Manual LinkedIn follow-up** — unchanged                                                                                               |
| 5 (Day 10)   | Metro Speed Index & scarcity                   | **County/public-observation angle.** Metro rank *and* "active seat scarcity" both removed — county seats are out of September (§1.9) |

Note "**configurable** starting cadence" — the day offsets are now explicitly
configuration, not hardcoded constants. That should shape ticket 07's state
model and ticket 06's scheduler.

### 3.1 🟡 Human approval on every early-client send

> "Apply the comments' behavioral rule first: **every early-client send
> reviewed**; class-specific earned authority; no agent discretion over
> legal/financial policy." (§2.7)
> "Start with human approval for every send. A template class may earn the
> tighter promise after **50 clean approvals**." (§1.6)

The sequencer **cannot autonomously dispatch** for early clients. Touches 1/3/5
need an approval step before send. This strongly favours the
`agent_work_orders` route in ticket 06 — it already has autonomy bands, risk
classes, and hash-bound Slack approval cards. Decision register **O-08** flags
"Band 2 from launch versus every-send review at first client" as unresolved,
with the every-send correction controlling.

### 3.1b 🟡 Engines are a config row — and holdouts exist

*(Added 2026-09-03 — missed in the original revision.)*

> "Engines are **configuration row + execution adapter**, carrying routing,
> templates, sequence, **county scope**, tier, and **holdout settings**."
> — §2.1

The map's design (ticket 07) has `sequence_runs` + `agent_work_orders` and
**no engine configuration row**. §2.3 makes the cadence "configurable" but the
map never said where configuration lives; per §2.1 it belongs in an engine
row, not scattered across tables or constants.

**Holdout settings appear nowhere in the map at all.** A holdout is a control
group deliberately excluded from sends — it changes enrollment logic, not just
reporting, and it interacts with the champion/challenger experiment registry
(§2.7) that ticket 12 touches.

Consequence: an engine/campaign config table should exist and `sequence_runs`
should reference it. Cheap now, expensive to retrofit once runs exist.

### 3.2 🟡 Ten reply classes for 3.1.3

> `HOT_LEAD`, `QUESTION`, `OBJECTION`, `LATER`, `NURTURE`, `UNSUBSCRIBE`,
> `COMPLAINT`, `LEGAL_GRIEF`, `WHALE_OWNER`, `PARTNER` (§2.3)

The Dev 3 reference's 3.1.3 has only three Slack actions (Reply in Thread /
Book Meeting / Mark Opt-Out) and no classification. Keep opt-out and
legal/complaint handling deterministic.

### 3.3 🟡 More Slack channels than are wired

Source map adds `#blackink-economics`, `#client-{name}-growth`,
`#client-{name}-launch` to the six in `config/slack_channels.py`. Also:
"A channel inventory is not a named approval owner or fallback roster" —
decision register **O-07** flags business hours, timezone, holidays and
approver fallback as **missing inputs**.

### 3.4 🟡 The real acceptance test for Task 3.1

Nine-link contract, **Link 2 — "Reach them"** (§1.12):

> "Campaign to **100 verified addresses** with **bounce under 3%**;
> deliberately **noncompliant draft hard-blocked**; **mid-campaign suppression
> prevents any later send**."

This is a different and harder bar than the Dev 3 reference's 60-recipient cap
test. The 60→50 mailbox-cap test still matters, but Link 2 is what the
September gate is judged on.

**Three separate assertions — all three must be built:**

1. **100 verified addresses.** Requires `email_status = 'VERIFIED'` to be
   reachable. Today `promotion_sweep.py:214` writes `"UNVERIFIED"`, nothing
   ever sets `VERIFIED`, and `evaluate_compliance_gate`'s `email_provider`
   parameter is accepted and never used. **This blocks every send, in test or
   production.** → CLIENT-ASKS **A13**.
2. **Bounce under 3%.** A live deliverability outcome, not a unit test.
   Depends on warm domains (A3) and on ticket 09's two sentinel bugs being
   fixed, since a burning domain currently keeps sending.
3. **Deliberately noncompliant draft hard-blocked.** *(Added 2026-09-03 — the
   original revision quoted this but never tracked it.)* Validate the
   **rendered draft** immediately before send and refuse a noncompliant one.
   `outbound_templates.require_client_firm_tag` and `validate_template`
   already exist and are **not wired into any dispatch path**. §2.2 backs the
   requirement: "Outbound templates require the `{client_firm}` tag, ensuring
   the recipient clearly sees the operating company name." Buildable now, no
   client dependency.

Plus **"mid-campaign suppression prevents any later send"** — exactly ticket
01's per-touch gate, and §2.1's "suppression must work across clients and
sending domains at dispatch time, including updates during a running
campaign."

Link 3 — "Talk to Blackink" covers 3.1.3: a reply "reaches a human, is
recorded against the originating firm, and is answered within an explicitly
configured SLA."

---

## 4. Dates — the September window is real and imminent

> Marketing demonstration **Sep 11**; assisted pilot **Sep 16–18**; portal/
> preflight **Sep 25**; full chain and two-tenant zero-code repeat
> **Sep 30 2026**. (§2.8, §1.1)

Today is 2026-09-03. **This resolves CLIENT-ASKS D7**: the Dev 3 reference's
"interim reply bridge active September 11–16" is genuinely next week — it
brackets the marketing demo and the assisted pilot. 3.1.3 is urgent, not
stale.

Response SLA is **30 minutes in business hours, next business morning after
hours** (§1.6) — and explicitly "do not inherit the older sub-60-second,
five-minute/24×7, or day-one autonomous wording." The Dev 3 reference's
30-second *surfacing* target (§15.3) is about getting a card into Slack, which
is compatible; but §4.1's demo screen 5 says to replace the "<60-second
demonstration with permitted email/SLA behavior."

---

## 5. Open items the Source of Truth explicitly does **not** resolve

From the decision register (§5.1) — relevant to Task 3.1:

| ID             | Missing input                                                                                                                                                               |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **O-05** | Per-client inbound alias scheme, uniqueness, ownership, lifecycle, collision policy, forwarding verification —**blocks 3.1.3**                                       |
| **O-06** | Reply-To client address vs persistent thread handling; "prove subsequent replies are forwarded back once; avoid BCC loops and duplicate ingestion" —**blocks 3.1.3** |
| **O-07** | Business hours, timezone, holidays, approver fallback — blocks the SLA and Touch 2's calling-hours indicator                                                               |
| **O-08** | Band naming vs every-send review — blocks ticket 06's autonomy design                                                                                                      |
| **O-23** | Client agreement text, cleared state/county scope,**the September 11 real firm** — not supplied                                                                      |

Note **O-14**: the Owner Visibility Score's category weights are given but
"scoring interpolation/data floor absent — do not invent normalized formula,
missing-data denominator, tie-breaks or licence treatment." So Touch 1's new
proof content is itself not fully specified.

---

## 6. Recommended action on the wayfinder map

| Ticket                   | Action                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| 01 compliance re-check   | **Keep** — reinforced by §2.1's dispatch-time suppression rule                      |
| 02 EventLogger           | **Keep** — §2.1 makes generic events the first thing built                          |
| 03 event taxonomy        | **Keep** — revise payload: no `sendspark_video_id`, no `ghost_shopper_speed_sec` |
| 04 Instantly research    | Closed — still valid                                                                       |
| 05 sender choice         | **Keep** — unaffected                                                                |
| 06 architectural seat    | **Keep** — every-send approval now favours `agent_work_orders`                     |
| 07 sequence state        | **Keep** — add "cadence is configurable"                                             |
| 08 send cap              | **Keep** — add Link 2's 100-address / <3% bounce test                                |
| 09 sending_domain        | **Keep**                                                                              |
| 10 Sendspark research    | Closed —**findings now out of scope for September**                                  |
| 11 Touch 1 content       | **Rewrite** — Owner Visibility Score, not audit PDF + video                          |
| 12 template_version      | **Keep**                                                                              |
| 13 market_metro          | **Close as resolved** — county is the unit; do not add the column                    |
| 14 Touch 2 timing        | **Keep**                                                                              |
| 15 Slack 24h expiry      | **Keep**                                                                              |
| 16 Book Meeting          | **Keep** — confirmed direction                                                       |
| 17 provision Sendspark   | **Close — out of scope.** Do not purchase                                            |
| 18 verify watch webhooks | **Close — moot**                                                                     |
| *new*                  | Owner Visibility Score: what does Touch 1 actually say?                                     |
| *new*                  | Inbound alias scheme (O-05/O-06) — blocks 3.1.3                                            |
| *new*                  | Every-send approval workflow (O-08)                                                         |

---

## 7. Caution

The Source of Truth says of itself: *"This is a consolidated requirements
reference, not evidence that the platform has been implemented… No
application repository was supplied."* Its statements about repo state
(§4.3's PR list, mocked DNC, missing tests) are **source-reported, not
verified** — and several are already wrong against `main`. Trust it for
**requirements and decisions**; trust the repo for **current state**.
