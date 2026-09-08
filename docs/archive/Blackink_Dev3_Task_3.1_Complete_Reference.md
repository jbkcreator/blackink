# Project Blackink — Dev 3 Task 3.1 Complete Reference

## Scope

**Developer 3:** Outbound Sequencer & Booking Engine  
**Task 3.1:** Multi-Touch Outbound Sequencer & Interim Reply Bridge  
**Subtasks covered:**
- 3.1.1 — Core Sequencer Engine & Email Touch Dispatch (Touches 1, 3, 5)
- 3.1.2 — Phone Task Bridge & LinkedIn Deep-Link Generator (Touches 2 & 4)
- 3.1.3 — Interim Inbound Reply Bridge & Slack Routing Cards

This document consolidates all Task 3.1 requirements and directly relevant integration details from the supplied Week 1 task split and the Complete Implementation Blueprint. A final section records the later handoff amendments supplied in chat, clearly separated from the source documents.

---

# 1. Task 3.1 — Multi-Touch Outbound Sequencer & Interim Reply Bridge

Task 3.1 owns the Week 1 outbound sequence and the temporary human/reply bridge around it.

The end-to-end outbound sequence is:

| Touch | Channel | Timing | Purpose |
|---|---|---:|---|
| Touch 1 | Cold Email | Day 0 | Speed Loss Audit + Sendspark video + Reply-YES micro-ask |
| Touch 2 | Human Phone Call via Slack task | Day 1–2 | Audit follow-up call using audit/video context |
| Touch 3 | Cold Email | Day 4 | Fee-Stack Opportunity |
| Touch 4 | LinkedIn Deep-Link, manual | Day 7 | Executive peer-networking handoff |
| Touch 5 | Cold Email | Day 10 | Metro Speed Index & Scarcity |
| Conditional | Engaged SMS nudge | Post-engagement only | Direct scheduling nudge; never cold SMS |

The blueprint describes the sequence as a five-touch personalized outbound campaign fed by ghost-shopper audit data, the dynamic PDF loss report, and Sendspark personalization. The outbound sequence then flows into the interim reply bridge and later booking/show-rate workflows.

## Governing principles for Task 3.1

- Compliance is deterministic and must be re-evaluated at execution time.
- Cold outreach is email-first. Cold SMS is prohibited.
- Human phone activity is represented as a Slack task, not automated cold calling.
- LinkedIn activity is manual; no automated browser interaction or scraping is permitted.
- Sending identity is tenant-isolated and mailbox-capped.
- Every outbound and inbound interaction must be represented in the shared events ledger through the central event logging layer.
- Slack interactive actions require payload-bound cryptographic verification.
- A contact that opts out, replies, or becomes ineligible must not continue through the cold sequence.

---

# 2. Dependencies and upstream inputs

Task 3.1 is not standalone. It consumes outputs from other Week 1 systems.

## 2.1 Contact and company data

The sequencer works from `contacts` and related company data. Relevant fields include:

- `contact_id`
- `company_id`
- `first_name`
- `last_name`
- `email`
- `phone`
- `linkedin_url`
- `is_opted_out`
- `suppression_state`
- `dnc_clean`
- `compliance_eligibility`
- `last_outbound_touch_at`
- company `domain`
- company `company_name`
- company `door_count_est`
- company `market_metro`

For cold outbound, the key execution state is:

```text
compliance_eligibility = 'EMAIL_COLD_ELIGIBLE'
```

## 2.2 Compliance gate

The blueprint defines a deterministic compliance waterfall:

1. Global / explicit opt-out check.
2. Cross-client non-poach validation.
3. DNC registry and quiet-hours check.
4. Warm-channel waterfall routing.

Cold prospects are eligible for verified email and human phone tasks. Transactional SMS unlocks only after engagement or a confirmed appointment. Cold outbound SMS is strictly banned.

Task 3.1 must not bypass this gate. The sequencer and manual-task generation must re-check eligibility immediately before each step.

## 2.3 Ghost-shopper audit outputs

Touch 1 and Touch 2 depend on ghost-shopper data, including:

- `audit_speed_score_sec`
- exact audit response latency
- Speed & Revenue Loss Report PDF
- company-specific benchmark information

