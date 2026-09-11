# S-2 — Cora 24-hour stale-draft age bound

**Task:** W0 §3.0.2 C.
**Audit item:** Dev Item S-2.
**Status:** task-analysis output. No source file changed.

## 1. What the task asks for

Blueprint §3.0.2 C:

> "Strict queue bounds and pacing throttles are embedded into draft
> orchestrators. Generation limits automatically pause new drafting once
> unreviewed Slack approval queues reach capacity (**50 unreviewed drafts
> or any unreviewed draft older than 24 hours**), resuming only as human
> reviews clear items."

The 50-count bound is built (`throttle.py:33-77`, tested in
`tests/test_cora_throttle.py`). The 24-hour age bound is not built at all.

## 2. The contradiction I found and resolved

The audit's own suggested fix ("An age predicate in `throttle.py` calling
`queued_depth(older_than=24h)`") **does not work** — verified by reading
both sides:

- Cora's actual approval backlog (`throttle.py`'s `cora:approval:pending`
  Redis key) is a bare integer counter, incremented only from
  `src/agents/cora/worker.py:210`'s `_process_draft()` and decremented from
  the genuine Cora-approval Slack handlers
  (`src/services/slack/listeners.py:1780-1782`, `approve_ink_campaign`/
  `reject_ink_campaign`). It carries **no per-item timestamp** — there is
  no way to ask "how old is the oldest entry" from an integer.
- `queued_depth(older_than=...)` (`src/services/work_orders/__init__.py:332`)
  queries the **`agent_work_orders` SQL table**, `WHERE status = 'QUEUED'`.
  That table is written by `sequence_enrollment.py`, `winback_sequencer.py`,
  `stl_cadence.py`, and `meeting_outcome_prompts.py` — the cold-outbound
  touch, win-back touch, STL-cadence, and Log-Outcome approval-card
  systems. **Cora's own drafts never create a row there** — confirmed by
  `worker.py`'s own docstring: *"`_process_draft()` is intentionally
  minimal for week 0 ... It logs the event and calls `notify_draft_queued()`
  as a placeholder"* — Cora's card uses Redis (`draft_store.py`) only.

`queued_depth()`'s own docstring claims *"What Dev 2 §C's Cora throttle
reads"* — this is simply wrong; it was written assuming Cora's queue would
live in `agent_work_orders`, but Cora's actual implementation never landed
there. Two different teams' work never got reconciled.

**Resolution (Step 2 — existing code wins over a mismatched docstring):**
wiring `throttle.py`'s age check to `queued_depth()` would silently gate
Cora's drafting on the age of an **unrelated** subsystem's approval
backlog (sequence/win-back/STL touches) instead of Cora's own. The age
bound must be built against Cora's own Redis-based queue instead. This is
a technical correction, not a business decision — no client question
needed.

**Also found, out of scope for this task, flagged not fixed:**
`listeners.py:792-797`'s `_finalize_terminal_decision()` — the *generic*
`agent_work_orders` approve/reject handler used by every one of those four
unrelated subsystems — **also** calls `notify_approval_resolved()`,
decrementing Cora's counter for approvals that have nothing to do with
Cora. Today this is harmless only because Cora's worker isn't registered
anywhere in `main.py`/`crontab.txt` (per the Week 0-2 audit's own W-1
finding) — `notify_draft_queued()` never fires in production, so the
counter sits at/near 0 regardless. The moment Cora's worker is wired up
(W-1), this pre-existing miswiring would start under-counting Cora's real
backlog every time an unrelated sequence/win-back/STL card is
approved/rejected. Not touched here (out of scope, unrelated to the age
bound) — flagged for the user, same as the `apply_stl_cadence.py`
`os.environ` defect flagged during S-1.

## 3. Design

