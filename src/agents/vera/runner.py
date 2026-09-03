"""Vera health-check runner — aggregates all three checks.

Runs pm_feed, campaign_feed, and pipeline checks in sequence and returns
the full list of HealthResult objects. DB-backed checks (pm_feed, pipeline)
share one system_session_scope() so they execute in a single transaction.

ABSTAIN propagation: if any check returns ABSTAIN or UNKNOWN, callers
should not interpret the aggregate as "healthy" — they must inspect each
result individually. This module does not collapse the list to a single
boolean, by design: hiding a partial failure inside a summary boolean is
exactly the silent-zero problem in aggregate form.

Usage:
    results = run_health_checks()
    for r in results:
        print(r.check_name, r.state, r.detail)
"""
from __future__ import annotations

import logging
from typing import List

from src.agents.vera.health_result import HealthResult
from src.agents.vera.checks.pm_feed import check_pm_feed
from src.agents.vera.checks.campaign_feed import check_campaign_feed
from src.agents.vera.checks.pipeline import check_pipeline_health
from src.core.database import Database

logger = logging.getLogger(__name__)


def run_health_checks() -> List[HealthResult]:
    """Execute all Vera health checks and return one HealthResult per check.

    Order: pm_feed, campaign_feed, pipeline.
    DB-backed checks run inside a single system_session_scope().
    campaign_feed is not DB-backed (calls Instantly directly).
    """
    results: List[HealthResult] = []
    db = Database()

    try:
        with db.system_session_scope() as sess:
            results.append(check_pm_feed(sess))
            results.append(check_pipeline_health(sess))
    except Exception as exc:
        logger.error("vera.runner: system_session_scope failed — both DB checks ABSTAIN: %s", exc)
        from src.agents.vera.health_result import ABSTAIN
        results.append(HealthResult("pm_feed", ABSTAIN, None, f"DB session failed: {exc}"))
        results.append(HealthResult("pipeline", ABSTAIN, None, f"DB session failed: {exc}"))

    results.append(check_campaign_feed())

    for r in results:
        logger.info("vera: %s state=%s detail=%s", r.check_name, r.state, r.detail)

    return results
