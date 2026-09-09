# Glossary

Canonical domain vocabulary. Glossary only — no implementation detail.

## Speed-to-Lead lead
An inbound inquiry from a renter or property owner (Task 4.2.1). NOT a
PM-firm prospect. Lives entirely on `inbound_messages` (prospect name/email/
phone inline); has no `companies` or `contacts` row, and therefore no
`contact_id`. Its durable identity is `inbound_messages.id`.

## Speed-to-Lead cadence
The six-attempt follow-up (Task 4.2.2): one 60-second acknowledgement
(the 4.2.1 auto-response) plus, if the lead goes quiet for 24h, five daily
follow-up touches (Day 1–5). A message-anchored cadence — keyed on
`inbound_messages.id`, never a `contact_id` (contrast the outbound Cold
Sequencer, which keys on `contacts.contact_id`). The Win-Back sequencer
(keyed on `winback_row_id`) is the precedent for a non-contact cadence.

## Cadence arm
The act of scheduling the five follow-up touches for a lead. Happens only
when, 24h after `inbound_lead_received`, no reply / booking / opt-out has
occurred for that lead.

## Early client phase
A per-client lifecycle state (v2 blueprint) during which every Respond /
Speed-to-Lead send — including cadence touches — requires human approval
before dispatch. When a client graduates out of early phase, cadence
touches auto-send. Governs the cadence's autonomy: approval-card vs.
auto-send. (The Week2 task doc's "no approval for cadence touches" is
overridden by v2, which is the client source of truth.)

## Cadence stop
Termination of an armed cadence on any of: an inbound reply (any
sentiment), a booking, or an opt-out. Recorded two ways for a
message-anchored lead: a stop **event** (audit trail) plus a **latch** —
`stopped_at` / `stop_reason` columns on `inbound_messages` (mirrors the
win-back stop-column pattern) — which the cadence sweep checks cheaply each
tick.
