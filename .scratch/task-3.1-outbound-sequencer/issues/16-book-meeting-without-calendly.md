# Respecify the `Book Meeting` action without Calendly

Label: `wayfinder:grilling`
Status: open
Assignee:
Blocked by: —

## Question

3.1.3's original wording (§17.2) says the `Book Meeting` Slack button
"generates a one-click Calendly booking link". A later client handoff
(§27.3, §26.3) says there is **no Calendly plan at all** — integrate against
Google Calendar and Microsoft Graph, with GoHighLevel as fallback.

The amendment wins. But "generate a Calendly link" and "integrate with
Google Calendar + Microsoft Graph" are not the same shape of thing, and
swapping one for the other is not a substitution — Calendly *is* a booking
page; Google Calendar is an API with no built-in prospect-facing scheduling
UI.

So: what does the button actually do now?

- **Post a self-service booking link** — which requires something to host a
  booking page and own availability logic. Does GoHighLevel provide that, in
  which case is it really the *fallback* or the primary?
- **Create a calendar event directly** and email an invite — no prospect
  choice of time, so the rep must pick, which changes the interaction from
  one-click to a modal.
- **Open a Slack modal** showing the rep's free slots read from Google/Graph,
  rep picks, invite sent. Most faithful to "one-tap" while keeping the
  amendment, and the repo already has `open_modal`.

Settle also:

- **Which calendar does it read** — the rep's, a shared team calendar, or a
  round-robin pool? Multi-tenant: whose Google/Microsoft tenant, and how is
  OAuth stored per client?
- **Google *and* Microsoft, or one first?** Supporting both doubles the auth
  surface. Is there a known split across the customer base, or is this
  speculative generality?
- **What is GoHighLevel actually the fallback *for*** — a provider outage, a
  client with neither Google nor Microsoft, or clients already on GHL?
- Is any of this **actually 3.1.3's**, or does the button just hand off to
  Task 3.2's booking engine? §27.4/§27.5 put booking confirmation and
  no-show handling in 3.2. If 3.2 owns booking, 3.1.3's button may only need
  to *invoke* it — which would shrink this ticket dramatically and is worth
  checking first.

Check that last point before doing anything else here.

## Update from Dev 4's handoff (2026-09-03)

Two useful confirmations.

**The no-Calendly direction has a citable source** — Week 1 Open Item #8:
"we need no Calendly plan for the product at all… Integrate against Google
Calendar and Microsoft Graph; GoHighLevel is the fallback." Matches Source of
Truth §1.5 and §2.4 state 6.

**And the downstream integration is calendar-agnostic**, which narrows this
ticket usefully. Dev 4: "Whichever calendar produces your booking, the modal
doesn't care — it only needs `contact_id` + a timestamp." So the choice of
Google vs Graph vs GHL is **purely a booking-side decision**; nothing
downstream constrains it. That removes one axis from the question.

Note the post-booking handoff (`open_meeting_outcome_modal`) is **Task 3.2**,
not this ticket — see the map's Out of scope section, which now records the
handoff specifics.
