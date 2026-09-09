---
name: blackink-review
description: End-to-end post-task review — completeness, correctness, security, edge cases, performance, and this repo's own invariants. Use after finishing ANY implementation task in this repo, before calling it done.
---

# Post-task review (Blackink)

Run this after finishing an implementation task, before reporting it done,
opening a PR, or handing it back to the user — regardless of how small the
task looked. Read the actual diff/new files, not just a summary of what you
intended to do. If a code-review instruction elsewhere in the conversation
already specifies an output format, follow that; otherwise report findings
with `ReportFindings` if available (most-severe first, empty list if none
survive), or a plain ranked list if not.

Work through Step 0, then all seven sections below. Skipping a section
because "it obviously doesn't apply" is itself a finding-shaped risk on a
system this size — say so explicitly (one line) rather than silently
omitting it.

## Step 0 — run the tests now

Before anything else: run the full test suite (not just new tests) and
record pass/fail. If anything new is red, stop and fix it before reviewing
further — don't review code you haven't confirmed still passes. If a
failure looks pre-existing, verify that (`git stash`, or re-run against the
prior commit) rather than assuming it. Never report "all green" without
having actually just seen it.

## 1. Scope completeness

- Re-read the original request/plan/DoD line-by-line against what actually
  landed. List anything asked for that is missing, stubbed, or silently
  narrowed — including a test that was supposed to exist and doesn't.
- If part of the scope was deliberately deferred or left out, is that stated
  explicitly (to the user, in a docstring, in CLAUDE.md) — or does it read as
  finished when it isn't?
- Check for scope CREEP too: code, abstractions, or config added beyond what
  the task actually required. Flag speculative generality (unused
  parameters, a feature flag nothing reads, a helper with one call site) as
  a finding, not just as a style note.