The blueprint describes the PDF as containing the timestamped form submission/reply comparison, metro benchmark comparisons, and modeled lost revenue.

## 2.4 Sendspark outputs

Task 3.1 consumes:

- Sendspark personalized landing page / video URL
- Sendspark video identifier
- animated GIF thumbnail
- video engagement/watch percentage when available

The dynamic page uses company/audit merge data. The GIF previews the prospect's website and audit score and links to the Sendspark page.

## 2.5 Fee-Stack output

Touch 3 uses the ADD-8-Lite Fee-Stack Opportunity material. The blueprint describes this artifact as highlighting fee leakage such as:

- lease renewal fees
- maintenance markups
- tenant setup fees
- pet rent share
- resident benefits packages
- ancillary partner opportunities such as Ryse rent advances / utility concierge integrations

---

# 3. Subtask 3.1.1 — Core Sequencer Engine & Email Touch Dispatch

## 3.1 Purpose

Build the core sequencer orchestrator that schedules and dispatches the three automated cold-email touches for eligible contacts:

- Day 0 — Touch 1
- Day 4 — Touch 3
- Day 10 — Touch 5

The sequencer must re-check compliance before every touch, not only when the sequence begins. It permanently halts future cold sequence execution for a contact who opts out, replies, or otherwise becomes ineligible between touches.

Touches 1 and 3 must belong to the same email thread. Touch 5 is the final cold email and starts a 30-day cooling period.

---

## 3.2 Sequencer lifecycle

Conceptually:

```text
Eligible Contact
  -> schedule sequence
  -> Touch 1 due
  -> compliance re-check
  -> select assigned warmed mailbox
  -> build/send Touch 1
  -> record outbound event
  -> schedule/await next step
  -> Touch 2 human task handled by 3.1.2
  -> Touch 3 due
  -> compliance/reply/opt-out re-check
  -> thread to Touch 1
  -> send/log
  -> Touch 4 manual LinkedIn task handled by 3.1.2
  -> Touch 5 due
  -> compliance re-check
  -> send/log
  -> write 30-day cooling timestamp
```

The task specification requires permanent stopping of the cold sequence when the prospect opts out, replies, or becomes ineligible.

---

## 3.3 Pre-send eligibility re-check

Before **every** email touch, read the contact's current compliance state.

Required eligible value:

```text
EMAIL_COLD_ELIGIBLE
```

This is intentionally a per-step check. A contact who was eligible on Day 0 may not still be eligible on Day 4 or Day 10.

At minimum, sequence execution must stop if:

- `is_opted_out = TRUE`
- compliance eligibility changes away from `EMAIL_COLD_ELIGIBLE`
- a prospect reply has been recorded
- the contact otherwise fails the compliance gate

For Touch 3 specifically, the source explicitly says to verify there is no prior opt-out or positive reply before dispatch.

---

# 4. Touch 1 — Day 0 Cold Email

## 4.1 Content

Touch 1 is the Speed Loss Audit + Sendspark Video email.

Required content/components:

- Speed Loss Audit / Speed & Revenue Loss PDF attachment
- prospect-specific animated GIF thumbnail
- GIF linked to the Sendspark personalized landing page
- low-friction reply micro-ask:

```text
Reply YES to see where you rank in Tampa
```

- open tracking active
- click tracking active

The blueprint describes the angle as personalized speed-loss proof backed by the ghost-shopper audit, dynamic revenue-loss model, and personalized Sendspark video.

## 4.2 Recipient requirements

The blueprint says Touch 1 is for a **verified corporate email only**.

## 4.3 Tracking

The task split requires open and click tracking to be active for Touch 1. The broader blueprint also identifies real-time open/click engagement webhooks as inputs to the Campaign Agent.

The later Dev handoff supplied in chat fixes the exact event names as:

```text
email_opened
email_clicked
```

See the “Handoff Amendments” section at the end for the exact integration requirement.

## 4.4 Successful Touch 1 acceptance criteria

- email sent from the correct warmed mailbox
- received by the test inbox
- PDF attached
- GIF visible
- tracking enabled
- dispatch event logged

---

# 5. Touch 3 — Day 4 Cold Email

## 5.1 Content / angle

Touch 3 sends Email 2: **Fee-Stack Opportunity**.

