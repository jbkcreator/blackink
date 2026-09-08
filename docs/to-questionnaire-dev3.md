# Blackink Week 1 — Outbound Sequencer & Reply Bridge: decisions we need from you

**Purpose:** The multi-touch outbound sequencer, the phone/LinkedIn task cards, and the interim inbound reply bridge (Dev 3, Task 3.1) are built and tested. Before we point them at real prospects, a set of decisions can only be made by you — they're about *your* copy, *your* compliance posture, *your* calendars, and *your* mailboxes. This document collects them in one place so we can turn the system on without guessing.

**From:** HEU (Blackink engineering, Dev 3) · **To:** the client owner for outreach, compliance, and calendars · **How your answers will be used:** each answer unblocks a specific piece of the September build; several are hard gates before any real send.

## Context

Blackink runs a 5-touch outreach sequence per prospect: three cold emails (Days 0/4/10), a phone-call task (Day 1), and a LinkedIn connection task (Day 7). Every email is drafted into a Slack card and **only sends after a human clicks Approve**. When a prospect replies, an interim bridge posts the reply into a Slack channel as a card with one-tap actions (reply, opt-out) until our automated reply agent ships in Week 2. The engine is done; the questions below are the human decisions, content, and accounts it needs to run against live prospects.

## How to answer

Please return within **3 business days** — most items are a one-line choice; the copy items may take longer. Partial answers and "I don't know / not sure yet" are genuinely useful — flag anything uncertain rather than skipping it, so we know what's still open versus decided.

## 1. Email copy & who we mail

### For a firm that ranks too low to show a public rank, do we still email it — and with what hook?

_Why this matters: rank is only published for the top 25 per county, so most prospects have no public rank. We need a fallback before Touch 1 can go to them._

Our proposed rule: a firm outside the top 25 still gets emailed, but the copy drops the rank claim and leads with the firm's Owner Visibility Score and its weakest categories instead ("Reply YES for your Owner Visibility breakdown for {county}"). A firm with too little public data to score at all (fewer than 3 signals) is **held — not mailed** — because there's no credible hook. Confirm yes/no, and adjust if you'd rather.

>

### Will you supply the approved words for the three cold emails (Touches 1, 3, 5), or approve our template?

_Why this matters: nothing sends until copy is locked; this is the most common blocker._

We can ship a merge-tag template (e.g. "Hi {first_name}, here's the owner-visibility picture for {firm} in {county}…") for your approval, or you hand us final copy. Which, and by when?

>

### What should the LinkedIn (Touch 4) connection note say?

We ship a ≤300-character merge-tag placeholder. Give us your preferred note, or approve ours.

>

### When a setter finishes a Touch 2 call, should the card capture an outcome, or just "Done"?

Options: plain "Mark Called", or a dropdown (Connected / Voicemail / No answer). We default to plain "Done" for September unless you want the richer capture.

>

## 2. Timing & compliance windows

### What are the valid **calling** hours, and is 8 PM inside or outside them?

_Why this matters: your spec says "red outside 8 AM–9 PM" but its own test says 8 PM should show red. Those conflict. A calling-hours indicator that's wrong in the permissive direction is the dangerous kind of wrong._

We currently ship a conservative window (8 AM–8 PM recipient-local, 8 PM = red). Tell us the real window and whether any state-by-state variation applies.

>

### What are the valid **email-sending** hours — whose timezone, and which days/holidays?

_Why this matters: combined with the day-based schedule, "business hours only" quietly turns into business-day behavior; we'd rather you choose the model outright._

Recipient-local, your local, or Eastern? Exclude weekends? Any holiday calendar? (All September launch counties are Eastern, so this can start simple.)

>

### Is the 14-day "cooldown" between touches a real compliance commitment, a deliverability rule of thumb, or a placeholder?

_Why this matters: taken literally it blocks Touch 3 (Day 4) and Touch 5 (Day 10), making the sequence impossible — we must not weaken a real commitment silently, so we need to know what it's protecting._

>

## 3. Inbound replies

### How should we catch prospect replies — mailbox polling (IMAP) or a parsing webhook? Which suits you?

_Why we need this: our cold emails explicitly ask the prospect to reply ("Reply YES…"). When they do, a rep must see that reply in Slack within ~30 seconds and act on it. Email arrives over one protocol (mail); our app runs on another (web), so something has to bridge a reply email into our system. This bridge is the one piece we can't finish without your decision — everything downstream (attribution, the Slack reply card, opt-out) is already built and tested and works the same either way._