- Every claim made to the user ("all tests pass", "verified against real
  Stripe", "idempotent") should be re-checked against what was ACTUALLY run
  (Step 0 covers the test claim; re-verify any other claim the same way).

## 2. Security / vulnerabilities

For a system moving money and holding tenant data, this outranks everything
below it except a broken build — review it before edge cases and
performance, not after.

- Injection: every runtime query uses parameterized `text()` binds, never
  raw string interpolation of user input into SQL, a shell command, or a
  URL.
- SSRF: any code that fetches a URL from data an external party (not just
  the operator) controls validates and PINS to a resolved public IP (not
  just checking the hostname — DNS rebinding), enforces `https`-only where
  applicable, a connect/read timeout, and a body-size cap.
- Secrets: no secret value (API key, password, token) appears in a log
  line, an error message, a committed file, or is echoed back in this
  session's own output. Confirm `.env*` stays gitignored and nothing new
  reads `os.environ` directly instead of the settings module.
- AuthN/authZ: every new endpoint/handler checks who is allowed to call it
  and on what data (not just that a token is present, but that the token's
  own scope covers the resource being touched) — check for an IDOR-shaped
  bug (an id taken from the request used to look up a row without also
  checking ownership/tenant).
- Deserialization / eval: no `eval`, `exec`, `pickle.loads` on
  untrusted input, no stored predicate/expression string ever passed to
  a query builder or `text()` unescaped.
- Tenant isolation (this repo's own top invariant): every new tenant-bearing
  table is registered in `config/tenant_policies.py`; every child table FKs
  on the composite `(client_id, parent_id)` pair, not a bare parent id; no
  new code imports `get_system_db_context()` from `src/api/`.
- New dependencies: any package added to `requirements.txt`/`package.json`
  this task is a new attack surface and a new license to account for — name
  it explicitly, check it's from a maintained/reputable source, and note if
  it pulls in transitive deps you haven't looked at. Don't wave a new
  dependency through unexamined because "it's just a helper library".

## 3. Correctness / remaining bugs

- Walk every new/changed function for the classic classes: off-by-one at a
  stated boundary (a `<` vs `<=`, an exact threshold value), sign errors,
  wrong operator precedence, a swallowed exception that should propagate, a
  return value that's checked in some call sites but not others.
- Check every SQL statement's WHERE/JOIN for a wrong column, a missing
  tenant/status filter, or a predicate that silently matches more or fewer
  rows than intended (test it against a boundary row if there's any doubt).
- Check every dataclass/typed-dict field is actually populated on every
  construction path — a field left `None`/default on one branch but read as
  non-optional elsewhere is a live bug waiting for that branch.

## 4. Side effects and edge cases

- What happens on a RETRY of this exact operation — is it idempotent, or
  does a second call double-write, double-charge, double-notify? Check
  idempotency keys, `ON CONFLICT` clauses, and UNIQUE constraints are
  actually load-bearing, not just present.
- What happens under CONCURRENCY — two workers claiming the same row, a race
  between a flag-check and a flag-flip (is the check-then-act atomic, e.g.
  in the same transaction/row lock, or is there a window)?
- What happens on a CRASH mid-operation — between two writes that should be
  atomic, is there a state that looks "half-done" and never resolves? Is
  there a lease/claim timeout so a crashed worker's claimed row becomes
  reclaimable?
- What does an EMPTY/NULL/zero/negative input do — an empty list, a NULL
  optional column, a zero-amount charge, a negative count? Does the code
  assume a collection is non-empty or a value is always positive?
- Does this change have effects on OTHER already-shipped features that share
  the touched table/module/config — a new column with a NOT NULL default
  that breaks an existing INSERT list; a new required field that an existing
  caller doesn't pass; a shared function whose behavior changed for its
  other callers?
- If a background/sweep/scheduled job is involved: what happens if it's
  scaled to zero, restarted mid-run, or run twice concurrently on the same
  claim query?

## 5. Performance / complexity

- State the Big-O of every new loop/query in the hot or frequently-run path
  (a request handler, a sweep that runs every N seconds) — flag any
  accidental O(n²) (a loop doing a per-item DB query instead of a batched
  one; a nested loop over the same collection).
- Every claim query against a growing table has an index that actually
  covers its WHERE/ORDER BY — check the migration added the matching index,
  not just the column.
- A sweep/batch job has a `LIMIT`/claim size bound — nothing should attempt
  to load or lock an unbounded number of rows in one transaction.
- Any N+1 pattern: a loop that issues one query per iteration where a single
  JOIN/batched IN-query would do.
- Any unbounded growth: a list/buffer that grows without a cap (check
  in-memory buffers like `events.py`'s retry buffer for their stated bound),
  a cache with no eviction, a log line inside a hot loop.
- Any new sweep or scheduled job that calls a paid external API (Stripe,
  Google Places, an LLM endpoint) has an explicit rate/budget cap — no
  unbounded per-tick spend. Cross-reference `owner_visibility_sweep.py` as
  the standing example of why this matters.

## 6. Observability

- Does a new failure path (a BLOCKED row, a caught exception, a rejected
  request) actually log enough to diagnose it in production — which
  entity/id, why, and what happens next — or does it fail silently into a
  status column nobody alerts on?
- Does a new sweep/worker log its own tick summary (count processed,
  count failed) the way the existing sweeps do, so a stuck worker is
  visible in logs rather than only inferred from a growing backlog?
- Is a new alert-worthy condition (an alert-threshold crossing, a
  fail-closed rejection) actually emitted as an `events` row or a log line
  at a level someone monitors — not just a `logger.debug` nobody reads?
  This is distinct from the secrets-in-logs check in §2 — the question here
  is "is there enough signal", not "is there too much".

## 7. This repo's own invariants (Blackink-specific)

- **Migrations**: every DDL statement idempotent (`IF NOT EXISTS` /
  guarded `DO $$` for `CREATE TYPE`/`ADD CONSTRAINT`); new migration added to
  BOTH `.github/workflows/tests.yml` and `tenant_leakage_nightly.yml` in the
  same relative position (before `apply_rls_policies.py` if tenant-bearing),
  AND to CLAUDE.md's Common Commands — `tests/test_migration_coverage.py`
  checks this mechanically, run it. Every new table has explicit `GRANT`s
  for both runtime roles including `GRANT USAGE ON SEQUENCE ...` for a
  serial PK (a missing grant is silent until the first live INSERT under
  that role).
- **Migration rollback path**: this repo has no down-migrations (no
  Alembic) — state plainly, for every new migration, what an operator would
  actually have to do to undo it against the live server (a manual
  `DROP TABLE`/`DROP COLUMN` script, or "additive only, safe to leave"). A
  migration that drops or renames an existing column/table needs this
  stated explicitly before it's run against anything but the disposable
  local DB — that's an unrecoverable-without-a-backup action, not a routine
  one.
- **Money-moving code confinement**: exactly one module per financial
  subsystem calls a money-moving Stripe API; a charge/credit function
  re-reads its own amount/precondition from the DB rather than trusting a
  caller argument; idempotency keys are pinned to stable identifiers with no
  timestamp; a `BLOCKED`/`FAILED` transient-vs-structural distinction is an
  explicit reason code, not inferred from status alone. This repo has
  shipped the "BLOCKED = forever excluded from retry" bug three times
  (settlement installment 2, then installment 1, then it had to be
  generalized) — the closest existing structural test is
  `tests/test_settlement_clawback_pms_outage.py`'s alert-at-five-attempts
  test, but there is NO repo-wide test enforcing this pattern for a new
  subsystem yet; if this task adds one, it needs its OWN version of that
  test (claimable-after-retry, exactly-one-alert, never-FAILED_PERMANENT),
  not just a comment promising the behavior.
- **Fail-closed defaults**: a new external integration/rate-limiter/feature
  flag defaults to the SAFE state when unconfigured or unreachable; a
  tri-state vendor result (`True`/`False`/`None`) is never collapsed to a
  binary.
- **Structural proof over code-review claims**: a DoD line phrased "no X
  predicate exists" or "confirmed via code review" should have a test that
  actually greps/introspects the code (see
  `tests/test_no_upfront_charge_paths.py`), not rest on the reviewer's word.
  A boundary named in a DoD line (a threshold, a time window) has a test AT
  the boundary, not just comfortably inside it.
- **Deviations from a printed spec/blueprint**: every deviation traces to a
  named, quoted contradiction in a nearby comment/docstring — never a silent
  gap.

## Reporting

For each finding: file + line, a one-sentence summary of the defect, and a
concrete failure scenario (what input/state causes what wrong behavior) — not
"this could be an issue". Rank most-severe first. An empty findings list
after actually working through Step 0 and all seven sections is a valid,
complete result — don't manufacture a finding just to have something to
report.