The blueprint describes the purpose as introducing ADD-8-Lite fee analysis showing uncollected ancillary revenue and partnership opportunities such as Ryse.

## 5.2 Threading requirement

Touch 3 must be threaded as a reply to Touch 1.

Required behavior from the task split Definition of Done:

- same subject relationship, with `Re:` prefix
- `In-Reply-To` header matches Touch 1's original `Message-ID`

Therefore Touch 1's `Message-ID` must be persisted somewhere accessible when Touch 3 is built.

## 5.3 Pre-send stop checks

Before Touch 3:

- re-check current compliance eligibility
- verify no opt-out
- verify no prior positive reply

The acceptance test explicitly requires manually setting `is_opted_out = TRUE` after Touch 1 and verifying Touch 3 does not dispatch.

---

# 6. Touch 5 — Day 10 Cold Email

## 6.1 Content / angle

Touch 5 sends Email 3: **Metro Speed Index & Scarcity**.

The blueprint says it references the final quarterly Speed Index publication and upcoming market territory locks.

## 6.2 Final cold touch

Touch 5 is the final cold email in the sequence.

After successful dispatch:

- write a 30-day cooling timestamp to the contact record
- prevent further cold sequence activity during the cooling period

The task Definition of Done requires verifying that the cooling timestamp is written after Touch 5.

---

# 7. Mailbox assignment, pacing, and tenant isolation

## 7.1 Assigned warmed mailbox

Each email must be sent from an assigned warmed mailbox taken from the sending pool assigned to the contact's client.

Task 3.1.1 specifically says the contact's client uses a **6-mailbox cluster** and sends are rotated across the cluster subject to daily volume limits.

## 7.2 Daily sending cap

Per mailbox:

```text
30–50 cold emails / mailbox / day
```

The acceptance test requires proving that a single mailbox never dispatches more than **50 emails in a 24-hour window**, tested using a batch of 60.

## 7.3 Blueprint sending-pool architecture

The Complete Implementation Blueprint defines:

- 20 domains / 40 mailboxes total
- Blackink internal outbound: 5 domains / 10 mailboxes
- client outbound pools: 15 domains / 30 mailboxes
- each active client: dedicated 3-domain / 6-mailbox cluster
- no sending identity or reputation pooled across clients

Blackink internal example domains:

- `growth-blackink.com`
- `connect-blackink.com`
- `audit-blackink.com`
- `pm-blackink.com`
- `scale-blackink.com`

## 7.4 Deliverability sentinel context

The broader blueprint also defines a deliverability sentinel:

- bounce rate >3% within rolling 48h -> quarantine trigger
- spam complaint rate >0.08% within rolling 48h -> quarantine trigger
- degraded domain is paused/quarantined
- system rotates to a pre-warmed reserve domain
- alert posted to `#blackink-qa`

This sentinel is broader platform behavior, but it directly affects which mailboxes/domains are valid for sequencer dispatch.

---

# 8. `outbound_touch_dispatched` event contract

Every successfully dispatched email in Task 3.1.1 must be logged to the shared `events` ledger.

Required event type:

```text
outbound_touch_dispatched
```

The Week 1 task split says each event must include full dispatch metadata, and the shared EventLogger schema defines six mandatory payload keys:

```text
touch_step
channel
recipient_email
template_version
sending_domain
mailbox_id
```

The central event logger validates required payload fields at write time. Missing/null required fields raise:

```text
MalformedEventError
```

and the event is rejected.

The central logger is the shared write path; Week 1 components must not bypass it with direct `INSERT` statements into `events`.

## 8.1 Blueprint example

The blueprint gives an example shaped like:

```json
{
  "event_type": "outbound_touch_dispatched",
  "source": "cold_outbound_sequencer",
  "payload": {
    "touch_step": 1,
    "channel": "email",
    "recipient_email": "j.smith@suncoastpm.com",
    "template_version": "v1.4_speed_audit_video",
    "ghost_shopper_speed_sec": 15120,
    "sendspark_video_id": "spk_99281a",
    "sending_domain": "growth-blackink.com",
    "mailbox_id": "mbx_04"
  }
}
```

`ghost_shopper_speed_sec` and `sendspark_video_id` appear in the blueprint example as additional fields; the six listed fields above are the mandatory schema fields defined by the shared Week 1 EventLogger contract.

