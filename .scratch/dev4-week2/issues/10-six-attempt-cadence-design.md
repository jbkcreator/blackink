# Design the six-attempt inbound cadence

Type: grilling
Status: open
Blocked by: 05, 09

## Question

After the 60s ack, if the owner is quiet 24h (no reply / no `meeting_booked` / not opted
out), arm 5 daily follow-up email touches (Day 1–5), stopping instantly on any reply,
booking, or opt-out. No human approval (follow-up to inbound, not cold outbound). Same
orchestrator code path as the Speed-to-Lead response.

Decide (on the engine chosen in ticket 9): the arm trigger (24h-quiet detection substrate),
the per-touch schedule and `touch_step` (1–5), the exact stop conditions and how a stop
cancels queued touches, the `outbound_touch_dispatched` event with
`campaign_type = 'SPEED_TO_LEAD_CADENCE'`, and where the sequence/config ID lives (config
row, not hardcoded). Email only — no SMS. Output: the cadence state + stop spec.
