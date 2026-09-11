# Consolidated work log — single branch: `feature/audit-spec-gaps`

This branch carries multiple sections/tasks, worked one at a time. Git
actions (commit, push, or anything else touching git state) are held until
the user explicitly says so — work accumulates in the working tree /
local commits only when instructed. This file is the running record of
what was done, in order, so the eventual PR description and review can be
built from it without reconstructing history.

Base: `origin/main` (up to date as of 2026-09-10).

---

## Section 1 — S-1: Vera health job + settlement/billing halt consumer

**Status:** built and unit-tested. Not committed — git stays held per
standing instruction. Still worth a client sign-off on the ABSTAIN-vs-UNKNOWN
question below before this ships; not otherwise blocking.

- Plan: `docs/plans/2026-09-10-s1-vera-health-gate.md`
- Task: W0 §3.0.2 A + Week-0 AC #2 — Vera health checks existed and were
  already correctly built, but nothing called `run_health_checks()` and
  nothing halted settlement/billing on `ABSTAIN`.
- Decision confirmed with user: `ABSTAIN` halts, `UNKNOWN` does not (plan
  §7 Q-A). Recorded in `health_gate.py`'s module docstring and in CLAUDE.md.

**Built:**
- `migrations/apply_vera_health_runs.py` — new, non-tenant `vera_health_runs`
  table (persisted per-check state per run). Added to `config/tenant_policies.py`'s
  "deliberately not tenant-scoped" list, to `CLAUDE.md`'s migration order,
  and to both `.github/workflows/{tests,tenant_leakage_nightly}.yml`.
