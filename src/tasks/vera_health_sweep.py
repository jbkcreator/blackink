"""Vera health sweep (S-1) — the missing scheduled caller of
src.agents.vera.runner.run_health_checks().

Runs every 5 minutes from src/api/main.py::_start_background_workers (see
that module for why: both consumers this gate protects — settlement_sweep
and billing_sweep — already run as in-process threads there, not via cron;
putting the health producer in a different reliability class than its
consumers would let a Vera outage and a sweep outage be independent events).

Each run: executes all three checks, persists exactly one row to
vera_health_runs (migrations/apply_vera_health_runs.py), and posts to
#blackink-qa ONLY on a transition into or out of a halting state — not on
every tick. With 7 gated sweep functions on 60s-3600s ticks, alerting from
each of them while halted would flood the channel; alerting from here, once
per state change, keeps the channel meaningful. See
src/agents/vera/health_gate.py for what "halting state" means.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.agents.vera.health_gate import evaluate_settlement_health
from src.agents.vera.health_result import HealthResult
from src.agents.vera.runner import run_health_checks
from src.core.database import get_system_db_context
from src.services.events import log_event
from src.services.slack.post import post_notice

logger = logging.getLogger(__name__)

_INTERNAL_SALES_CLIENT_ID = "BLACKINK_INTERNAL_SALES"


def _result_by_name(results: list[HealthResult], name: str) -> HealthResult:
    for r in results:
        if r.check_name == name:
            return r
    # Every call site here passes the fixed 3-check list run_health_checks()
    # always returns (see its own docstring: "Order: pm_feed, campaign_feed,
    # pipeline" — unconditional, no partial-list return path) — a name miss
    # would mean runner.py's contract changed without this module noticing,
    # which must fail loudly rather than silently write a NULL check state.
    raise ValueError(f"vera_health_sweep: no HealthResult named {name!r} in runner output")


def _persist_run(results: list[HealthResult]) -> str:
    """Writes one vera_health_runs row. Returns 'OK' or 'HALT' — the
    ABSTAIN-only rule lives in health_gate.py; this just mirrors it for the
    row's own `overall` column so a human reading the table doesn't have to
    re-derive it."""
    pm_feed = _result_by_name(results, "pm_feed")
    campaign_feed = _result_by_name(results, "campaign_feed")
    pipeline = _result_by_name(results, "pipeline")

    from src.agents.vera.health_result import ABSTAIN
    overall = "HALT" if ABSTAIN in (pm_feed.state, campaign_feed.state, pipeline.state) else "OK"

    with get_system_db_context() as session:
        session.execute(
            text("""
                INSERT INTO vera_health_runs (
                    ran_at, overall,
                    pm_feed_state, pm_feed_detail,
                    campaign_feed_state, campaign_feed_detail,
                    pipeline_state, pipeline_detail
                ) VALUES (
                    :ran_at, :overall,
                    :pm_feed_state, :pm_feed_detail,
                    :campaign_feed_state, :campaign_feed_detail,
                    :pipeline_state, :pipeline_detail
                )
            """),
            {
                "ran_at": datetime.now(timezone.utc),
                "overall": overall,
                "pm_feed_state": pm_feed.state,
                "pm_feed_detail": pm_feed.detail,
                "campaign_feed_state": campaign_feed.state,
                "campaign_feed_detail": campaign_feed.detail,
                "pipeline_state": pipeline.state,
                "pipeline_detail": pipeline.detail,
            },
        )
    return overall


def _maybe_alert_and_log(previous_ok: bool, decision) -> None:
    """Posts to #blackink-qa and writes the proof-ledger event ONLY on a
    transition (healthy->halted or halted->healthy) — see module docstring
    for why this must not fire every tick.

    On entering a halt, the durable events write happens BEFORE the Slack
    post (not after): post_notice() is documented "never raises" only for
    SlackApiError — a raw network-layer exception from the Slack client
    would still propagate. Writing the proof-ledger record first means that
    exact failure mode still leaves the halt provably recorded, even if the
    Slack notification itself is lost."""
    if previous_ok and not decision.ok:
        logger.warning("vera_health_sweep: entering HALT — %s", decision.detail)
        with get_system_db_context() as session:
            log_event(
                _INTERNAL_SALES_CLIENT_ID,
                "vera_health_halt_issued",
                entity_type="vera_health_run",
                entity_id=decision.reason or "unknown",
                payload={
                    "reason": decision.reason,
                    "abstaining_checks": list(decision.abstaining_checks),
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                },
                actor="vera_health_sweep",
                session=session,
            )
        asyncio.run(
            post_notice(
                channel_key="qa",
                text=(
                    f":rotating_light: Vera health gate HALTED settlement/billing sweeps — "
                    f"reason={decision.reason} detail={decision.detail}"
                ),
            )
        )
    elif not previous_ok and decision.ok:
        logger.info("vera_health_sweep: recovered from HALT — %s", decision.detail)
        asyncio.run(
            post_notice(
                channel_key="qa",
                text=f":white_check_mark: Vera health gate recovered — settlement/billing sweeps resumed. {decision.detail}",
            )
        )


# Process-local — this task runs as one thread in one long-lived process
# (see main.py's _start_background_workers), so a plain module global is
# sufficient to detect a transition between consecutive ticks. A restart
# starts from previous_ok=True (assume healthy until the first real read),
# which is safe: if the true state is actually halted, this run's own
# evaluate_settlement_health() call will surface it as a fresh (silent, on
# restart only) transition-into-halt on the very next tick after the first
# vera_health_runs row exists — never worse than the pre-restart alert that
# already fired once.
_previous_ok = True


def run_sweep() -> None:
    global _previous_ok

    results = run_health_checks()
    _persist_run(results)

    decision = evaluate_settlement_health()
    _maybe_alert_and_log(_previous_ok, decision)
    _previous_ok = decision.ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_sweep()
    print("vera_health_sweep: done")