## 8.2 Important source discrepancy

The later “Campaign Agent” section of the Complete Implementation Blueprint describes outbound dispatch as logging a `touch_sent` event. The Week 1 task split and the shared event logger contract explicitly require `outbound_touch_dispatched` for Task 3.1.1.

This document does **not** silently reconcile that discrepancy. For Dev 3 integration, the Week 1 task/event contract is the explicit acceptance contract for this sprint, while `touch_sent` remains a conflicting term present elsewhere in the blueprint.

---

# 9. Subtask 3.1.1 — Definition of Done

All of the following are required:

- [ ] Touch 1 dispatches from the correct warmed mailbox.
- [ ] Touch 1 arrives in a test inbox.
- [ ] Speed Loss Audit PDF is attached.
- [ ] GIF thumbnail is visible.
- [ ] Touch 3 is correctly threaded to Touch 1.
- [ ] Touch 3 subject/thread appears as a reply (`Re:` relationship).
- [ ] Touch 3 `In-Reply-To` equals Touch 1 `Message-ID`.
- [ ] Setting `is_opted_out = TRUE` between Touch 1 and Touch 3 prevents Touch 3.
- [ ] Touch 5 dispatches.
- [ ] 30-day cooling timestamp is written after Touch 5.
- [ ] No single mailbox sends more than 50 emails in 24 hours.
- [ ] 60-recipient test batch proves the cap.
- [ ] Every successful touch writes `outbound_touch_dispatched`.
- [ ] Every dispatch event contains all required payload fields.

---

# 10. Subtask 3.1.2 — Phone Task Bridge & LinkedIn Deep-Link Generator

## 10.1 Purpose

Implement the two human-assisted touches:

- Touch 2 — Day 1–2 Phone Task
- Touch 4 — Day 7 LinkedIn Deep-Link

Neither is an autonomous social/dialing bot. The goal is to equip a human setter/closer with context and action shortcuts.

---

# 11. Touch 2 — Day 1–2 Phone Task

## 11.1 Destination

Create an actionable Slack card in:

```text
#dial-tasks
```

## 11.2 Timing

The task split requires the card to appear within **60 seconds of Touch 1 dispatch**.

The blueprint describes it as Day 1–2. This creates a wording/timing tension in the source: the task's business requirement says the card appears within 60 seconds of Touch 1, while the sequence timing labels Touch 2 as Day 1–2. This should be clarified in implementation planning rather than silently changed.

## 11.3 Required Slack card fields

The card must include:

- prospect full name
- company name
- door count
- direct phone number
- local timezone
- current local time
- ghost-shopper response latency in hours/minutes
- Sendspark video watch percentage, if available

## 11.4 Calling-hours compliance indicator

The card must show whether it is currently valid to call the prospect:

- green = currently within valid calling hours
- red = outside valid calling hours

The Definition of Done specifically tests a prospect whose local time is **8 PM** and expects the indicator to be red.

The blueprint describes the governance rule as “verified direct line; local calling hours enforced.”

## 11.5 Compliance re-check

Before Touch 2 is created/executed, the system must re-check the contact's current compliance state.

If the contact is ineligible:

- skip the Touch 2 task
- log the skip

---

# 12. Touch 4 — Day 7 LinkedIn Deep-Link

## 12.1 Purpose

Touch 4 is a **manual LinkedIn handoff**, not automated LinkedIn outreach.

The blueprint labels the psychological/content angle as “Executive Peer Networking.”

## 12.2 Required behavior

Construct a LinkedIn search/profile path using:

- `first_name`
- `last_name`
- `company_name`

Generate a tailored connection note that is:

- template-based
- merge-tag populated

Provide a Slack interactive action that copies:

- the LinkedIn URL
- the connection note

into the setter's clipboard.

## 12.3 Hard prohibition

The source explicitly prohibits:

- LinkedIn scraping
- Playwright automation
- Puppeteer automation
- headless-browser LinkedIn interaction
- automated LinkedIn connection/request behavior

The task Definition of Done also requires verifying that the Touch 4 code path contains no Playwright/Puppeteer or LinkedIn API calls.

## 12.4 Event logging

Touch 4 must be logged as a manual task:

```text
event_type = 'linkedin_task_created'
```

