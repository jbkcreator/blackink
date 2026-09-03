# Who builds the shared EventLogger, and what is in its first version?

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03)

**Do not build it. Dev 4 already has.** Answered by evidence, not by asking:
`origin/feature/week1-metrics-and-sandbox` (7 commits ahead of main)
contains `src/services/events.py` and `tests/test_events.py`. No other branch
has any `log_event` or `MalformedEventError`.

Dev 3 **integrates**; it does not build, and it must not add a second write
path. Dev 4's module already refactored the two pre-existing raw-SQL writers
(`slack/listeners.py`, `promotion_sweep.py`) onto itself for exactly that
reason.

### The contract

```python
log_event(
    client_id, event_type, *,
    entity_type, entity_id, payload,
    actor=None, session=None,
) -> None
```

- `MalformedEventError` on missing required keys; event **not written**.
- `REQUIRED_PAYLOAD_FIELDS` **already registers `outbound_touch_dispatched`**
  with exactly the six keys Task 3.1.1 needs — `touch_step`, `channel`,
  `recipient_email`, `template_version`, `sending_domain`, `mailbox_id`.
- On DB failure it **buffers and swallows** — deliberately, so logging can
  never fail a caller's business logic.
- `flush_pending()` drains the buffer; no background thread.

### Four gaps Task 3.1 must work around

1. **The buffer is process-local and lost on restart.** `_pending_buffer` is a
   module-level list. A DB blip during dispatch buffers events in the
   *sequencer's* process; a restart loses them — **but the emails were already
   sent**. Real sends, no record.
2. **`flush_pending()` only runs from `daily_digest.py::main()`**, a different
   process, and a process can only flush its own buffer. **The sequencer's
   worker loop must call `flush_pending()` itself** or its buffer never
   drains.
3. **Buffered events lose their occurrence time.** `created_at` is set by the
   DB at insert, so an event dispatched at 10:00 and flushed at 14:00 records
   14:00.
4. **Two §24 requirements are unimplemented**: no data-loss alert to
   `#blackink-qa` for events buffered over an hour (there is no age tracking
   to trigger it), and `_pending_buffer` is not lock-guarded, so "async-safe
   concurrent writes" is not met. Also `log_event` is **synchronous** while
   Cora's queue is async — calling it directly from an async worker blocks
   the event loop.

### Decisions

**Work around 1–3 rather than renegotiating Dev 4's module** (their branch is
in flight, and they already flag the durable outbox as a known upgrade path).
Specifically: **Task 3.1 carries its own dispatch timestamp inside the
payload** and never relies on `created_at` for sequencing or windowing. This
makes 3.1's events self-describing and costs no cross-dev negotiation. Raise
gap 4's `#blackink-qa` alert with Dev 4 as a spec item they may not have seen.

**Write events in the sequencer's own transaction** — pass `session=`. The
event is then atomic with the dispatch record: no send without an event, no
event without a send. For a DoD line and a billing-adjacent audit record,
atomicity beats decoupling.

**Knock-on for ticket 08 — the send cap must NOT count events.** A rolling
24h `COUNT(*)` over `outbound_touch_dispatched` would undercount
silently-dropped events (gap 1) and mis-bucket re-timestamped ones (gap 3),
and **both errors push permissive** — exceeding 50/mailbox/day and burning a
warmed domain. This eliminates one of ticket 08's three candidate substrates
on correctness grounds.

**Still open, carried to ticket 03:** what `entity_type` / `entity_id` should
be for each of Task 3.1's event types. Dev 4 made them required parameters but
did not specify values.

## Question

§8 and §24 describe a shared, central event-logging service that every Week 1
component must write through. It does not exist. There is no `log_event()`,
no `MalformedEventError`, no payload validation, no buffering. The only
writer in the repo is a private `_log_event(...)` in
`src/services/slack/listeners.py` doing a direct INSERT — precisely what §24
says components must not do.

Task 3.1 depends on it for at least six event types:
`outbound_touch_dispatched`, `linkedin_task_created`,
`inbound_reply_received`, `opt_out_recorded`, `email_opened`,
`email_clicked`, plus unnamed skip events.

Two questions, in order:

1. **Ownership.** §24 frames this as *shared* Week 1 infrastructure across all
   four devs. Is it Dev 3's to build, another dev's task Dev 3 should treat
   as a blocking external dependency, or unowned and therefore Dev 3's by
   default? Check with the team before assuming. If it is external, this map
   gains a dependency it cannot resolve alone and the answer should say so.

2. **Scope of v1.** §24 specifies quite a lot: schema validation per event
   type, `MalformedEventError` on missing required keys, async-safe
   concurrent writes, local buffering of failed DB writes capped at 1,000
   events, retry, and a data-loss alert to `#blackink-qa` for anything
   buffered over an hour. Which of these does the first version need for
   Task 3.1 to proceed, and which can follow? Buffering and the alert look
   separable from validation; validation does not look separable from
   anything.

Also settle: does the existing private `_log_event` in the Slack listeners
get migrated onto the new service as part of this, or left alone?

## Why it matters

Every DoD item in all three subtasks ends in "and the event is written".
If the logger's contract is not fixed, none of them can be verified.
