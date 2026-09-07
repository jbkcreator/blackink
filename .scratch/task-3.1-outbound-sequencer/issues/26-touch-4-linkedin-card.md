# Touch 4 LinkedIn deep-link card — URL construction, note template, and the "clipboard" affordance

Label: `wayfinder:prototype`
Status: closed
Assignee:
Blocked by: 29 (closed)

## Resolution (2026-09-03) — decided by user directive "go with your leans"

Ticket 29's research settled the affordance question, so this closes on the
recommended shape rather than a live prototype iteration:

1. **Card affordance (no fake clipboard button):**
   - **LinkedIn link** → a `url`-type Block Kit button ("Open LinkedIn
     search") — one click to the browser, no backend round-trip.
   - **Connection note** → rendered inside a triple-backtick / code block so
     desktop setters get Slack's native hover copy-icon for free.
   - **Mobile fallback** → a "Copy note" button that opens a modal with a
     `plain_text_input` prefilled (`initial_value`) with the note, selectable
     on every client.
   The v2 DoD line "clipboard copy button confirmed working in browser" is
   satisfied by the `url` button + desktop code-block copy; the literal "copies
   to clipboard" is reinterpreted (it was never buildable — see ticket 29).

2. **URL construction: prefer stored profile, fall back to people-search.**
   If `contacts.linkedin_url` is populated (ingestion supplied it), deep-link
   straight to it. Else build a people-search URL:
   `https://www.linkedin.com/search/results/people/?keywords=<first last company>`
   (URL-encoded). Honest given we don't hold a verified profile per contact.

3. **Note template: placeholder for September, C7 owed.** The client-approved
   connection-note copy (asset C7) is still not supplied. Ship a **score-free**
   merge template for September (Dev 2's OVS may not be ready, and a LinkedIn
   note capped at **300 chars** has no room to justify a score anyway). Merge
   tags: first name, company, county. Flag C7 as needed-soon; swap the copy in
   when it lands — no code change, just the template string.

4. **Trigger + compliance: day-grain sweep, reuse the per-touch gate.** Touch 4
   is Day 7 with **no** 60-second SLA (unlike Touch 2), so the existing
   day-grain sweep posts it when the `LINKEDIN_TASK` order comes due — simpler
   than Touch 2's event path. Re-check compliance with `evaluate_touch_gate`
   before posting; an opt-out between Touch 3 and Touch 4 **skips** and logs
   `touch_skipped_compliance`.

**Nothing left to decide before building Touch 4** — a `LINKEDIN_TASK`
dispatcher (sweep-posted), the card JSON above, and the modal handler for the
mobile "Copy note" fallback.

## Question

`enroll_contact` already enqueues Touch 4 as a `LINKEDIN_TASK` work order
(Day 7). Like `DIAL_TASK`, nothing dispatches it yet. v2 3.1.2 requires: a
LinkedIn search URL built from `first_name`, `last_name`, `company_name`, a
merge-tag connection note, both delivered to the setter "via a Slack
interactive button" — and **no** Playwright/Puppeteer/LinkedIn-API in the
code path (DoD asserts this via code review).

### 1. The "copy to clipboard" affordance — the hard part

The map already flagged this as fog: **Slack has no native copy-to-clipboard
button.** A button click is a backend round-trip; it cannot write the user's
OS clipboard. Ticket 29 (research) resolves what Slack *does* offer. Candidate
patterns to react to once 29 lands:

- Put the URL and note in a **`rich_text` / code block** that renders a native
  copy icon on hover (desktop) — no button at all.
- A **`url`-type button** that opens the LinkedIn search directly in the
  browser (no round-trip), plus the note in a copyable block beside it.
- A **modal** with a `plain_text_input` prefilled with the note text, so it's
  selectable/copyable.

This is a **prototype** ticket: build the Block Kit JSON for the card, post it
to the test workspace, and have the human react to which affordance actually
feels one-tap. The DoD's "clipboard copy button confirmed working in browser"
is the acceptance line to satisfy or renegotiate.

### 2. LinkedIn URL construction

- Search URL vs profile URL: v2 says "constructs the prospect's LinkedIn
  **search** URL" (DoD) but also "the prospect's LinkedIn profile URL"
  (description). We don't hold a verified profile URL per contact, so a
  **people-search** URL keyed on name + company is the honest construction:
  `https://www.linkedin.com/search/results/people/?keywords=<first last company>`.
  Confirm search-not-profile; profile would need a data source we lack.
- `contacts.linkedin_url` **exists** (schema, subtask 1.1.1) but is populated
  only when ingestion supplied it. If present, deep-link to it; else fall back
  to the search URL. Confirm this fallback.

### 3. Connection-note template

Content asset **C7** (LinkedIn connection-note template) is still owed — 🟡 in
CLIENT-ASKS. Merge tags available on the contact: first name, company, county,
door count, Owner Visibility Score (once Dev 2 ships). LinkedIn connection
notes cap at **300 characters** — the template must fit. Who writes the copy,
and does it merge the OVS (Dev 2 dependency) or stay score-free?

### 4. Trigger + compliance re-check

- Same trigger question as ticket 25: automatic on due (Day 7 sweep) vs
  event-driven. Day 7 is not latency-sensitive (no 60s SLA here), so the
  **day-grain sweep** likely suffices — simpler than Touch 2.
- Compliance re-check before posting (§11.5): an opt-out between Touch 3 and
  Touch 4 must **skip** and log `touch_skipped_compliance`. The per-touch gate
  (`evaluate_touch_gate`) already exists from 3.1.1 — reuse it.

## Why it matters

Touch 4 is small in logic but the clipboard requirement is the one genuinely
uncertain UX in Dev 3 — worth a prototype rather than guessing and shipping a
button that does nothing. Blocked on ticket 29's research into what Slack
actually supports.