## 12.5 Compliance re-check

Before Touch 4:

- re-check compliance eligibility
- if the contact has opted out or is otherwise ineligible, skip Touch 4
- log the skip

The acceptance test specifically covers an opt-out occurring between Touch 3 and Touch 4.

---

# 13. Subtask 3.1.2 — Definition of Done

- [ ] Touch 2 card appears in `#dial-tasks` within the required window.
- [ ] All required Touch 2 context fields are present.
- [ ] Calling-hours indicator is red for an 8 PM local-time test contact.
- [ ] Touch 4 LinkedIn URL is correctly constructed and manually verified.
- [ ] Slack clipboard action copies URL + tailored note.
- [ ] `linkedin_task_created` is written to `events`.
- [ ] No prohibited LinkedIn automation exists in the Touch 4 path.
- [ ] Opted-out contact between Touch 3 and Touch 4 causes Touch 4 to be skipped and a skip event to be logged.

---

# 14. Subtask 3.1.3 — Interim Inbound Reply Bridge & Slack Routing Cards

## 14.1 Purpose and lifetime

Deploy a temporary reply handling bridge active **September 11–16**.

It exists to cover the gap until the automated Week 2 Reply Triage Agent goes live.

The bridge must ensure that inbound prospect communication is not missed and is routed quickly to human sales operators.

---

# 15. Inbound channels

The bridge must ingest:

## 15.1 Email replies

Supported via either:

- email webhook
- IMAP polling

## 15.2 SMS replies

Supported via:

- Twilio inbound webhook

## 15.3 Latency target

Incoming replies must be ingested and surfaced within **30 seconds of receipt**.

---

# 16. Slack reply routing

Every inbound reply creates an interactive card in:

```text
#sales-replies
```

Required card context:

- prospect full name
- company domain
- door count
- full message thread history for the last 3 messages
- Sendspark video watch percentage

The blueprint describes this as the real-time inbound communication stream for prospect email/SMS responses.

---

# 17. Slack action buttons

The original Week 1 task split specifies three one-tap actions:

## 17.1 `Reply in Thread`

- opens a Slack thread
- rep composes a reply there

## 17.2 `Book Meeting`

Original task wording:

- generates a one-click Calendly booking link for the prospect

**Important:** a later client handoff supplied in chat explicitly says there is to be **no Calendly plan** and that Google Calendar / Microsoft Graph are the intended integrations, with GoHighLevel as fallback. See “Handoff Amendments.”

## 17.3 `Mark Opt-Out`

This action is deterministic and server-side.

Required behavior:

1. set:

```text
contacts.is_opted_out = TRUE
```

2. halt the contact's sequence
3. log:

```text
event_type = 'opt_out_recorded'
```

No human confirmation step is specified between the click and the deterministic opt-out write.

---

# 18. Inbound reply event logging

Every inbound reply must produce:

```text
event_type = 'inbound_reply_received'
```

Payload must include at least:

- raw message body
- detected channel

The event is required for both email and SMS test replies.

---

# 19. Slack action security

Every interactive Slack action must use cryptographic payload binding.

The Task 3.1.3 requirement says altered payloads are rejected server-side using SHA-256 payload binding.

The shared Slack security specification provides the full card-hash shape:

```text
card_hash = SHA-256(payload + recipient_id + config_state + timestamp_window)
```

Backend behavior:

- validate hash on every button interaction
- reject mismatches
- no action executes on mismatch

Shared Slack security also defines:

- cards expire after 24 hours
- expired interactions are rejected
- mismatch/expired UX message:

```text
This action has expired or was altered
```

`Mark Opt-Out` is explicitly included among the shared interactive actions that require hash verification.

---

# 20. Operational Slack channels relevant to Task 3.1

The shared Week 1 Slack hub includes:

- `#blackink-command`
- `#blackink-setter`
- `#sales-replies`
- `#dial-tasks`
- `#blackink-qa`

Direct Task 3.1 usage:

- `#dial-tasks` — Touch 2 phone task queue
- `#sales-replies` — inbound email/SMS reply bridge
- `#blackink-qa` — broader system health / failure alerts, including deliverability and data-loss conditions

The `@Blackink` router is expected to be active across all operational channels.

---

# 21. Subtask 3.1.3 — Definition of Done