The key insight: `is_auto_paused()` is read on **every** worker-loop
iteration via `cora_should_stop()` (called before claiming any new work,
per `worker.py`'s own docstring step 1) — not just reactively when
`notify_draft_queued`/`notify_approval_resolved` fire. Making the age
condition a **live computation inside `is_auto_paused()`** (rather than a
second sticky flag set reactively) closes the one real gap a
reactive-only design would have: a queue that goes idle with a single
stale item sitting unreviewed — no new draft arrives to re-trigger a
check, so a purely event-driven flag would never notice. A live check on
every read has no such gap, and needs no new scheduled job.

| Piece | Design |
|---|---|
| New Redis key | `cora:approval:queued_at` — a **LIST**, FIFO, oldest at index 0, values are `str(time.time())`. No TTL (matches the existing two keys' no-TTL posture). Not tenant-scoped (matches the module's existing stated policy). |
| New constant | `DRAFT_MAX_AGE_HOURS: int = 24` — bare module constant, same style as `DRAFT_QUEUE_CAPACITY`/`RESUME_THRESHOLD` (no `config/settings.py` env var — the blueprint states a fixed value, not something to make configurable). |
| `notify_draft_queued()` | Adds one `RPUSH cora:approval:queued_at <now>` alongside the existing `INCR`. Same try/except/return -1 envelope, no signature change. |
| `notify_approval_resolved()` | Adds one `LPOP cora:approval:queued_at` alongside the existing `DECR`. Popping the **head** (oldest) is a deliberate best-effort approximation — with the pre-existing miswiring in §2, we cannot always know which item was actually reviewed; retiring the oldest tracked entry on any resolution event bounds the list's growth and self-corrects over time without touching the miswired call site. No signature change, no ID needed at either the correct or the miswired call site. |
| `is_auto_paused()` | Extended: `True` if `_AUTO_PAUSED_KEY` exists (unchanged, count-based, sticky/hysteresis) **OR** the oldest entry in the FIFO list is older than `DRAFT_MAX_AGE_HOURS` (new, live, no sticky flag — self-clears the instant that entry is popped). Boundary: **strict `>`** ("older than 24 hours"), not `>=` — deliberately different from the count bound's `>=` ("reach capacity"), matching the blueprint's exact wording for each. |
| `_check_capacity()` / `_maybe_resume()` / `_AUTO_PAUSED_KEY` | **Unchanged.** The age condition needs no hysteresis of its own (a live check on a monotonically-non-decreasing age only ever flips true→false when the stale entry is actually removed — no flapping-at-the-boundary risk the way rapid queue churn near 50 has). |
| Fail-open on Redis error | The new age helper returns `None` on any Redis error (logged), and `is_auto_paused()` treats `None` as "no age problem" — same fail-open posture the module already documents for the count path (an automatic quality guard, not the Relay compliance halt). |
| `work_orders/__init__.py::queued_depth()` | One-line docstring fix only — remove the false "What Dev 2 §C's Cora throttle reads" claim (§2). No behavior change. |
| `kill_switch.py` | Docstring update only, to mention both triggers instead of just capacity. No code change — `cora_should_stop()` already just reads `is_auto_paused()`. |

## 4. Requirement → trace → code → test

| # | Requirement | Trigger → outcome trace | Code path | Test |
|---|---|---|---|---|
| R1 | A draft older than 24h pauses new drafting, even if the count is far below 50 | `notify_draft_queued()` pushes a timestamp → time passes with no review → worker loop's next iteration calls `cora_should_stop()` → `is_auto_paused()` reads the FIFO head → age > 24h → `True` → worker idles, claims no new event | `throttle.py::is_auto_paused()`, `_oldest_queued_age_seconds()` | `test_is_auto_paused_true_when_oldest_item_older_than_24h` — seed the list directly with a backdated timestamp (no need to wait 24h), assert `is_auto_paused()` True with count=1 |
| R2 | Boundary: exactly 24h old is NOT paused; 24h+1s is | age comparison uses strict `>` | `is_auto_paused()` | `test_exactly_24h_old_is_not_paused`, `test_24h_plus_one_second_is_paused` |
| R3 | The pause clears once the stale item is actually resolved | operator clicks Approve/Reject on the genuine Cora card → `notify_approval_resolved()` → `LPOP` removes the stale head entry → next `is_auto_paused()` call sees either an empty list or a fresher head → `False` (assuming count is also below `RESUME_THRESHOLD`, or age-only if count already low) | `notify_approval_resolved()`, `is_auto_paused()` | `test_age_pause_clears_after_stale_item_is_popped` |
| R4 | A count-triggered pause is NOT lifted while a different item is still stale, even after count drops below `RESUME_THRESHOLD` | `_maybe_resume()` clears `_AUTO_PAUSED_KEY` on count alone → but `is_auto_paused()`'s live age check independently still returns `True` because an older, different entry remains at the list head | `is_auto_paused()`'s OR logic (no change needed in `_maybe_resume`) | `test_resume_blocked_by_still_stale_item_even_after_count_drops` |
| R5 | Existing count-only behavior is unchanged | all 12 existing tests in `test_cora_throttle.py` | unchanged `_check_capacity`/`_maybe_resume` | full existing suite re-run, must stay green |
| R6 | Redis failure reading the age list fails open (no false pause) | `_oldest_queued_age_seconds()` raises internally → caught → returns `None` → `is_auto_paused()` treats as "no age problem" | `_oldest_queued_age_seconds()` | `test_oldest_queued_age_returns_none_on_redis_error` |
| R7 | `queued_depth()`'s docstring no longer makes a false claim | — | `src/services/work_orders/__init__.py:333` (comment only) | none needed (doc-only) |

No new tenant-bearing table — this is Redis-only, matching the module's
existing "NOT tenant-scoped" policy. No `TENANT_POLICIES`/RLS changes.

## 5. Open questions

None. The one real ambiguity (§2, which queue the age bound should read)
resolves cleanly via existing code without a business decision, and is
stated above rather than silently chosen.

---

**Next:** `production-execution` against §4.
