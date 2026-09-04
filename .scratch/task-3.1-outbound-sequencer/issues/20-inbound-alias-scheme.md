# Define the inbound alias scheme and reply return path

Label: `wayfinder:grilling`
Status: open
Assignee:
Blocked by: —

## Partial resolution (2026-09-03) — client "Lead agent + Part 14" comment

The client supplied the **forwarding/return-path model** (Part 14 "The
forwarding alias"). It resolves four of the six sub-questions; the ticket
stays **open** on the two it does not (the alias string scheme, and
attribution). What's settled:

- **(2) Which domain receives** — a **delegated subdomain**, deliberately
  separate from the bulk cold-outreach pool ("Respond's simpler delegated
  sending setup must not be confused with the bulk cold-outreach pool"). One
  DNS record at onboarding.
- **(3) Forwarding verification** — a **two-minute client mail-forwarding
  rule** + one DNS record at onboarding; onboarding asks the client to point
  one or more source addresses at the alias. (The *proof* step — how we
  confirm the forward works before go-live — is the Link 3 acceptance test the
  client now explicitly wants; see below.)
- **(4) The loop risk** — **confirmed real and load-bearing.** The client's
  own rule is: send from the delegated subdomain, **Reply-To = client's own
  address**, **BCC the client on every send**. Because the client also forwards
  their inbox to us, our BCC/reply can echo back into the alias — so the
  dedup/loop-break is mandatory, not optional. Dedup key = Message-ID +
  direction; reject any inbound whose Message-ID matches one of our own sends.
- **(5) Thread history** — **confirmed: from first forwarded contact, no
  backfill.** "We build thread history from first contact forward." A first
  contact has exactly one message; the card degrades gracefully. Feeds
  ticket 28.

Also newly explicit — the client wants two **acceptance tests** that were
skipped as "no build":
- **Link 3** — a reply to a real campaign email reaches a human, is **recorded
  against the originating firm**, and is answered inside SLA. This is the whole
  point of the 3.1.3 bridge and makes sub-question (6) attribution testable.
- **Link 4** — a signature produces a retrievable acceptance record
  (agreement_version · accepted_at · accepted_by_email · ip · hash). This is
  Task 3.2+/contract, **out of Dev 3 Week 1** — noted only so it isn't lost.

**Still open (why this stays a blocker):**

- **(1) The alias string scheme** — the actual per-client address format,
  uniqueness/collision policy, and offboarding lifecycle (O-18: a client-owned
  forward can't be remotely revoked, so the alias must be killable on our
  side). The client gave the *mechanism*, not the *naming convention*.
- **(6) Attribution** — how a forwarded message (arriving from the client's
  system, not the prospect's) recovers the originating firm + contact. The
  forwarded envelope, the alias it arrived on, or body parsing? Link 3 makes
  this testable but the client comment doesn't specify the recovery method.

## Question

Subtask 3.1.3 cannot be specified until this is answered, and the Source of
Truth explicitly flags it as a **missing input** rather than resolving it.

The ingestion model changed. The Dev 3 reference offers "email webhook **or
IMAP polling**" (§15.1). The Source of Truth forbids the mailbox route:

> "**Do not build Gmail or Microsoft Graph mailbox access.** Calendar OAuth
> remains. **Clients forward** their owner-inquiry address to a Blackink
> address. More than one source address may forward to the alias. No general
> mailbox read permission is held." — §1.6

Two decision-register entries name what is missing:

- **O-05** — "Per-client inbound alias/domain/collision rules: need actual
  scheme, uniqueness, ownership, lifecycle and forwarding verification."
- **O-06** — "Reply-To client address versus persistent automated thread
  handling: prove subsequent replies are forwarded back once; avoid BCC loops
  and duplicate ingestion."

Settle:

1. **The alias scheme.** What does a Blackink inbound address look like per
   client? Uniqueness and collision policy when two clients want the same
   handle. Lifecycle on offboarding — §1.6 and **O-18** note a client-owned
   forwarding rule "cannot be remotely revoked automatically", so the alias
   must be killable on our side even when their forward persists.
2. **Which domain receives?** Not a cold-outreach domain — mixing inbound
   client mail with bulk outbound reputation is exactly what §2.2 warns
   against ("Respond's simpler delegated sending setup must not be confused
   with the bulk cold-outreach pool").
3. **Forwarding verification at onboarding.** §1.6 calls for a DNS setup step
   and Launch state 13 covers routing; how do we *prove* the forward works
   before going live?
4. **The loop risk (O-06).** §1.6's Respond sending rule is: delegated
   subdomain, **Reply-To set to the client's own address**, and **BCC the
   client on every send**. If the client also forwards their inbound address
   to us, a BCC or a reply can land back in our alias and be re-ingested as a
   new inbound message. Design the de-duplication and loop-break explicitly —
   this is the kind of bug that only appears in production and then floods.
5. **Thread history.** §1.6: "Build thread history from the first forwarded
   contact; whole-inbox coverage and preexisting history are not available."
   The 3.1.3 card requires the "last 3 messages" (§16) — so for a first
   contact there is exactly one, and there is no backfill. Confirm the card
   degrades gracefully rather than looking broken.
6. **Attribution.** A forwarded message arrives from the *client's* system,
   not the prospect's. How do we recover the originating firm and contact —
   the forwarded envelope, the alias it arrived on, or parsing the body?
   Link 3 of the acceptance contract requires the reply be "recorded against
   the originating firm", so this is testable and load-bearing.

## Scope note

This is scoped to **cold-sequence reply handling** for Task 3.1.3. The wider
Respond product shares the mechanism, so coordinate — do not design a
sequencer-only alias scheme that Respond then has to replace.
