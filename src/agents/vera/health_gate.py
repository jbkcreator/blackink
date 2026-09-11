"""Vera health gate — the missing "downstream halts" half of §3.0.2 A.

The tri-state HealthResult type and the three checks (health_result.py,
checks/*) were already correct: they never fabricate a value for a failed
or unconfigured integration. What did not exist anywhere in the codebase
(S-1 in the Week 0-2 implementation audit) is a consumer that actually acts
on that tri-state signal — settlement_sweep.py and billing_sweep.py ran
every tick regardless of Vera's state, so the tri-state type existed but
nothing downstream of it changed behavior.

This module is that consumer. It reads the LATEST persisted health run
(src/tasks/vera_health_sweep.py writes one every 5 minutes to
vera_health_runs — see that module and migrations/apply_vera_health_runs.py)
and decides whether settlement/billing sweeps may proceed.

Three ways to HALT, not one:
  ABSTAIN            — the latest run recorded at least one check as
                        ABSTAIN: an integration is configured and the check
                        was attempted, but it failed or could not be
                        verified. This is the literal case the blueprint
                        names ("Downstream settlement routines halt
                        automatically ... rather than assuming zero payable
                        activity").
  NO_HEALTH_RUN       — no health run has ever completed. Silently treating
                        "we have never checked" as "everything is fine"
                        would be the exact silent-zero bug this task exists
                        to close, just moved one layer up. See the
                        consolidated Source of Truth doc, line 534: "the two
                        sources scheduled and writing nothing are silent
                        zeros by another name. This is the same bug class as
                        W0-2" (W0-2 is this task).
  STALE_HEALTH_RUN    — the latest run is older than
                        settings.vera_health_max_age_minutes (default 30 —
                        six missed 5-minute ticks). A health job that has
                        silently stopped running is indistinguishable from
                        "everything is fine" unless staleness itself is
                        treated as a halt condition.

UNKNOWN does NOT halt (deliberate, confirmed decision — see
docs/plans/2026-09-10-s1-vera-health-gate.md §4 C1 and §7 Q-A). UNKNOWN means
an integration was never configured (check_campaign_feed returns UNKNOWN
whenever Instantly has no API key — true today, see B8/B12 in the
implementation audit). If UNKNOWN halted, this fix would halt every
settlement and billing sweep from the moment it shipped, for as long as
Instantly remains unconfigured — a regression dressed as a safety feature.
This is flagged to the client as an open question in the plan; the switch
is a single predicate in evaluate_settlement_health() below if the answer
comes back the other way.

Every sweep function this gate protects MUST call it FIRST, before opening
its own get_system_db_context() or making any external (Stripe) call — a
halted sweep must never touch its own working session or an external
provider. This gate opens its OWN short-lived session internally (it needs
one to read vera_health_runs — there is no way to check health without a
read), entirely separate from the sweep's own get_system_db_context import,
which is exactly what makes both true at once and both independently
testable: see tests/test_vera_health_gate.py's "the SWEEP's own
get_system_db_context is never called while halted" assertions (patches
`src.tasks.settlement_sweep.get_system_db_context` /
`src.tasks.billing_sweep.get_system_db_context` specifically — a different
name than this module's own import, so patching one cannot mask the other),
same discipline as run_miss_credit_sweep()'s existing settings-gate check in
billing_sweep.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from config.settings import get_settings
from src.agents.vera.health_result import ABSTAIN
from src.core.database import get_system_db_context

HALT_ABSTAIN = "ABSTAIN"
HALT_NO_HEALTH_RUN = "NO_HEALTH_RUN"
HALT_STALE_HEALTH_RUN = "STALE_HEALTH_RUN"


@dataclass(frozen=True)
class HealthGateDecision:
    ok: bool
    reason: Optional[str]      # None when ok=True; one of the _HALT_* constants otherwise
    detail: str                # human-readable, safe to log or post to Slack verbatim
    abstaining_checks: tuple   # check_names that were ABSTAIN in the latest run, () otherwise


def evaluate_settlement_health(*, as_of: Optional[datetime] = None) -> HealthGateDecision:
    """Read the latest vera_health_runs row and decide OK vs HALT.

    Opens its own get_system_db_context() — deliberately independent of the
    caller's session (see module docstring). `as_of` (default: real now)
    makes the staleness check fast-forwardable in tests without clock
    mocking — same discipline as every claim_time/as_of parameter in
    settlement_sweep.py / billing_sweep.py.

    Never raises: a DB error reading vera_health_runs itself is treated as
    NO_HEALTH_RUN (fail closed — we could not verify health, so we do not
    assume health is fine), consistent with every check function's own
    "DB query failed -> ABSTAIN, never a fabricated VALUE" posture.
    """
    as_of = as_of or datetime.now(timezone.utc)
    max_age = timedelta(minutes=get_settings().vera_health_max_age_minutes)

    try:
        with get_system_db_context() as session:
            row = session.execute(
                text("""
                    SELECT ran_at, overall,
                           pm_feed_state, campaign_feed_state, pipeline_state
                    FROM vera_health_runs
                    ORDER BY ran_at DESC
                    LIMIT 1
                """)
            ).fetchone()
    except Exception as exc:
        return HealthGateDecision(
            ok=False,
            reason=HALT_NO_HEALTH_RUN,
            detail=f"could not read vera_health_runs — treating as no health run: {exc}",
            abstaining_checks=(),
        )

    if row is None:
        return HealthGateDecision(
            ok=False,
            reason=HALT_NO_HEALTH_RUN,
            detail="no Vera health run has ever completed",
            abstaining_checks=(),
        )

    ran_at: datetime = row.ran_at
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)

    age = as_of - ran_at
    if age > max_age:
        return HealthGateDecision(
            ok=False,
            reason=HALT_STALE_HEALTH_RUN,
            detail=f"latest Vera health run is {age} old (max age {max_age})",
            abstaining_checks=(),
        )

    checks = {
        "pm_feed": row.pm_feed_state,
        "campaign_feed": row.campaign_feed_state,
        "pipeline": row.pipeline_state,
    }
    abstaining = tuple(name for name, state in checks.items() if state == ABSTAIN)
    if abstaining:
        return HealthGateDecision(
            ok=False,
            reason=HALT_ABSTAIN,
            detail=f"Vera check(s) abstained: {', '.join(abstaining)} (run at {ran_at.isoformat()})",
            abstaining_checks=abstaining,
        )

    return HealthGateDecision(
        ok=True,
        reason=None,
        detail=f"healthy (run at {ran_at.isoformat()})",
        abstaining_checks=(),
    )