- `src/agents/vera/health_gate.py` — new. `evaluate_settlement_health()`
  reads the latest run and returns OK, or HALT with one of three reasons:
  `ABSTAIN`, `NO_HEALTH_RUN`, `STALE_HEALTH_RUN` (the latter two are a
  deliberate extension beyond the audit's literal wording — traced to the
  Source of Truth doc's line 534 on the same bug class). Opens its own
  short-lived DB session, independent of the caller's, so a halted sweep
  can be proven to never touch its own session.
- `src/tasks/vera_health_sweep.py` — new. Runs all 3 existing checks
  (unchanged), persists one row, and alerts `#blackink-qa` /
  writes `vera_health_halt_issued` to the proof ledger (`events`) only on a
  state *transition*, not every tick.
- `src/tasks/settlement_sweep.py`, `src/tasks/billing_sweep.py` — all 7
  sweep functions now call the gate first, before opening their own DB
  session or making a Stripe call.
- `src/api/main.py` — registers `vera_health_sweep.run_sweep` as a 5-minute
  background thread, same run surface as the sweeps it protects (not cron).
- `config/settings.py` — `vera_health_max_age_minutes` (default 30).
- `src/services/events.py` — new `vera_health_halt_issued` event type.
- `scripts/crontab.txt` — comment only, clarifying this is deliberately NOT
  a cron entry.
- CLAUDE.md — new Architecture subsection documenting the whole mechanism.

**Test changes:**
- New `tests/test_vera_health_gate.py` (17 tests) — gate decision logic
  (ABSTAIN/NO_HEALTH_RUN/STALE_HEALTH_RUN halt, UNKNOWN does not) and all 7
  sweep functions proven to short-circuit without opening their own DB
  session while halted.
- New `tests/fixtures/vera_health.py::healthy_vera_run` — seeds one healthy
  row for live-DB tests; deliberately NOT folded into the shared
  `canary_tenants` fixture (used by ~30 unrelated tenant-isolation tests).
- `tests/test_billing_live.py` — `attended_billable_appointment` now also
  depends on `healthy_vera_run` (the one live-DB call site of a gated sweep,
  `run_sit_invoice_sweep`, found by grepping every test file for all 7 gated
  function names before writing any code).
- `tests/test_billing_sweep_gate.py` — updated
  `test_miss_credit_sweep_runs_when_explicitly_enabled` to mock the new
  gate, since it tests the enable/disable flag specifically, not the gate.
- `tests/test_background_workers.py` — new registration regression guard,
  same pattern as the existing show-rate-reminder one.

**Verification done this session:** all new/modified files import cleanly;
43 targeted tests pass; all 202 `test_migration_coverage.py` checks pass
(confirms the new migration is correctly wired into CLAUDE.md + both CI
workflows); full non-live suite run before and after (via `git stash`) to
confirm the 10 pre-existing failures on `origin/main` are unchanged by this
work — nothing here caused a new failure. This is NOT a substitute for the
`testing-verification` skill pass (live-DB, boundary, and DoD-line
verification still to come).

**`testing-verification` pass completed 2026-09-10.** Ran against a
genuinely fresh, disposable Postgres container (`blackink-s1-fresh-test`,
port 5555 — NOT the pre-existing `blackink-local-pg` dev container, which
was left untouched throughout; both torn down/cleaned up after). Full
PASS/PARTIAL/BLOCKED table:

| # | Category | Result | Evidence |
|---|---|---|---|
| 1 | Unit tests | **PASS** | `tests/test_vera_health_gate.py` — 17 tests: gate decisions (ABSTAIN/NO_HEALTH_RUN/STALE_HEALTH_RUN halt, UNKNOWN doesn't, DB-read-error fails closed), all 7 sweep functions proven to never open their own DB session while halted. |
| 2 | Integration (real Postgres) | **PASS** | `tests/test_billing_live.py` — 17/17 pass against fresh DB with real gate wired in. Additional ad-hoc script (since deleted) ran the REAL `vera_health_sweep.run_sweep()` (real DB checks + real "Instantly unconfigured" UNKNOWN path) end to end and confirmed: no-run halts a real sweep call; a real health run un-halts it; a later ABSTAIN row halts again even after a healthy one ("latest row wins"); a 45-min-old row halts on staleness. |
| 3 | Fresh-DB migration | **PASS** | All 60 migrations from CLAUDE.md's Common Commands, in documented order, applied cleanly to a genuinely empty DB. Table/constraint/grant structure verified via `\d vera_health_runs` and `information_schema.role_table_grants` — matches the migration exactly. |
| 4 | Migration rerun/idempotency | **PASS** | Full 60-migration sequence re-run a second time end to end with zero errors. |
| 5 | Tenant isolation | **N/A, justified** | `vera_health_runs` is deliberately not tenant-bearing (platform-wide checks — see D4 in the plan); correctly absent from `TENANT_POLICIES`, present in its "deliberately not tenant-scoped" comment list. Full `tests/test_tenant_isolation.py` suite (38 tests) re-run against the DB carrying the new migration — all pass, confirming no interference. |
| 6 | Boundary tests | **PASS** | Exact 30-minute staleness threshold tested both as a fake-session unit test (`test_exact_30_minute_boundary_is_not_stale` / `test_one_second_past_30_minutes_is_stale`) AND independently against real Postgres with real timestamps — both agree: exactly 30:00 healthy, 30:01 stale (strict `>`). |
| 7 | Duplicate/concurrent-worker | **N/A, justified** | The gate is read-only (one `SELECT ... LIMIT 1`, no claim query) — no race to test. `vera_health_sweep.run_sweep()` runs on exactly one background thread; concurrent sweep threads only ever read the latest committed row (Postgres read-committed isolation — no partial-write visibility issue). |
| 8 | Failure/retry/restart | **PASS, with one documented residual risk** | Unit-tested: a DB error reading `vera_health_runs` fails closed to `NO_HEALTH_RUN` (never assumes healthy). Both `post_notice` and `log_event` are documented "never raises" for their normal failure paths. Residual risk found and documented (not fixed — low severity, fails in the safe direction): if the event-log's own `get_system_db_context()` call fails structurally (not `log_event`'s own internal handling), `run_sweep()`'s `_previous_ok` flag is left stale, which can cause at most one redundant re-alert on the next tick — never a missed halt alert. |
| 9 | External-provider test-mode | **BLOCKED — deferred to server, by user decision (2026-09-10)** | Checked this machine's local `.env`: it holds only DB credentials, no Slack bot/app token — real Slack credentials exist only on the server (per the audit's own note). User chose to verify the `#blackink-qa` post manually on the server later rather than add local Slack credentials. One-liner to run there when convenient: `PYTHONPATH=. python -c "import asyncio; from src.services.slack.post import post_notice; print(asyncio.run(post_notice(channel_key='qa', text=':test_tube: S-1 Vera health gate manual test — ignore, verifying alert wiring only.')))"` — a non-`None` return value (the message `ts`) confirms the post succeeded. |
| 10 | Full regression | **PASS** | Full suite run against the real fresh DB (not excluding live tests, unlike production-execution's earlier pass): 1741 passed, 7 failed, 1 skipped. All 7 failures confirmed pre-existing on `origin/main` via `git stash` + identical rerun against the same DB (same 7 test names, same failures, with or without this change). |

**Also found, pre-existing, unrelated to S-1 (flagged, not fixed — "keep
unrelated changes outside the task branch"):** `migrations/apply_stl_cadence.py`
reads `os.environ.get("DATABASE_URL")` directly via raw `psycopg2`, bypassing
`config/settings.py`'s `ENV_FILE` mechanism entirely — violates CLAUDE.md's
own Tooling Rules ("never read `os.environ` directly elsewhere"). Every other
migration script correctly goes through the settings-based DSN. Worth its own
small fix, separately.

**Genuinely still unverified:** the real Slack alert post (item 9). Everything
else — schema, migrations, gate logic (unit AND live), staleness boundary,
tenant-isolation non-interference, and full-suite non-regression — has been
verified with real evidence above.

**`code-review` pass completed 2026-09-10 — 5 findings, all fixed and
re-verified:**
1. Alert/transition logic (`_persist_run`/`_maybe_alert_and_log`/
   `_result_by_name`/`run_sweep`/`_previous_ok`) had zero test coverage —
   fixed: new `tests/test_vera_health_sweep.py` (12 tests).
2. Dead `halt_reason` column (declared, never written, 2 of 3 allowed
   values could never be truthfully written anyway) — fixed: dropped from
   the migration.
3. No rollback path documented for the new migration — fixed: added to
   the migration's own docstring.
4. (Minor) halt-reason constants were module-private — fixed: exported
   (`HALT_ABSTAIN`/`HALT_NO_HEALTH_RUN`/`HALT_STALE_HEALTH_RUN`), test file
   updated to import them instead of hardcoding string literals.
5. (Minor) Slack alert posted before the durable event write on halt entry
   — fixed: reordered so the proof-ledger record survives a Slack-side
   failure that isn't a plain `SlackApiError`.

Re-verified after fixes: 1579 passed (was 1568; +11 new tests), same 10
pre-existing failures, migration re-applied clean to a fresh DB with the
column removed. **S-1 is fully done** except the two non-blocking items
below.

**Remaining before this section is fully done:**
- The ABSTAIN-vs-UNKNOWN question surfaced to the client/lead (one-liner
  in plan §7 Q-A) — not blocking further work, but should land before
  final sign-off.
- The real Slack alert post (testing-verification item 9) — deferred to
  the server by user decision; one-liner command given above.
- A decision on whether/when to fix the unrelated `apply_stl_cadence.py`
  defect found above.

---

## Section 2 — S-2: Cora 24-hour stale-draft age throttle

**Status:** built and unit-tested (task-analysis → production-execution
complete). Not committed. `testing-verification`/`code-review` not yet run
for this section.

- Plan: `docs/plans/2026-09-10-s2-cora-24h-age-throttle.md`
- Task: W0 §3.0.2 C — the 50-draft count bound existed; the "any unreviewed
  draft older than 24 hours" bound did not.
- **Found and resolved without needing a client question:** the audit's own
  suggested fix (wire the age check to `work_orders.queued_depth(older_than=...)`)
  was wrong — that helper counts `agent_work_orders` rows belonging to four
  *unrelated* subsystems (sequence touches, win-back, STL cadence, meeting
  outcomes). Cora's real backlog is a separate Redis integer counter with no
  per-item timestamps at all. Built age-tracking against Cora's own queue
  instead.
- **Found and flagged, not fixed (out of scope):** the generic
  `agent_work_orders` approve/reject handler (`listeners.py`'s
  `_finalize_terminal_decision`) also decrements Cora's counter for
  approvals that have nothing to do with Cora. Currently harmless only
  because Cora's worker isn't registered anywhere yet (separate known gap,
  audit item W-1). Will start under-counting Cora's real backlog the moment
  W-1 is fixed.

**Built:**
- `src/agents/cora/throttle.py` — new `cora:approval:queued_at` Redis LIST
  (FIFO, timestamps), new `DRAFT_MAX_AGE_HOURS=24` constant, new
  `_oldest_queued_age_seconds()`/`_count_flag_set()` helpers.
  `is_auto_paused()` now returns True if EITHER the sticky count flag is set
  OR the oldest queued item is live-computed as >24h old — live rather than
  a second reactive sticky flag, because `is_auto_paused()` is read on every
  worker-loop tick (not just on queue/dequeue events), which is what closes
  the "one stale item in an otherwise-idle queue" gap a purely event-driven
  flag would miss. `notify_draft_queued()`/`notify_approval_resolved()`
  unchanged signatures, each now also pushes/pops the FIFO list.
  `_check_capacity()`/`_maybe_resume()` updated to read the new
  count-only `_count_flag_set()` instead of the combined `is_auto_paused()`
  — a bug caught during my own implementation, before it ever became a
  test failure: the original substitution would have made `_maybe_resume()`
  log a bogus "auto-resumed" whenever only the age condition was true, and
  made `_check_capacity()` skip setting the real count flag on a tick where
  age also happened to be breached.
- `src/services/work_orders/__init__.py` — one-line docstring fix on
  `queued_depth()`, removing the false "what Cora's throttle reads" claim.
- `src/agents/cora/kill_switch.py` — docstring only, mentions both triggers.

**Test changes:**
- `tests/test_cora_throttle.py` — 11 new tests: FIFO push/pop, age-alone
  triggers a pause with count far below capacity, exact 24h boundary (not
  paused) vs 24h+1s (paused, using a pinned `time.time()` to avoid a
  real timing-precision flake I hit and fixed), pause clears once the stale
  entry is popped, multiple-stale-entries survive more than one resolve
  (a FIFO correctness case I got wrong on the first attempt — LPOP always
  retires the single oldest entry, so one stale item cannot outlive even
  one resolve; needed several stale entries back-to-back to test outliving
  multiple), resume blocked by remaining stale entries even after the count
  flag clears, Redis-error fail-open. Also fixed the ORIGINAL pre-existing
  `test_is_auto_paused_returns_false_on_redis_error` — its `MagicMock` never
  configured `.lindex`, so the new age-check path called it and got a
  `MagicMock`-default `float() == 1.0`, misread as a very old timestamp,
  flipping a "Redis is fully down" test to incorrectly expect "paused".

**`testing-verification` pass completed 2026-09-10.** This feature is
Redis-only (no DB, no migration, no tenant-bearing table) — categories 3, 4,
5 are N/A, stated explicitly rather than skipped silently.

| # | Category | Result | Evidence |
|---|---|---|---|
| 1 | Unit tests | **PASS** | `tests/test_cora_throttle.py` — 32/32 pass (11 new for S-2). |
| 2 | Integration | **PASS** | No DB to integrate with; instead ran the real external dependency — pointed `throttle.py` at the real, disposable `blackink-test-redis` container (port 6380, left untouched otherwise, all test keys cleared before/after) instead of `fakeredis`. Boundary result matched the unit tests exactly. |
| 3 | Fresh-DB migration | **N/A** | No schema change — Redis keys only. |
| 4 | Migration rerun/idempotency | **N/A** | Same reason. |
| 5 | Tenant isolation | **N/A, justified** | Module's own pre-existing docstring already states these Redis keys are deliberately not tenant-scoped (a global platform resource for week 0) — unchanged by this task. |
| 6 | Boundary | **PASS** | Exactly 24h00m00s -> not paused, 24h+1s -> paused. Confirmed twice: `tests/test_cora_throttle.py::test_exactly_24h_old_is_not_paused`/`test_24h_plus_one_second_is_paused` (fakeredis, `time.time()` pinned to avoid the flake described above) AND independently against real Redis (see #2). |
| 7 | Duplicate/concurrent-worker | **PASS** | Real threads (20 concurrent `notify_draft_queued()` + 8 concurrent `notify_approval_resolved()`) against real Redis (not fakeredis, not sequential calls) — final state exactly consistent: count=12, age-list length=12, matching the expected 20-8=12 with zero lost updates. Proves Redis's own atomic `INCR`/`DECR` holds under real concurrent access. |
| 8 | Failure/retry/restart | **PARTIAL — one narrow gap found, not fixed (code-review's call)** | Redis-error fail-open is unit-tested (`test_oldest_queued_age_returns_none_on_redis_error`, `test_is_auto_paused_returns_false_on_redis_error`). **New finding**: `notify_draft_queued()`'s `INCR` + `RPUSH` are two separate Redis calls, not one atomic operation (same for `notify_approval_resolved()`'s `DECR` + `LPOP`). A crash between the two leaves the counter and the age-list length disagreeing by one. Traced both directions: a crash after `INCR`/before `RPUSH` undertracks one draft's age (never flagged stale — an under-pause risk, but the count bound is unaffected and still catches it at 50). A crash after `DECR`/before `LPOP` leaves a stale timestamp for an already-resolved draft, which can trigger an unnecessary later pause (an over-pause risk). Both windows are a single line wide with no I/O in between (microseconds), and both fail in a bounded, narrow, non-financial direction — the second (over-pause) is arguably the *safe* direction for a "pause new AI-generated content" guard. Not wrapped in a Redis `MULTI`/pipeline to close this — flagging for code-review to decide if it's worth the added complexity given the failure's low probability and bounded impact. |
| 9 | External-provider test-mode | **PASS** | Real Redis is this task's only external dependency — verified for real in #2/#7 above, not mocked. |
| 10 | Full regression | **PASS** | 1590 passed, same 10 pre-existing failures (`test_calendar_subscription_renewal.py` x2, `test_ghost_shopper.py` x2, `test_no_show_prompts.py::test_token_round_trips`, `test_schema.py` x3, `test_show_rate_reminders.py::test_30min_reminder_fires_within_60s_of_the_30min_mark_and_sends`, `test_slack_listeners.py::test_log_outcome_opens_modal_for_assigned_closer`) — exact same test names as every prior session's baseline. |

**DoD table (plan §4, R1-R7):**

| Requirement | Evidence | Verdict |
|---|---|---|
| R1 — age alone pauses, count far below capacity | `test_is_auto_paused_true_when_oldest_item_older_than_24h` (count=1) | **PASS** |
| R2 — exact 24h/24h+1s boundary | fakeredis + real-Redis, both above | **PASS** |
| R3 — pause clears once stale item popped | `test_age_pause_clears_after_stale_item_is_popped` | **PASS** |
| R4 — count-clear doesn't lift a still-stale age pause | `test_resume_blocked_by_remaining_stale_items_even_after_count_flag_clears` | **PASS** |
| R5 — existing count-only behavior unchanged | all 12 original tests still pass; full regression stable | **PASS** |
| R6 — Redis failure fails open | `test_oldest_queued_age_returns_none_on_redis_error`, `test_is_auto_paused_returns_false_on_redis_error` | **PASS** |
| R7 — `queued_depth()` false-claim docstring fixed | doc-only diff in `work_orders/__init__.py`, read back to confirm | **PASS** |

No new production wiring was needed for this task — `notify_draft_queued`/
`notify_approval_resolved`/`is_auto_paused` all already have real callers
(`worker.py`, `listeners.py`, `kill_switch.py`) with unchanged signatures,
so the new behavior is live wherever they're already called — no orphan
function risk.

**Genuinely still unverified:** none of the DoD requirements — only the
narrow, low-severity crash-window finding above (item 8), explicitly not a
DoD requirement, surfaced for code-review's judgment call.

**`code-review` pass completed 2026-09-10 — 2 findings, both Important, both
fixed and re-verified:**
1. `notify_draft_queued()`'s `INCR`+`RPUSH` and `notify_approval_resolved()`'s
   `DECR`+`LPOP` were two separate, non-atomic Redis calls — upgraded from
   testing-verification's "narrow, code-review's call" framing to a formal
   Important finding once traced fully: a crash between `DECR` and `LPOP`
   leaves a permanent phantom entry that, once it crosses 24h, would
   indefinitely block ALL new Cora drafting with no real backlog behind it
   and no self-healing path — fixed by wrapping both pairs in a Redis
   pipeline (atomic `MULTI`/`EXEC`).
2. The pause log message never distinguished count vs. age as the cause —
   fixed: new `throttle.pause_reason()` ("count"/"age"/"count+age"/`None`),
   `kill_switch.py`'s log line now names it.

Re-verified after fixes: `test_cora_throttle.py` 39/39 (7 new tests: 4 for
`pause_reason()`, 3 for the pipeline atomicity — including one that force-fails
the pipeline and confirms neither write lands). Re-ran the real-Redis
concurrency check (20 concurrent queues + 8 concurrent resolves against the
real `blackink-test-redis` container) specifically because it's the exact
scenario the atomicity fix targets — count=12, list_len=12, exact lockstep
under real concurrent access. Full regression: 1597 passed (+7), same 10
pre-existing failures, zero new failures.

**S-2 is fully done** — both findings resolved, all DoD requirements PASS,
no unresolved Critical/Important findings. Ready for `blackink-pr` once
bundled with the rest of this branch's sections.

---

## Section 3 — S-8 + S-11: tracking pixel/click/email_replied + reply-send + Book Meeting

**Status:** task-analysis complete (plan:
`docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md`). Shared blocking
prerequisite fully built, live-verified, and unit-tested (autonomous tick,
no new user input — continuing this session's own stated plan/default).
S-8/S-11's own new work (pixel/click endpoints, reply-send handler, Book
Meeting) not yet started.

**Prerequisite fix completed this tick — `src/services/inbound_ingest.py`
(`ingest_inbound_reply`):** found and fixed a genuinely pre-existing,
currently-shipped bug, unrelated to S-8/S-11's original ask but blocking
both (both need this exact function): the code wrote/read
`message_id`/`from_address`/`to_alias`/`raw_body`/a UUID `id`, none of which
exist on the real table (`original_message_id`/`sender_email`/
`destination_address`/`body_text`/`BIGSERIAL id` instead — confirmed by
reading the winning `CREATE TABLE` directly, not just citing research). Also
found mid-fix: the real `UNIQUE` constraint is on `idempotency_key`
(`SHA-256(destination_address + original_message_id)`, per the migration's
own comment), never populated by this function at all — every real call
would have raised `UndefinedColumn`/`NotNullViolation` against a live
Postgres. Fixed using the exact working pattern already proven in
`src/api/inbound_router.py::_idempotency_key`. Also switched the function's
raw `INSERT INTO events` to the repo's single write path (`log_event()`),
since a new-style write was being added right next to the old-style one in
the same edit anyway.

**Also found, separate, unrelated, flagged not fixed:**
`migrations/apply_sequence_touch_dispatches.py` creates its table with zero
`GRANT` statements — `blackink_app`/`blackink_system` have never had
SELECT/INSERT on it, meaning `inbound_attribution.py::is_bcc_echo()`'s query
against it has been failing with `InsufficientPrivilege` in every real
deployment. Discovered only because live verification of the fix above
required reading that exact table. Needs its own migration fix (add the
missing `GRANT`s) — not done here, out of scope for this task's diff.

**Verification this tick:**
- Existing `tests/test_inbound_ingest.py` (7 tests) initially failed after
  the fix — not a regression, but because its `_fake_session()` fixture
  returned the same mocked result for every `session.execute()` call and
  couldn't distinguish the dedup `SELECT` from the new `INSERT ... RETURNING
  id`. Made it call-aware (inspects the SQL text). All 7 pass.
- **Live-DB proof** (the exact category that would have caught this bug
  originally, since mocks can't validate real column names): spun up an
  isolated, disposable Postgres container, ran the *entire* documented
  migration sequence, and executed `ingest_inbound_reply()` for real —
  confirmed a correct row lands with real column values, the
  `inbound_reply_received` event writes correctly, and a second ingest of
  the identical message is correctly deduped (`{"status": "discarded",
  "reason": "duplicate"}`), never a second row. Torn down after.
- Full regression: 1597 passed, same 10 pre-existing failures, zero new.

**S-8 build completed (this tick):**
- `config/settings.py` — new `email_tracking_secret` (dedicated, not reused
  from `email_unsubscribe_secret` — CLAUDE.md's own "unrelated token
  families must rotate independently" reasoning).
- `src/services/email_tracking.py` — new. Mirrors `email_unsubscribe.py`'s
  mint/verify/url shape exactly: `mint_pixel_token`/`verify_pixel_token`/
  `pixel_url`, `mint_click_token`/`verify_click_token`/`wrap_link`, each
  with its own `_TOKEN_TYPE` so a pixel token can never be replayed as a
  click token. Click tokens carry their destination URL inside the signed
  token itself (chosen by us at send time) — the public click endpoint
  never takes a request-supplied redirect target, closing the open-redirect
  risk without a DB lookup. Also `html_body_with_pixel()` — a deliberately
  minimal plain-text-to-HTML render (line breaks only, no redesign — that's
  dev items S-4/S-5's job) with the pixel `<img>` appended, HTML-escaped.
- `src/api/email_tracking_router.py` — new. `GET /api/v1/public/pixel` (always
  returns a valid 1x1 GIF, valid token or not — a broken pixel would itself
  be a signal) and `GET /api/v1/public/click` (302 to the token's own URL,
  generic error page if invalid). Both dedupe their event write per
  `dispatch_id` before inserting — undeduped, the digest's
  `COUNT(email_opened)/COUNT(dispatched)` open-rate would be inflated by
  every re-fetch (Apple Mail Privacy Protection alone prefetches every
  pixel once at delivery, on top of that). Mounted in `main.py`.
- `src/services/events.py` — registered `email_opened`, `email_clicked`,
  `email_replied` (exact names required by `daily_digest.py`'s own
  documented event contract).
- `src/services/sequence_orchestrator.py` — `dispatch_touch` now builds the
  HTML body with the pixel and passes `html_body=` to `sender.send()`
  (previously omitted entirely, even though `EmailSender.send()` already
  supported it). Click-wrapping mechanism built but not called here —
  stated explicitly in-code: no touch body today contains a link besides
  the plain-text (deliberately unwrapped — RFC 8058 reasons) unsubscribe
  footer.
- `src/services/inbound_ingest.py` — `email_replied` producer, gated on
  Tier-1 (message-id) attribution only, since that's the only case with a
  resolvable `dispatch_id`; a Tier-2 (sender-email-only) match still posts
  to `#sales-replies` but doesn't count toward the digest's reply-rate —
  stated in-code as a scope line, not a silent gap.
- `src/services/settlement/evidence.py` — §2's gap line is now conditional
  on a real query against `events` for the transaction's contacts, not an
  unconditional literal; prints real open/click/reply fields when present.

**Test changes:** new `tests/test_email_tracking.py` (10), new
`tests/test_email_tracking_router.py` (8), new
`tests/test_evidence_section2_engagement.py` (3), 3 new tests added to
`tests/test_inbound_ingest.py` (Tier-1-with-dispatch writes the event,
Tier-1-without-a-SENT-dispatch doesn't, Tier-2 doesn't), 1 new test added to
`tests/test_sequence_orchestrator.py` confirming `html_body` actually
reaches `sender.send()`.

**One regression caught and fixed before it shipped:** adding the
`pixel_url()` call to `dispatch_touch()` broke two *other*, pre-existing
test files (`test_sequence_orchestrator.py`, `test_sequence_content_flow.py`)
that stub `unsubscribe_url` the same way but never anticipated a second
secret-requiring call being added to the same function — both now also stub
`pixel_url`. Found by running the full suite immediately after the change
(11 failed, not the expected 10) rather than only the directly-related test
file.

**Verification this tick:** full regression 1622 passed (+25 from before
this section), same 10 pre-existing failures, zero new.

**S-11 build completed (this tick):**
- `src/services/events.py` — new `sales_reply_sent`/`sales_meeting_link_sent`
  event types, deliberately distinct from `outbound_touch_dispatched` so a
  manual rep reply never pollutes the cold-sequence digest metrics.
- `src/services/slack/listeners.py`:
  - `sales_reply_content_blocks()` — added the "Book Meeting" button
    (previously absent entirely).
  - `handle_reply_in_thread()` — now parses `action["value"]` (inbound_id/
    contact_id/client_id) and merges it into the modal's `private_metadata`;
    previously discarded, leaving the submit handler with only
    `{channel, ts}` and no idea who to email.
  - `handle_reply_thread_modal_submit()` — now actually emails the prospect
    (`get_active_mailbox_for_client` + `build_email_sender().send(...,
    in_reply_to=<original message id>, list_unsubscribe_url=...)`, the same
    primitives `sequence_orchestrator.dispatch_touch` already uses) before
    posting the sent copy into the Slack thread. Idempotent on
    `inbound_messages.status='RESPONDED'` (surfaced as a modal error via
    `ack(response_action="errors", ...)`, matching this repo's own
    `handle_revise_submit` pattern for view-submission validation errors,
    not a silent no-op). Opt-out check added (resolved open question, see
    below) via a new `_is_opted_out()` helper. On success, marks
    `RESPONDED` — the exact status `mailbox_dispatcher.py`'s rolling-24h
    capacity subquery already recognizes, so a manual reply now correctly
    counts against the sending mailbox's daily cap without needing a new
    dispatch table.
  - New `handle_book_meeting()` — resolves a real link via
    `resolve_booking_link()` (Blackink's own internal-sales-demo calendar,
    not a client's owner-booking flow) and emails it through the same
    mailbox/send path; fails visibly (ephemeral Slack error) rather than
    silently no-op-ing when no default sales-booking connection is flagged
    (a separate, pre-existing gap this task doesn't fix, just doesn't hide).
  - New `_load_inbound_message()`/`_is_opted_out()` helpers shared by both
    handlers.

**Open question resolved (plan's stated default, applied as stated, no
correction received):** reply-sends check `contacts.is_opted_out` before
sending (a real compliance-adjacent gap — the same card has a "Mark
Opt-Out" button right next to Reply in Thread) but skip the full
cold-outbound `compliance_gate.py` (DNC/non-poach/cooldown don't apply to
answering someone who just emailed us).

**Test changes:** new `tests/test_sales_reply_send.py` (9 tests: modal
value-plumbing, send success + thread post, idempotent-on-RESPONDED,
opted-out block, missing-card-context fails visibly, mailbox-capped
surfaces as a modal error, Book Meeting success, Book Meeting fails visibly
with no link, unauthorized-user rejection). 1 new test added to
`tests/test_slack_listeners_3_1_2.py` confirming the Book Meeting button is
present on every card. Both real column names used in the new raw SQL
(`contacts.is_opted_out`, `inbound_messages.responded_at`) independently
confirmed against their migrations, not assumed.

**Verification this tick:** full regression 1632 passed (+10 from S-8's
count), same 10 pre-existing failures, zero new.

**`testing-verification` pass completed 2026-09-10.** Spun up an isolated
disposable Postgres (`blackink-s8s11-verify`, torn down after), ran the full
documented migration sequence, and exercised the REAL code paths (not
mocks) end to end:

| Flow | Real-DB result |
|---|---|
| Pixel open | `pixel(token=...)` against a minted token → real `email_opened` row with correct `dispatch_id`; a second hit with the same token wrote zero additional rows (dedup confirmed against real Postgres, not just the mock-based unit test). |
| Click | `click(token=...)` → real 302 to the token's own embedded URL, real `email_clicked` row. |
| Reply-send | Seeded a real `inbound_messages` row + a real mailbox/sending-domain pair, called `handle_reply_thread_modal_submit` for real → mailbox correctly resolved via the actual `get_active_mailbox_for_client` query, sender called with the right `to_address`/`in_reply_to`, `inbound_messages.status` flipped to `RESPONDED` with a real `responded_at`, `sales_reply_sent` event written. |
| Idempotency | Same modal submitted a second time against the now-`RESPONDED` row → zero additional sends, correct `ack(response_action="errors")`. |

**Two more instances of the pre-existing `apply_stl_cadence.py` grant bug
surfaced during this verification** (already flagged during S-1's
testing-verification, now confirmed broader): that migration creates
**both** `stl_cadence_dispatches` (used by `mailbox_dispatcher.py`'s
capacity query, which the real reply-send path calls) **and** the
`inbound_messages.cadence_state` columns with **zero `GRANT` statements**
anywhere in the file — `blackink_app`/`blackink_system` have never had
privileges on `stl_cadence_dispatches`. Worked around in the disposable
test DB only (a manual `GRANT`); not fixed in this diff (unrelated to
S-8/S-11's own scope) — flagging again, more concretely, since verifying
S-11's real send path is what surfaced it a second time.

Boundary/concurrency: pixel/click dedup logic already has dedicated unit
tests (`test_email_tracking_router.py`); no new time-based boundary in this
section (unlike S-1/S-2) — the dedup key is `dispatch_id` identity, not a
threshold. DoD line-by-line: all requirements from the plan's §4 tables
(S-8 R1-R5, S-11's 5 requirements) have both a unit test and, for the two
end-to-end flows above, live-DB confirmation.

Full regression re-run after cleanup: 1632 passed, same 10 pre-existing
failures, zero new — confirmed a second time post-verification, not just
immediately after the code change.

**Section status: S-8 and S-11 fully built, unit-tested, and live-verified.**
`code-review` for this whole section is the next step.

**`code-review` pass completed 2026-09-10 — 3 findings, all fixed and
re-verified:**

1. **(Critical)** `handle_reply_thread_modal_submit` sent a real outbound
   email with **zero `approver_authorized()` check**, despite its own
   sibling `handle_book_meeting` (written in the same diff) correctly
   having one, and despite every other consequential Slack action in this
   repo (work-order decisions, opt-out) being gated the same way — fixed:
   added the identical `approver_authorized(user_id, client_id=client_id)`
   check, rejecting with `ack(response_action="errors", ...)` before the DB
   session even opens. Re-verifying this surfaced that 2 of the 5 existing
   tests for this handler (`test_reply_send_is_idempotent_on_already_responded`,
   `test_reply_send_capped_mailbox_surfaces_as_modal_error`) were passing
   for the wrong reason — coincidentally matching the auth-block's identical
   error shape rather than genuinely exercising their intended path. Both
   fixed (added the authorized-user mock plus, for the capped-mailbox test,
   a real assertion that the error text actually mentions capacity), 2 more
   updated with the same mock, and one new dedicated test added
   (`test_reply_send_rejects_unauthorized_user` — confirms `get_db_context`
   is never even called when unauthorized).
2. **(Important)** The new `email_replied` producer
   (`inbound_ingest.py`) had no per-dispatch dedup, unlike `email_opened`/
   `email_clicked` added in the same diff — a prospect replying twice in the
   same thread would double-write `email_replied` and inflate the digest's
   `reply_rate_pct`. Fixed: added the same `already_logged_for_dispatch()`
   check before the write. New test:
   `test_email_replied_is_deduped_per_dispatch_id`.
3. **(Important)** TOCTOU race in the pixel/click dedup's check-then-insert
   (each endpoint had its own local `_already_logged()` doing a bare
   SELECT with no DB-level backstop) — two concurrent hits on the same
   `dispatch_id` could both pass the check before either INSERTs. Fixed two
   ways: (a) consolidated the check into one shared function,
   `src/services/events.py::already_logged_for_dispatch()`, used by both
   `email_tracking_router.py` and `inbound_ingest.py` instead of a
   duplicated per-file implementation; (b) added a real database-level
   backstop, `migrations/apply_events_dispatch_dedup_index.py` — a partial
   unique index on `(client_id, event_type, payload->>'dispatch_id')` for
   exactly the three deduped event types. A race-losing `log_event()` call
   hits this constraint, but per `events.py`'s own documented contract
   `log_event()` never raises — it catches the `IntegrityError`, buffers
   the write in memory, and lets it harmlessly retry-then-evict rather than
   corrupt data or crash the request. New migration registered in
   `CLAUDE.md`, both `.github/workflows/{tests,tenant_leakage_nightly}.yml`,
   immediately after `apply_events.py`.

**Re-verified after fixes:**
- `tests/test_sales_reply_send.py` — 10/10 pass (was 9; +1 new).
- `tests/test_email_tracking_router.py` — 8/8 pass (confirms the
  consolidation refactor didn't change behavior).
- `tests/test_inbound_ingest.py` — 11/11 pass (was 10; +1 new dedup test).
- Full regression: **1634 passed**, same 10 pre-existing failures
  (`test_calendar_subscription_renewal.py` x2, `test_ghost_shopper.py` x2,
  `test_no_show_prompts.py::test_token_round_trips`, `test_schema.py` x3,
  `test_show_rate_reminders.py::test_30min_reminder_fires_within_60s_of_the_30min_mark_and_sends`,
  `test_slack_listeners.py::test_log_outcome_opens_modal_for_assigned_closer`)
  — exact same test names as every prior session baseline, zero new
  failures from this round of fixes. (41 collection errors in this same run
  are live-DB connectivity failures — no local Postgres reachable in this
  environment right now — unrelated to any code in this diff.)

**S-8/S-11 is fully done** — all 3 findings resolved, zero unresolved
Critical/Important findings. Ready for `blackink-pr` once bundled with S-1
and S-2 into the single consolidated PR.

---

## Section 4 — Week 0-2 audit crosscheck: D-6, D-7, `@Blackink` mention router

**Task:** user asked to crosscheck every open row in
`docs/Blackink_Week0-2_Implementation_Audit_10-09-2026_2.md` against the
actual current codebase, and implement whatever was a genuine gap (not
blocked by a missing external vendor/dependency). Full crosscheck done by
reading the real code (not re-trusting the audit's own text) for every row.

**Crosscheck result — not gaps at all, audit is stale, no action taken:**
- **W-6 (DNC scrubbing)** — `src/tasks/dnc_refresh.py` is a real, cron-registered
  (1st of month) monthly Tracerfy batch that populates
  `contacts.dnc_clean`/`dnc_checked_at` for every never-checked-or-stale
  contact with a phone; `compliance_gate.py::_check_dnc` correctly reads
  that cache (ABSTAIN on missing/stale, never a false PASS). The module's
  own docstring already documents exactly why a live per-contact
  `TracerfyDncProvider` is deliberately never wired into the synchronous
  gate (a real submit/poll round trip can take up to 10 minutes and can't
  block a live send path). This is the correct architecture already built,
  not an open dev-wiring item.
- **2-contacts-per-company schema cap** — already enforced via
  `UniqueConstraint("company_id", "contact_role_type", name="uq_contacts_company_role")`
  with exactly 2 allowed roles (`src/core/models.py:343`).
- **3.0.2 A / 3.0.6 #2 (Vera), 3.0.2 C (Cora throttle)** — done this session,
  Sections 1-2 above.
- **Hunter droplet-vs-cron** — the audit's own text says "no action needed."
- **`county_slug` nullable at staging** — deliberate design (enforced later
  at promotion, `promotion_sweep.py:153-154`), not a defect.

**Crosscheck result — genuinely blocked on a missing external vendor/key,
flagged not built (confirmed by reading `config/settings.py`, not assumed):**
- **Hunter registered-agent lookup** (3.0.2 D / 3.0.6 #4) —
  `src/agents/hunter/registered_agent_provider.py` always returns `None`;
  its own docstring says "no vendor contracted for week 0." No
  OpenCorporates/SunBiz API key anywhere in settings.
- **B1 — email verification** (3.0.5) — `email_verification_vendor_api_key`
  exists in settings but is `default=None`, and no real
  `EmailVerificationProvider` implementation exists anywhere (only the
  stub). Blocks the quarantine gate's `cleared` state from ever being
  reachable — same class of gap as Hunter's, confirmed the same way.
- **`#blackink-economics`** (3.0.3) — this is not a small wiring task; per
  the blueprint (`Implementation_Blueprint_v2.md:1309-1313`) it's a full
  "Economics Governor" agent mission (per-tenant cost ledgers, ad-spend
  wallet caps $150-300/client, contribution margins, dunning). No Google
  Ads/Meta Marketing API credential exists anywhere in `config/settings.py`
  — wallet-cap tracking has no data source. Flagged rather than building a
  hollow producer that can't compute the thing the channel is named for.
- **Reuse Ledger** (3.0.1 / 3.0.6 #7) — turned out to already exist:
  `docs/reuse_ledger_week0.md`, 406 lines, git-tracked since commit
  `f3bca69` (not the excluded `Tasks/reuse_ledger_week0.md` path the audit
  scanned). Real, but self-admittedly stale ("Dev 1-3 sections... awaiting
  each dev's test counts... for final Gate 1 sign-off") and scoped to Week
  0 only — doesn't cover Week 1/2's work. Not a "no artifact" gap as the
  audit claims; flagged as an update-not-create decision for the
  client/lead, not built blind in this session.

**Crosscheck result — genuine gaps, no external blocker, built this tick:**

1. **D-6 — CI path filter too narrow.**
   `.github/workflows/compliance_gates_ci.yml`'s `pull_request` trigger was
   filtered to 8 named files — a PR adding a new SMS send path anywhere
   else would never run this gate. Fixed: removed the filter (`pull_request:
   {}`, matching `tests.yml`'s own unfiltered "every PR" posture); `push`
   trigger unchanged.

2. **D-7 — `sms_dispatch.py` re-implements the cold-SMS rule instead of
   using the named gate.** Traced deeper than the audit's own framing: this
   wasn't just a duplicated implementation risk, it was an ACTIVE,
   currently-shipped bug. `campaign_readiness_gate.py::evaluate_full_readiness()`
   (which `sms_dispatch.dispatch_sms()` calls) derived "engaged" from
   `contacts.inbound_sms_count`/`contacts.booked_appointment_id` —
   denormalized columns that a full-repo search confirmed **no production
   code path ever writes** (only this file's old SELECT and test fixtures
   ever referenced them). Since those columns are always `0`/`NULL` in
   production, `is_engaged()` was always called with `(0, None)`, so
   `evaluate_full_readiness()` could never yield `TRANSACTIONAL_SMS_ONLY`
   for ANY contact — `dispatch_sms()` was silently guaranteed to raise
   `ColdSMSBlockedError` for every contact, regardless of real engagement
   (currently moot only because no SMS vendor is contracted yet —
   `StubSmsProvider` raises `NotImplementedError` unconditionally — but a
   real, live-blocking bug the moment one is). Fixed: `evaluate_full_readiness()`
   now derives both signals via `cold_sms_gate.get_inbound_sms_count()`/
   `get_booked_appointment_id()` — the events ledger, `cold_sms_gate.py`'s
   own documented "source of record" — instead of the dead columns. One
   source of truth for the predicate now, matching the audit's own
   "consolidate" recommendation, plus a real correctness fix behind it.
   `is_engaged()`'s own signature/logic is unchanged.
   - Updated 7 live-DB tests in `tests/test_tenant_isolation.py` that
     previously set `contacts.inbound_sms_count`/`booked_appointment_id`
     directly (which no longer has any effect on the real code path) to
     instead seed real `sms_inbound`/`meeting_booked` events via two new
     helpers, `_seed_inbound_sms_event()`/`_seed_meeting_booked_event()`;
     extended `_cleanup_compliance_events()`/`_cleanup_cold_sms_events()`
     to also clean up those seeded events.

3. **`@Blackink` mention/intent router** (3.0.3) — no `app_mention`/
   `@app.event(...)` handler existed at all; macro pipeline queries were
   only ever served by the scheduled 8am `daily_digest.py` cron, never
   conversationally. Built a deliberately small first router in
   `src/services/slack/listeners.py`: `@app.event("app_mention")` →
   `handle_app_mention()`, matching on keywords (`pipeline`/`digest`/
   `status`/`numbers`/`metrics` → reuses `daily_digest.build_digest_text()`
   for an on-demand pull of the SAME computation the cron already posts,
   not a re-derived one; `halt` → reuses `halt_service.get_active_halts()`,
   same data `/blackink-halt status` already exposes; anything else → a
   help message). "halt" is checked before the digest keywords since "halt
   status" would otherwise also match "status". Deliberately scoped small
   — not the full "command & intent dispatcher" the blueprint's language
   implies; a genuinely open-ended conversational surface is its own task,
   not silently expanded into this one.

**Test changes:** new `tests/test_slack_mention_router.py` (13 tests:
digest-keyword variants including case-insensitivity, thread-ts handling
both fresh and already-threaded, halt-status with/without active halts,
unrecognized mention → help text, bot-mention-prefix stripping regression,
empty-mention-after-bot-id → help).

**`testing-verification` pass completed 2026-09-10 (autonomous continuation
— closing the exact gap flagged above as unverified).** Spun up a fresh
disposable Postgres (`blackink-d7-verify`, port 5556 — not the shared
`blackink-local-pg` dev container; a stray, never-started leftover
container from an earlier tick occupying the same port mapping was removed
first since it held no data and was never started). Ran the full 60-migration
sequence clean, then the 7 updated `test_tenant_isolation.py` tests.

**This surfaced a second, real bug in the D-7 fix — exactly what this step
exists to catch.** 3 of the 7 tests failed with a live
`ck_sms_dispatch_log_not_cold` CHECK-constraint violation:
`sms_dispatch.py::dispatch_sms()` has its OWN separate read of
`contacts.inbound_sms_count`/`booked_appointment_id` (a third site, missed
by the first pass — `campaign_readiness_gate.py` was the only one fixed) for
the `sms_dispatch_log` audit-snapshot INSERT. Once the readiness gate
correctly says ENGAGED (via a real events-ledger row) while this second,
still-unfixed site still reads the dead `contacts` columns (always 0/NULL),
the two disagree — and the DB's own defense-in-depth CHECK constraint
correctly rejected the resulting inconsistent row. Fixed: `dispatch_sms()`
now also derives both values via `cold_sms_gate.get_inbound_sms_count()`/
`get_booked_appointment_id()`, called on the same `outbox_session` right
before the INSERT — one source of truth across all three sites now, not two
of three.

**Verification, in order:**
1. `tests/test_tenant_isolation.py -k "readiness or dispatch_sms or cold_sms or dnc"` — 3 failed (the bug above), 20 passed.
2. Applied the `sms_dispatch.py` fix.
3. Same filtered run — **23/23 pass.**
4. Full `tests/test_tenant_isolation.py` — **38/38 pass** (no regressions elsewhere in the live suite).
5. `tests/test_sms_dispatch.py` (unit, mocked) — 9 tests newly failed on
   the fixed code (`_FakeOutboxSession`'s mock had no fetchone() support for
   the two new event-ledger queries `dispatch_sms()` now makes on the outbox
   session). Fixed the fixture: `_FakeResult` gained `.fetchone()`,
   `_FakeOutboxSession.execute()` now branches on SQL text to answer the two
   new query shapes distinctly from the INSERT's own `.scalar()`. Re-ran:
   **21/21 pass.**
6. Disposable container torn down after.
7. Full non-live regression: **1647 passed**, same 10 pre-existing failures,
   zero new.

**`code-review` pass completed 2026-09-10 (autonomous continuation).**
Confirmed production wiring for real (`src/api/main.py:52` imports
`listeners.py` specifically to register its `@app.*` decorators, so the new
`@app.event("app_mention")` handler is genuinely reachable, not an orphan).
Confirmed `events` RLS scoping (`config/tenant_policies.py:41`,
`{"mode": "direct", "column": "client_id"}`) and the new mention-router
code for injection/security issues — none found (keyword matching only,
`text()` binds unchanged elsewhere).

**One finding, Important, NOT fixed — flagged for a decision, not silently
resolved:** `cold_sms_gate.py`'s own module docstring explicitly states its
queries are designed to run under a **BYPASSRLS system session** ("the gate
must see events across all channels regardless of which client initiated
them"). Before this session, `get_inbound_sms_count()`/
`get_booked_appointment_id()`/`assert_not_cold_sms()` had **zero production
callers anywhere** — a fully-built, tested, orphaned module (itself worth
noting: the exact "helper nothing calls" class of finding this process
exists to catch, just discovered by fixing D-7 rather than by review
finding it cold). This session's D-7 fix wires them in via the regular
RLS-scoped app session (`campaign_readiness_gate.py`'s `session` parameter;
`sms_dispatch.py`'s `outbox_session`) — matching how every OTHER per-contact
check in both gates already works, but contradicting `cold_sms_gate.py`'s
own stated BYPASSRLS design intent. Practical effect: if a contact's
inbound-SMS event was logged while the contact's company was owned by a
DIFFERENT `client_id` (`county_allocation_reassessment.py`'s 30-day
reassignment), the new owning client's session won't see that older event
row and could undercount engagement — a contact that genuinely replied once
could evaluate as cold for the new tenant. Likely the architecturally
*correct* choice (consistent RLS-scoped, per-tenant checks throughout both
gates, matching `_check_dnc`'s own pattern), but it's a real contradiction
of the module's own documented intent and deserves an explicit decision
(update the docstring to match, or change the call sites to BYPASSRLS) —
not something to resolve unilaterally mid-autonomous-tick.

**Approve/Block:** no Critical findings; one Important finding open (above,
a design-intent question, not a wrong-answer bug) — **Blocked** on that one
decision per the code-review skill's own rule (no Critical/Important stays
unresolved). Everything else in this section (D-6, D-7 across all three
read sites, mention router) is built, unit-verified, and live-DB-verified
with zero other findings.

**Finding resolved 2026-09-10 — user decision.** Pointed to
`docs/SOURCE_OF_TRUTH_RECONCILIATION.md` §1.3, which settles it decisively:
**"No SMS this year"** is a hard 2026 client directive (W1-4), and that same
doc explicitly says `cold_sms_gate.py`/`sms_quiet_hours.py` "remain correct
as blocks but have nothing to gate this year." Since `StubSmsProvider`
unconditionally raises `NotImplementedError` regardless of engagement
(no vendor contracted), the RLS-vs-BYPASSRLS edge case has no live
consequence this year. Kept the RLS-scoped implementation (consistent with
every other check in both gates) and updated `cold_sms_gate.py`'s own
docstring to state this as the accepted, current design — including an
explicit note to revisit if a real SMS vendor is ever contracted. Re-verified:
`test_cold_sms_gate.py`/`test_campaign_readiness_gate.py`/
`test_sms_dispatch.py` — 59/59 pass (docstring-only change).

**Section status: fully done.** D-6, D-7 (all three read sites), and the
mention router are code-complete, unit-verified, live-DB-verified, and
code-reviewed with zero unresolved findings. Hunter registered-agent, B1
email verification, and the full Economics Governor mission remain
explicitly out of scope (external vendor blockers, confirmed by reading
`config/settings.py`, not assumed). The Reuse Ledger update remains a
client/lead decision, not built this session. **Ready for `blackink-pr`.**

---

<!-- Add one section per task below, in the same format, as work proceeds. -->
