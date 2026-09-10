# S-1 — Vera health job + settlement/billing halt consumer

**Task:** W0 §3.0.2 A + Week-0 Acceptance Criterion #2.
**Audit item:** Dev Item S-1 (`docs/Blackink_Week0-2_Implementation_Audit_10-09-2026_2.md`).
**Status of this document:** task-analysis output. No source file changed.

---

## 1. What the task actually asks for

Blueprint §3.0.2 A (`docs/Implementation_Blueprint_v2.md:134`), verbatim:

> **A. Vera Silent-Zero Remediation:** The data-reconciliation layer is
> refactored to enforce strict tri-state logic. When an external integration
> returns missing values, the system explicitly returns `UNKNOWN` or `ABSTAIN`.
> **Downstream settlement routines halt automatically and alert administrators
> rather than assuming zero payable activity.**

Week-0 AC #2 (`:190`):

> **Defect-Free Health Reporting:** Vera health jobs output `UNKNOWN` or
> `ABSTAIN` states on missing inputs with zero silent zero returns.

The first sentence and AC #2 are **already built and tested**. The bolded
sentence is not built at all.

## 2. What exists today (verified, not assumed)

| Piece | State | Evidence |
|---|---|---|
| Tri-state result type | Built, correct, enforced at construction | `src/agents/vera/health_result.py:46-53` raises if a value is supplied for UNKNOWN/ABSTAIN |
| `check_pm_feed` | Built. VALUE on success, ABSTAIN on DB error, never UNKNOWN | `src/agents/vera/checks/pm_feed.py:26-62` |
| `check_campaign_feed` | Built. UNKNOWN when Instantly unconfigured, ABSTAIN on API failure/None, VALUE otherwise | `src/agents/vera/checks/campaign_feed.py:37-89` |
| `check_pipeline_health` | Built. VALUE on success, ABSTAIN on DB error | `src/agents/vera/checks/pipeline.py:32-90` |
| `run_health_checks()` | Built, aggregates all three, deliberately does not collapse to a boolean | `src/agents/vera/runner.py:32-57` |
| Unit tests | **Comprehensive** — 24 tests incl. both runner paths. (The audit's own caveat says "test coverage was not assessed", so it did not credit this.) | `tests/test_vera_health.py` (302 lines) |
| **A caller for `run_health_checks()`** | **Does not exist** | zero references to `vera` outside `src/agents/vera/` (grep over `src/ config/ migrations/ scripts/`) |
| **Persistence of a health result** | **Does not exist** | no table, no column, no event type |
| **A consumer that halts on ABSTAIN** | **Does not exist** | `src/tasks/settlement_sweep.py` and `src/tasks/billing_sweep.py` import no Vera module |
| **An alert to `#blackink-qa`** | **Does not exist** | no `post_notice(channel_key="qa", ...)` anywhere in the Vera or settlement/billing paths |

So the work is: a run surface, a persisted result, a gate, and an alert.
The checks themselves need no change.

## 3. Dependency check against the audit's blocker list (B1–B13)

**No external dependency. S-1 appears nowhere in B1–B13 and is buildable now.**

| Thing it needs | Available? |
|---|---|
| Postgres (`client_pm_books`, `raw_prospect_companies`, `companies`) | Yes — all three tables exist and are migrated |
| Slack `#blackink-qa` | Yes — registered (`config/slack_channels.py:46-48`); audit records Slack credentials and all six channel IDs as **Closed** (rev-2 blocker table) |
| Instantly key (for `check_campaign_feed`) | **No** (B8/B12) — and deliberately not needed: unconfigured Instantly is exactly the `UNKNOWN` path, which by decision D2 below does not halt |
| A client/business decision | Only one, non-blocking — see §7 Q-A |

## 4. Contradictions found, and how each is resolved

### C1 — Does `UNKNOWN` halt, or only `ABSTAIN`?

Blueprint `:134` pairs "returns `UNKNOWN` or `ABSTAIN`" with "settlement
routines halt", which reads as *both* halting. The audit's S-1 row says
"halts … when any check returns **ABSTAIN**".

**Resolved: ABSTAIN halts, UNKNOWN does not.** Two sources, in priority order:

1. **Existing code as source of truth** (priority 4, and the only source that
   defines these two states precisely). `health_result.py:16-19` says of
   ABSTAIN only: *"ABSTAIN must always block downstream consumers from
   treating the result as clean — same semantics as compliance_gate.ABSTAIN."*
   Of UNKNOWN it says (`:10-12`) *"the source is not configured … The check
   was deliberately skipped."* The blocking obligation is attached to ABSTAIN
   and to ABSTAIN only.
2. **Consequence test.** `check_campaign_feed` returns UNKNOWN whenever
   Instantly is unconfigured (`campaign_feed.py:44-50`), which is the
   permanent state today (B8/B12 outstanding). If UNKNOWN halted, **shipping
   this fix would halt every settlement and billing sweep from the moment it
   deployed** — a regression dressed as a safety feature.

UNKNOWN is still recorded on the run row and named explicitly in the log and
in the alert body, so it is never invisible. See §7 Q-A: this is the one
reading worth a client confirmation, and it is stated rather than assumed.

### C2 — Is "didn't run" a halt condition?

The audit says halt "when any check returns ABSTAIN" — silent on the case
where the health job produced no result at all.

**Resolved: yes, absence and staleness both halt.** Traced to the
consolidated Source of Truth (priority 1 — client comments), which addresses
this case directly and names it as the same defect class as this task:

> "Distinguish 'ran and found nothing' from 'ran and failed' from 'didn't
> run.' Three states currently collapsing into one — and note that the two
> sources scheduled and writing nothing are silent zeros by another name.
> **This is the same bug class as W0-2**, which means you now have a live
> production proof case for why that fix matters."
> — `Blackink_Source_of_Truth (2).md`, line 534 (W0-2 *is* this task)

and

> "Stale or missing required data returns `UNKNOWN`/`ABSTAIN`; cached
> degraded results are labelled, never falsely fresh." — line 355

A gate that treats "no health record" as healthy would reproduce, inside the
fix for silent zeros, the exact silent zero it exists to prevent. So the gate
has three halting reasons: `ABSTAIN`, `NO_HEALTH_RUN`, `STALE_HEALTH_RUN`.

This is a deliberate **extension beyond the audit's stated scope**, quoted
above rather than added silently.

### C3 — Reuse `relay_halts`, or a separate health gate?

`src/agents/relay/halt_service.py` already implements a durable halt.

**Resolved: do not reuse it.** Three reasons, all structural:

1. **Wrong blast radius.** `relay_halts` locks *outbound dispatch queues*
   (blueprint §3.0.2 B). §3.0.2 A scopes the halt to *settlement routines*.
   A health ABSTAIN must not stop outbound email.
2. **Wrong failure direction.** `_db_is_halted` fails **open** by documented
   design (`halt_service.py:182-187`: *"treating as NOT halted
   (fail-open)"*) so degraded infra cannot deadlock every worker. A financial
   gate must fail **closed**.
3. **Wrong release semantics.** A `relay_halts` halt clears only via an
   authorised HMAC Slack resume (`halt_service.py:196-201`, no auto-resume by
   design). A transient Instantly timeout would then require a human
   cryptographic resume before billing could resume — whereas a *health*
   reading is a recurring measurement that should clear itself on the next
   clean run. Recording it as a permanent incident is the wrong model.

### C4 — Run surface: cron, or the API process's worker threads?

The audit says "cron entry on Hetzner". `scripts/crontab.txt` holds exactly
one entry (`daily_digest`), while **both consumers already run as in-process
threads** from `src/api/main.py::_start_background_workers` (`:133-142`).

**Resolved: register in `_start_background_workers()`.** The health producer
belongs in the same process and reliability class as the consumers it feeds;
putting it in cron while the sweeps run in the API process means a Vera
outage and a sweep outage are independent events, and the staleness gate then
halts billing for a reason unrelated to billing. `CLAUDE.md`'s own
Architecture section documents `_start_background_workers()` as this repo's
run surface for sweeps.

A comment is added to `scripts/crontab.txt` naming where the job actually
runs, so nobody adds a duplicate cron entry later. Low-stakes and reversible.

### C5 — Where does the gate live: a wrapper, or inside each sweep?

Both consumers have **two** live entry points: the `main.py` threads
(`:133-142`) and their own `python -m` `__main__` blocks
(`settlement_sweep.py:92-96`, `billing_sweep.py:195-200`), the latter
documented in `CLAUDE.md`'s Common Commands. **Resolved: the gate call goes
inside each of the seven sweep functions**, which is the only place that
covers both. A wrapper would cover one and leave the other unguarded.

## 5. Design decisions

| # | Decision | Why |
|---|---|---|
| D1 | Persist each run to a new `vera_health_runs` table; the gate reads the latest row rather than calling `run_health_checks()` inline | (a) `check_campaign_feed` makes a live Instantly HTTP call — 7 sweeps × 60s ticks would hammer a third party and make each sweep's latency depend on it; (b) C2's "didn't run" state is only *representable* if runs are recorded; (c) an inline call leaves an operator nothing to inspect after the fact |
| D2 | ABSTAIN halts. UNKNOWN records + surfaces but does not halt | C1 |
| D3 | Halting reasons: `ABSTAIN`, `NO_HEALTH_RUN`, `STALE_HEALTH_RUN` | C2 |
| D4 | `vera_health_runs` is **not** tenant-bearing: no `TENANT_POLICIES` entry, no RLS | Every check is platform-wide (`pm_feed` counts across all clients, `pipeline` counts all staging rows, `campaign_feed` is Instantly-account-wide). Same class as `relay_halts`, whose migration states this explicitly (`apply_relay_halts.py:3-5`). The migration will carry the same "do NOT add to tenant_policies" docstring so the omission is visibly deliberate, not forgotten |
| D5 | The alert is posted **by the health job, once per state transition** — not by the sweeps | 3 settlement + 4 billing sweeps on 60s/3600s ticks would post up to ~7 alerts/minute to `#blackink-qa` while halted, which trains operators to ignore the channel. The job compares its outcome against the previous run's recorded outcome and posts only on entry into a halting state and on recovery. Sweeps log at WARNING and skip silently |
| D6 | All 7 sweeps gated, uniformly | Halting a *credit* sweep (miss-credit, dispute, guarantee) delays money owed **to** the client, which superficially argues against gating it. It is still gated: every sweep is idempotent and reclaims on the next tick, so a halt defers rather than loses, and a uniform rule has no "which sweep was exempt again?" failure mode |
| D7 | No enable/disable flag on the gate. Only the staleness bound is configurable (`VERA_HEALTH_MAX_AGE_MINUTES`) | `billing_miss_credit_sweep_enabled` is fail-closed *by* a flag because the flag disables a sweep that would otherwise issue false credits. Here a flag would disable a **protection** — the opposite direction. A safety gate that defaults off is not a safety gate. There is no lock-in risk: an ABSTAIN clears automatically on the next clean run, and an operator can widen the staleness bound |
| D8 | Event type `vera_health_halt_issued`, written on transition only, under the reserved `BLACKINK_INTERNAL_SALES` client_id | `events` is the confirmed proof ledger (audit Q2/§3.1.1, client-confirmed 10/09), so a halt that blocked billing must be provable. Platform-wide events already use this reserved client — precedent: `src/services/billing/offers.py:152` writes `rate_migration_applied` the same way. Transition-only, because one event per run is ~1,440 rows/day of noise in the ledger backing billing disputes |

## 6. Requirement → trace → code → test

Health-job interval: **5 minutes**. Staleness bound: **30 minutes** (6 missed
ticks of tolerance before a halt).

| # | Requirement | Trigger → outcome trace | Code path | Test that proves it |
|---|---|---|---|---|
| R1 | Vera health checks actually run on a schedule | `main.py` lifespan starts thread → every 300s `vera_health_sweep.run_sweep()` → `run_health_checks()` (unchanged) → 3 `HealthResult`s → one `vera_health_runs` row (`ran_at`, per-check state/detail, `overall`) → visible in the row and the log | NEW `src/tasks/vera_health_sweep.py`; MOD `src/api/main.py` `_start_background_workers` + interval const; `src/agents/vera/runner.py` **unchanged** | `tests/test_background_workers.py` — extend membership assertion with `vera_health_sweep.run_sweep` (mirrors the show-rate-reminder regression guard) |
| R2 | A run is durably recorded, incl. per-check state | as R1 | NEW `migrations/apply_vera_health_runs.py` (non-tenant, `get_owner_db_context`, `CREATE TABLE IF NOT EXISTS`, grants to both runtime roles — `apply_relay_halts.py` as template) | `tests/test_vera_health_gate.py::test_run_persists_all_three_check_states`; live-DB insert/read in `tests/test_vera_health_live.py` (skipped without Postgres, same convention as `test_settlement_live.py`) |
| R3 | Any check ABSTAIN ⇒ settlement/billing sweeps halt | latest row has `overall='HALT'`/`reason='ABSTAIN'` → each of the 7 sweeps calls `assert_health_ok()` first → returns a HALT decision → sweep logs WARNING and returns 0 **before opening a DB session or reaching Stripe** | NEW `src/agents/vera/health_gate.py::evaluate_settlement_health()`; MOD `src/tasks/settlement_sweep.py` (×3), `src/tasks/billing_sweep.py` (×4) | `test_vera_health_gate.py`: parametrized over all 7 sweep functions, asserting each returns 0 **and never opens a DB session** while halted — the `get_system_db_context`-raises-AssertionError trick from `tests/test_billing_sweep_gate.py:16-21` |
| R4 | No health run at all ⇒ halt (C2) | empty `vera_health_runs` → gate returns HALT/`NO_HEALTH_RUN` → same as R3 | `health_gate.py` | `test_vera_health_gate.py::test_no_health_run_halts_every_sweep` |
| R5 | Stale health run ⇒ halt (C2) | latest `ran_at` older than `VERA_HEALTH_MAX_AGE_MINUTES` → HALT/`STALE_HEALTH_RUN` | `health_gate.py`; MOD `config/settings.py` (`vera_health_max_age_minutes: int = 30`) | `test_vera_health_gate.py::test_stale_run_halts` and `::test_run_inside_window_does_not_halt` — via the gate's explicit `as_of` parameter, no clock mocking (same discipline as every sweep's `claim_time`) |
| R6 | UNKNOWN alone does **not** halt (D2) | all checks VALUE/UNKNOWN, none ABSTAIN → `overall='OK'` → sweeps proceed unchanged | `health_gate.py` | `test_vera_health_gate.py::test_unknown_campaign_feed_does_not_halt` — the regression that matters most: it pins today's Instantly-unconfigured reality as non-halting |
| R7 | Administrators alerted in `#blackink-qa` on halt | run outcome differs from previous run's → `post_notice(channel_key="qa", …)` naming each check's state and the halt reason → message in `#blackink-qa` | `src/tasks/vera_health_sweep.py`, via `src/services/slack/post.py::post_notice` (existing, `asyncio.run` from a sync task — `daily_digest.py:145-149` precedent and its rationale comment) | `test_vera_health_gate.py::test_alert_posted_on_transition_into_halt`, `::test_no_alert_while_halt_state_unchanged`, `::test_alert_posted_on_recovery` (fake `post_notice`, assert call count) |
| R8 | The halt is provable in the proof ledger | transition into HALT → `log_event(..., "vera_health_halt_issued", ...)` under `BLACKINK_INTERNAL_SALES` | MOD `src/services/events.py` — add `vera_health_halt_issued` to `REQUIRED_PAYLOAD_FIELDS` (`{"reason", "abstaining_checks", "ran_at"}`) | `test_vera_health_gate.py::test_halt_writes_event`; `tests/test_events.py` convention for the required-field rejection |
| R9 | The new migration cannot silently skip CI or the runbook | — | MOD `.github/workflows/tests.yml`, `.github/workflows/tenant_leakage_nightly.yml`, `CLAUDE.md` migration block | **Existing** `tests/test_migration_coverage.py::test_documented_migration_order_matches_ci` asserts both directions (documented-but-not-in-CI **and** in-CI-but-undocumented) — it fails if any of the three is missed |

### Tenant-isolation note (mandatory per `CLAUDE.md`)

`vera_health_runs` is **not** tenant-bearing — see D4 for the reasoning and
the `relay_halts` precedent. Therefore: **no** `TENANT_POLICIES` entry and
**no** `apply_rls_policies.py` change. It is not a forgotten registration;
the migration docstring will say so explicitly, as `apply_relay_halts.py`
does. Consequence: `test_migration_coverage.py`'s tenant-table parametrized
tests will not cover it, but `test_documented_migration_order_matches_ci`
(R9) still will, because that test scans CLAUDE.md and the workflows
directly rather than iterating the registry.

### Files touched

**New (4):** `migrations/apply_vera_health_runs.py`,
`src/agents/vera/health_gate.py`, `src/tasks/vera_health_sweep.py`,
`tests/test_vera_health_gate.py` (+ `tests/test_vera_health_live.py`).
**Modified (8):** `src/tasks/settlement_sweep.py`,
`src/tasks/billing_sweep.py`, `src/api/main.py`, `src/services/events.py`,
`config/settings.py`, `CLAUDE.md`, both CI workflows,
`scripts/crontab.txt` (comment only), `tests/test_background_workers.py`.
**Unchanged:** `src/agents/vera/runner.py`, all three `checks/*.py`,
`health_result.py`, `tests/test_vera_health.py` — the tested core is not
touched.

## 7. Open questions

One, and it does not block starting the build.

**Q-A — Should a Vera `UNKNOWN` also halt settlement, or only `ABSTAIN`?**

Blueprint `:134` names both states in the same sentence as the halt
requirement. This plan halts on ABSTAIN only, on the reasoning in C1 —
`health_result.py:16-19` attaches the blocking obligation to ABSTAIN alone,
and because `check_campaign_feed` returns UNKNOWN for as long as Instantly is
unconfigured (B8/B12, still outstanding), an UNKNOWN-halts reading would stop
every settlement and billing sweep on the day this ships.

Wording for the client: *"Vera returns UNKNOWN when an integration is simply
not connected yet (Instantly today), and ABSTAIN when it is connected but the
check failed. We halt settlement on ABSTAIN — 'we tried and could not verify'
— and not on UNKNOWN — 'not wired up yet'. If you want UNKNOWN to halt too,
say so: it means settlement stays halted until the Instantly account exists,
which is a business call, not a technical one."*

Both readings are one predicate apart in `health_gate.py`, so a later reversal
is cheap. Everything else in Steps 3–5 is settled; there are no unresolved
financial, compliance, security or tenant-isolation decisions being handed to
`production-execution`.

---

**Next:** `production-execution` against §6.
