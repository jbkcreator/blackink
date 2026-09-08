# Non-email touches use a split posting model: event-driven dial, sweep-driven LinkedIn

The 5-touch sequence mixes email touches (1, 3, 5) with a phone-call touch (2, `DIAL_TASK`) and a LinkedIn touch (4, `LINKEDIN_TASK`). Email touches are dispatched by `sequence_sweep` on their day-grain `due_at`. For the two manual touches we post the Slack task card by two *different* mechanisms, chosen per the SLA each touch needs: the dial card is posted **event-driven**, the instant Touch 1 is approved (`_finalize_terminal_decision` → `_post_dial_task_after_touch1_approval`), to meet the ~60s call-while-hot SLA; the LinkedIn card is posted **by the day-grain sweep** on its day-7 `due_at`, gated through `evaluate_touch_gate` (skip + log `touch_skipped_compliance` on non-pass), because it has no urgency and belongs on the same day-grain cadence as the emails.

## Consequences

- `enroll_contact` no longer enqueues a touch-2 `DIAL_TASK` at enrollment time. The dial work order is created only by the event-driven post (keyed `seq:{run_id}:touch:2`), so exactly one exists per run. Enqueuing it upfront would have created an orphan the sweep never posts (the sweep filters email touches, and now also `LINKEDIN_TASK` — never `DIAL_TASK`).
- The two mechanisms are deliberately asymmetric. A future reader extending the sweep to "handle all touches uniformly" would reintroduce the orphaned dial order and lose the 60s SLA — do not collapse them without replacing the SLA guarantee.
- Opt-out is enforced differently per touch: LinkedIn is gated at post time by `evaluate_touch_gate`; the dial inherits Touch 1's approval-time gate, and a later opt-out CANCELs its work order via the halt path.