**What we've already built:** the full receive → attribute-to-prospect → Slack card → opt-out path is done and proven end-to-end. It's written to be transport-agnostic, so switching between the two options below is a small change on our side, not a rebuild. Replies land on a dedicated Blackink subdomain address (e.g. `leads@{your-subdomain}.getblackink.com`), kept separate from the mailboxes we send from.

**The two ways to bridge, and what each needs from you:**

- **(a) IMAP polling** — we log into the reply mailbox every ~30 seconds and pull new mail. *You give us:* mailbox host, username, and an app-password. *Pros:* you own the mailbox, no third-party parsing vendor, no DNS change if the mailbox already exists. *Con:* up to ~30s slower than a webhook (fine for this interim bridge).
- **(b) Hosted inbound-parse webhook** — a provider (Mailgun / Postmark / Amazon SES) receives the mail and instantly posts it to us. *You give us / set up:* a provider account and an MX/DNS change pointing the subdomain at that provider. *Pro:* near-instant (<5s). *Con:* adds a vendor account + a DNS change.

**Our lean is (a) IMAP polling** — no vendor, no DNS change, and 30s is well within the target. Which fits your setup, and can you provide the matching access (mailbox credentials for IMAP, or the provider + DNS owner for the webhook)?

>

### When a rep uses "Reply in Thread" on a reply card this September, is it OK that it records the draft in Slack only and does **not** auto-email the prospect yet?

_Why this matters: actual outbound reply-sending is the Week-2 Reply Triage Agent; we want to confirm the interim behavior is acceptable so no one expects the prospect to receive it._

>

## 4. Booking (meeting scheduling)

### Which calendar do your prospects book into first — Google Calendar, Microsoft, or GoHighLevel?

_Why this matters: supporting all three doubles the integration surface; we'd build the one you actually use first._

Also: is GoHighLevel the fallback for an outage, for clients on neither Google nor Microsoft, or for clients already on GHL?

>

### For the "Book Meeting" button on a reply card, what booking link should it hand the prospect?

_Why this matters: the booking engine can *receive* a booking, but nothing yet *generates* the link to send. We need a per-client scheduling URL (a GHL booking page or a Google appointment-schedule link) to put on the button._

Do you have a hosted booking page URL we can store per client, and does the prospect self-serve a time, or does a rep pick from their own free slots?

>

## 5. Interim-scope confirmations

### Confirm inbound **SMS** (Telnyx) capture is out of scope for September.

We built the reply bridge email-only. Your docs mention Telnyx SMS as "future". Confirm we defer it.

>

### The "Mark Opt-Out" button on reply cards is intentionally always-actionable (it never expires) and is not cryptographically payload-bound like the other cards. Is that acceptable?

_Why this matters: your spec asks for SHA-256 payload binding on all buttons, but opt-out is the one action that must still work on an old card — so we deliberately left it non-expiring. Flag if you'd rather it expire like the rest._

>

---

## HEU-internal (client can skip this section)

Items below are for the HEU operations/onboarding owner, not the client — provisioning and vendor decisions we resolve on our side.

### Do the sending mailbox seats exist, who pays, and can we get SMTP credentials?

Real cold sends need warmed Google Workspace / Outlook seats on the client's own SPF/DKIM/DMARC-verified domains, with app passwords. The Mandrill / `forcedactionleads.com` credentials used in testing belong to the parent project and must not carry client volume. Confirm seat count (up to ~34), payer, and how we obtain credentials.

>

### Are the compliance vendors contracted, or still stubbed?

Before any real prospect send: a live DNC scrub (Tracerfy — needs `DNC_VENDOR_API_KEY`) and email verification (needs a vendor + key). Both are stubbed today — fine for the demo against test data, a hard gate before real volume.

>

### For the chosen inbound transport, who owns the account and the DNS?

If IMAP: which mailbox and where hosted. If webhook: which provider account, who pays, and who makes the MX/DNS change on the `getblackink.com` subdomain.

>

### Which booking integrations need account setup — Google OAuth app, Microsoft Graph registration, GoHighLevel?

Track which of A8 (Google OAuth), A9 (MS Graph app), A10 (GHL) must exist before the booking engine can connect, and who owns each.

>

## Anything else?

Anything about your outreach, compliance posture, calendars, or mailboxes we didn't ask that we should know before pointing this at real prospects?

>