- [ ] Test email reply appears in `#sales-replies` within 30 seconds.
- [ ] Test SMS reply via Twilio appears in `#sales-replies` within 30 seconds.
- [ ] Reply card contains correct prospect identity, company, door count, and recent thread history.
- [ ] Sendspark watch percentage is shown when available.
- [ ] `Mark Opt-Out` sets `is_opted_out = TRUE`.
- [ ] `Mark Opt-Out` halts the sequence.
- [ ] `opt_out_recorded` event is written.
- [ ] Altered Slack payload is rejected server-side.
- [ ] Rejected action executes no side effect.
- [ ] `inbound_reply_received` is written for every test reply with raw body and channel.

---

# 22. Conditional engaged SMS nudge — related blueprint behavior

The Complete Implementation Blueprint includes a conditional post-engagement SMS nudge in the overall Task 3.1 sequence architecture.

It is **not** a cold touch.

The blueprint says it may occur after explicit engagement such as:

- clicking an audit link
- watching a video
- replying positively to email

Governance rule:

```text
Blocked unless explicit engagement event is logged in DB.
```

The broader compliance architecture says SMS permissions unlock strictly after an inbound message or confirmed appointment, and cold SMS is blocked at database/application/CI levels.

This conditional SMS appears in the blueprint's overall sequence but is not assigned as a separate Business Requirement under subtasks 3.1.1–3.1.3 in the Week 1 task split. It should therefore be treated as related blueprint scope that needs ownership clarification rather than silently assumed into one subtask.

---

# 23. Campaign Agent architecture relevant to Task 3.1

The Complete Implementation Blueprint describes the Campaign Agent as Cora + Relay:

## Cora — language/personalization/optimization

- ingests prospect profile + ghost-shopper audit score
- drafts dynamic 5-touch sequence payloads
- injects Sendspark dynamic video parameters
- formulates copy/offer test proposals

## Relay — execution/idempotency/rate throttling

- enforces per-mailbox 30–50 sends/day
- manages domain/mailbox allocation and rotation
- uses idempotent execution controls
- halts immediately on circuit-breaker trips

## Deterministic send gate

Before outbound dispatch:

- lint DNC
- validate opt-outs
- enforce cold-email-first waterfall

## Inputs

The blueprint lists:

- enriched contact records
- ghost-shopper latency logs
- Sendspark video IDs
- real-time open/click engagement webhooks

## Outputs

- outbound emails via sending connectors
- phone tasks in `#dial-tasks`
- optimization/metrics outputs in Slack

## Integrated tool references in blueprint

The blueprint names:

- Instantly API connectors
- Sendspark dynamic video endpoints
- Mailbrevo delivery helpers
- Looker tracking models

These are blueprint-level integration references. The Week 1 task split itself does not mandate a specific email provider API for Task 3.1.1.

## Governance / autonomy bounds

The blueprint says:

- Band 1: drafts for human approval
- Band 2: approved templates may dispatch autonomously after 50 clean reviews
- Band 3: minor copy/timing optimization may execute within send caps
- hard stop: no cold outbound SMS
- hard stop: cannot modify commercial pricing terms
- hard stop: cannot bypass mailbox caps

---

# 24. Shared EventLogger behavior Task 3.1 must respect

The Week 1 shared event layer is the single application-level write path to `events`.

Relevant behavior:

- `log_event(...)` validates payload schemas
- malformed required payloads raise `MalformedEventError`
- concurrent writes must be async-safe
- failed DB writes are buffered locally, max 1,000 events
- buffered writes are retried
- events older than 1 hour in the buffer generate a data-loss alert in `#blackink-qa`
- Week 1 components should not directly `INSERT` into `events`

Task 3.1 therefore depends on this service for:

- `outbound_touch_dispatched`
- `linkedin_task_created`
- `inbound_reply_received`
- `opt_out_recorded`
- skip-related event logging where required by the task
- tracking events supplied by the later handoff (`email_opened`, `email_clicked`)

---

# 25. Metrics dependency

The shared daily Slack digest reports:

- audits completed
- average metro response latency
- cold emails dispatched
- open rate
- click-through rate
- video completion rate
- appointments booked

Task 3.1 is therefore a primary producer for:

