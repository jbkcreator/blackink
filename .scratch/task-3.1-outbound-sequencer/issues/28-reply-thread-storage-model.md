# Reply-thread storage model — where do inbound replies and their thread history live?

Label: `wayfinder:grilling`
Status: open
Assignee:
Blocked by: 20 (partial — see note)

## Update (2026-09-03) — narrowed by the client "Part 14" comment

Ticket 20's partial resolution settled the ingestion *shape* this ticket
waited on, so the storage schema is now largely specifiable. Confirmed:

- **No backfill, thread from first forwarded contact** — "last 3 messages" is a
  **max, not a guarantee**; a first contact has exactly one message and the
  card must degrade gracefully. (Was a leaning here; now client-confirmed.)
- **BCC-loop dedup is mandatory** — the client BCCs itself on every send *and*
  forwards its inbox to us, so our own outbound can echo back as a fake
  "inbound". The dedup key **Message-ID + direction** must reject any inbound
  whose Message-ID matches one of our `sequence_touch_dispatches.message_id`
  rows. This is now a hard requirement, not a maybe.
- **Return-path headers** — replies carry `In-Reply-To` / `References` pointing
  at our outbound `sequence_touch_dispatches.message_id`; that's the join that
  attaches an inbound reply to a run + touch.

**Still blocked on ticket 20** for one field only: **attribution** (20 pt 6) —
how a forwarded message recovers `contact_id` (forwarded envelope vs. alias it
arrived on vs. body parse). The rest of the schema can be drafted now; the
`contact_id` resolution can't be finalized until 20 settles attribution.

## Question

3.1.3's reply card must show "full message thread history (**last 3
messages**)" (v2 3.1.3). No table in the schema holds a conversation thread —
`events` is append-only telemetry, not a message store. This ticket decides
the storage model for inbound replies and the thread they belong to.

Blocked by ticket 20 (alias scheme) because the **ingestion shape** decides
what a "message" record even contains: §1.6 forbids mailbox access, so replies
arrive as **client-forwarded email** to a Blackink alias — the parsing,
dedup-key, and source-address handling all hang on ticket 20's forwarding
model. But the *storage schema* can be sketched now against that constraint.

### 1. New table vs reuse

- A new **`inbound_messages`** (or `reply_messages`) table: `message_id`,
  `client_id`, `contact_id`, `run_id` (nullable — a reply may arrive against a
  contact with no active run), `direction` (inbound; outbound sends already
  live in `sequence_touch_dispatches`), `channel` (email — SMS is cancelled),
  `raw_body`, `from_address`, `received_at`, `in_reply_to` (RFC Message-ID for
  threading back to our Touch send). Tenant-bearing → **must register in
  `TENANT_POLICIES` and push through `apply_rls_policies.py`** (CLAUDE.md
  invariant).
- Threading key: our outbound `sequence_touch_dispatches.message_id` is the
  anchor. An inbound reply's `In-Reply-To` / `References` header points back at
  it — that's how we attach a reply to a run and a touch. Confirm the join.

### 2. "Last 3 messages" with no backfill

§1.6: thread history starts at the **first forwarded contact**, no backfill.
So a first reply has exactly **one** message (the prospect's), and the card
must **degrade gracefully** — "last 3" is a max, not a guarantee. Do we also
store our own **outbound** touch bodies in the same table so the thread shows
both sides (our Touch 1 + their reply), or does the card reconstruct the
outbound side from `sequence_touch_dispatches` + the composed copy?

- Leaning: store inbound only; reconstruct the outbound side by joining
  `sequence_touch_dispatches` for the same run — avoids duplicating send copy
  we already have. But the composed body isn't currently persisted (3.1.1
  composes at send time and keeps only the Message-ID). If the card needs to
  show what we said, either persist the outbound body on send, or the card
  shows outbound as "Touch N sent <date>" metadata only. Decide.

### 3. Opt-out and dedup

- `Mark Opt-Out` (ticket 27) lives on this card. The card's identity (its
  `slack_message_ts` and payload hash) must bind to a `contact_id` so the
  opt-out button targets the right person.
- **BCC loop / duplicate ingestion** (O-06, ticket 20): if we BCC the client
  and the client forwards their inbox back to us, our own send can echo back
  as an "inbound" — the dedup key (Message-ID + direction) must reject it.
  This is why 28 waits on 20's return-path decision.

## Why it matters

The reply bridge is due **next week** (Sep 11–16, D7) and is the interim
human-in-the-loop layer before the Week-2 Reply Triage Agent. Getting the
storage model right now means the triage agent later reads a real thread store
instead of scraping Slack. But it genuinely can't finalize until ticket 20
settles how replies arrive — hence blocked, not frontier.