- cold emails dispatched
- open tracking
- click tracking
- inbound replies / opt-outs that affect sequence state

The blueprint's sample event uses `channel: "email"` for outbound dispatch.

---

# 26. Source ambiguities / contradictions that must not be silently ignored

## 26.1 Event name discrepancy

- Week 1 task split + shared EventLogger: `outbound_touch_dispatched`
- Campaign Agent narrative in blueprint: `touch_sent`

Use the explicit Week 1 integration contract for acceptance unless the team formally changes it, but keep the discrepancy visible.

## 26.2 Touch 2 timing discrepancy

- Sequence timing: Day 1–2
- Subtask 3.1.2 business requirement: Slack card appears within 60 seconds of Touch 1 dispatch

This needs an explicit implementation decision/clarification.

## 26.3 Booking provider discrepancy / later client update

- Original Task 3.1.3 says `Book Meeting` generates Calendly link.
- Complete blueprint also references Calendly / Google Calendar in the Week 1 booking flow.
- Later client handoff says **no Calendly plan at all** and directs implementation toward Google Calendar + Microsoft Graph, with GoHighLevel fallback.

The later client instruction should be treated as an amendment, not silently merged into the original source wording.

---

# 27. Later Dev handoff amendments supplied in chat

The following requirements were supplied after the source documents as cross-developer integration notes. They are recorded here separately so the implementation reference is complete.

## 27.1 Exact email engagement event names

Tracking must emit exactly:

```text
email_opened
email_clicked
```

The stated reason is that the daily digest queries those event names for open-rate and click-rate metrics. Different names would cause those metrics to remain at 0% without necessarily throwing an error.

## 27.2 Exact `outbound_touch_dispatched` required keys

The handoff confirms the shared logger requires exactly these six keys:

```text
touch_step
channel
recipient_email
template_version
sending_domain
mailbox_id
```

It also states:

```text
channel = "email"
```

must be lowercase exactly because downstream SQL compares the JSON payload channel case-sensitively.

Missing a required key causes `MalformedEventError` at the call site and the event is not written.

## 27.3 No Calendly

Later client direction:

- no Calendly plan for the product
- integrate against Google Calendar
- integrate against Microsoft Graph
- GoHighLevel is fallback

For Task 3.1.3, this means the `Book Meeting` action should not be implemented as a Calendly-specific integration despite the original Week 1 task wording.

## 27.4 Booking confirmation “Log Outcome” handoff

This is primarily Task 3.2, not Task 3.1, but it is adjacent to the Task 3.1 reply/booking bridge and was supplied in the handoff.

The booking-confirmation card's `Log Outcome` button should call:

```python
from src.services.slack.listeners import open_meeting_outcome_modal

await open_meeting_outcome_modal(
    trigger_id=body["trigger_id"],
    contact_id=<contact id as string>,
    meeting_occurred_at=<meeting datetime as ISO 8601 string>,
)
```

The called handler resolves tenant ownership itself; `client_id` should not be passed.

## 27.5 No-show pause handoff

Also Task 3.2-adjacent: the later handoff says the current post-meeting modal posts a No-Show Slack notice but does not yet pause outbound. Dev 3 must wire the real sequence pause once the no-show handler exists.

---

# 28. Recommended implementation order for Task 3.1

This order is an implementation organization derived from dependency structure; it is not a separately stated client requirement.

1. Lock event contracts and shared logger integration.
2. Build/persist core sequence state and touch scheduling.
3. Implement per-touch compliance re-check.
4. Implement mailbox allocation, tenant isolation, and 24h send cap.
5. Implement Touch 1 payload, PDF/GIF/Sendspark integration.
6. Implement open/click tracking and exact engagement events.
7. Persist Touch 1 `Message-ID`.
8. Implement Touch 3 threading and prior-reply/opt-out checks.
9. Implement Touch 5 and 30-day cooling state.
10. Implement Touch 2 Slack phone card + local-time compliance display.
11. Implement Touch 4 manual LinkedIn deep-link + copy action.
12. Implement inbound email/SMS reply ingestion.
13. Implement `#sales-replies` cards and secure actions.
14. Implement deterministic `Mark Opt-Out` sequence halt.
15. Replace original Calendly-specific booking action with the amended calendar integration contract.
16. Execute all Definition-of-Done tests and cross-component event checks.

---

# 29. Consolidated acceptance checklist

## 3.1.1

- [ ] Day 0 Touch 1 scheduling works.
- [ ] Day 4 Touch 3 scheduling works.
- [ ] Day 10 Touch 5 scheduling works.
- [ ] Eligibility rechecked before every send.
- [ ] Opt-out/reply/ineligibility halts future cold touches.
- [ ] Touch 1 includes audit PDF.
- [ ] Touch 1 includes visible GIF linked to Sendspark.
- [ ] Touch 1 includes Reply-YES micro-ask.
- [ ] Open/click tracking active.
- [ ] Exact tracking events integrated per handoff.
- [ ] Touch 1 `Message-ID` retained.
- [ ] Touch 3 threaded using `In-Reply-To`.
- [ ] Touch 3 skipped after opt-out/positive reply.
- [ ] Touch 5 final cold email sent.
- [ ] 30-day cooling timestamp written.
- [ ] Assigned warmed mailbox used.
- [ ] 6-mailbox cluster rotation works.
- [ ] 30–50 daily pacing respected.
- [ ] Absolute test cap of 50/mailbox/24h passes.
- [ ] `outbound_touch_dispatched` written for every successful email.
- [ ] All six required payload fields present.
- [ ] `channel` exactly `email` per handoff.

## 3.1.2

- [ ] Touch 2 Slack card created in `#dial-tasks`.
- [ ] Prospect full name shown.
- [ ] Company name shown.
- [ ] Door count shown.
- [ ] Direct phone shown.
- [ ] Local timezone shown.
- [ ] Current local time shown.
- [ ] Ghost-shopper latency shown in hours/minutes.
- [ ] Sendspark watch % shown if available.
- [ ] Calling-hours compliance indicator works.
- [ ] 8 PM test shows red.
- [ ] Compliance re-check before Touch 2.
- [ ] Touch 4 LinkedIn search/profile URL generated.
- [ ] Tailored merge-tag connection note generated.
- [ ] Copy action copies URL + note.
- [ ] `linkedin_task_created` event logged.
- [ ] No LinkedIn automation/scraping/browser bot/API path exists.
- [ ] Compliance re-check before Touch 4.
- [ ] Opted-out test skips Touch 4 and logs skip.

## 3.1.3

- [ ] Interim bridge active for Sept 11–16 period.
- [ ] Email reply webhook/IMAP ingestion works.
- [ ] Twilio inbound SMS webhook works.
- [ ] Reply surfaced within 30 seconds.
- [ ] `#sales-replies` card created.
- [ ] Card shows prospect name.
- [ ] Card shows company domain.
- [ ] Card shows door count.
- [ ] Card shows last 3 messages.
- [ ] Card shows Sendspark watch %.
- [ ] `Reply in Thread` action works.
- [ ] `Book Meeting` follows amended calendar integration direction.
- [ ] `Mark Opt-Out` updates DB deterministically.
- [ ] `Mark Opt-Out` halts sequence.
- [ ] `opt_out_recorded` event logged.
- [ ] `inbound_reply_received` event logged with raw body + channel.
- [ ] SHA-256 payload-bound action validation works.
- [ ] Altered action payload rejected.
- [ ] Rejected action creates no side effect.
- [ ] 24h card-expiry behavior honored by shared Slack security layer.

---

# 30. Source references used

1. **Week1_Tasks_Dev_Split.md**
   - Developer 3, Task 3.1 and Subtasks 3.1.1–3.1.3
   - Shared EventLogger requirements
   - Shared Slack SHA-256 security and channel configuration

2. **Project Blackink — Complete Implementation Blueprint**
   - Week 1 campaign/demo architecture
   - Ghost-shopper / PDF / Sendspark dependency chain
   - Multi-Touch Outbound Sequencer & Human Bridge table
   - Interim Reply Bridge description
   - Day-One event example
   - Campaign Agent Cora/Relay architecture
   - tenant-isolated sending pool and deliverability caps
   - Week 1 milestone requirements

3. **Dev 3 handoff notes supplied in chat**
   - exact `email_opened` / `email_clicked` event names
   - exact outbound payload key/casing constraints
   - no-Calendly amendment
   - post-meeting outcome/no-show integration notes

