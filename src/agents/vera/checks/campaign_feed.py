"""Vera check — Instantly campaign feed health.

Returns the platform-wide daily send/bounce/open counts from Instantly.
Three possible outcomes:

  UNKNOWN — Instantly is not enabled or no API key is configured.
            The check is deliberately not attempted. Callers must not
            treat UNKNOWN as "zero sends" — they are different facts.

  ABSTAIN — Instantly is configured but the API call failed or returned
            None. Cannot verify campaign feed health. Same downstream
            treatment as compliance_gate ABSTAIN: must block any claim
            that the feed is healthy.

  VALUE   — Real analytics obtained. value dict carries sent/opened/
            bounced/complained counts. Any of these can legitimately
            be zero (e.g. no sends yesterday) and callers must interpret
            zero as a real reading, not an error.

The silent-zero invariant is enforced by the UNKNOWN path: if Instantly
is unconfigured and we returned VALUE(sent=0), that would make a
"not connected to Instantly at all" state look identical to "sent 0
emails yesterday" — a verified-clean result. UNKNOWN prevents that.
"""
from __future__ import annotations

import logging
from typing import Optional

from config.settings import get_settings
from src.agents.vera.health_result import ABSTAIN, UNKNOWN, VALUE, HealthResult
from src.services.instantly_service import InstantlyService

logger = logging.getLogger(__name__)


def check_campaign_feed(campaign_id: Optional[str] = None) -> HealthResult:
    """Check Instantly daily analytics for the platform (or one campaign).

    campaign_id: optional Instantly campaign UUID to scope the check.
                 None = platform-wide totals.
    """
    settings = get_settings()
    if not settings.instantly_enabled or not settings.instantly_api_key:
        return HealthResult(
            check_name="campaign_feed",
            state=UNKNOWN,
            value=None,
            detail="Instantly not configured (instantly_enabled=False or no API key)",
        )

    try:
        data = InstantlyService().get_daily_analytics(campaign_id=campaign_id)
    except Exception as exc:
        logger.error("vera.campaign_feed: InstantlyService raised — abstaining: %s", exc)
        return HealthResult(
            check_name="campaign_feed",
            state=ABSTAIN,
            value=None,
            detail=f"Instantly API raised an exception — cannot verify campaign feed: {exc}",
        )

    if data is None:
        # get_daily_analytics() returns None when all retries exhausted or
        # a non-retryable HTTP error occurs.  A None result is NOT the same
        # as "zero sends" — it means we couldn't reach the API.
        logger.error("vera.campaign_feed: get_daily_analytics returned None — abstaining")
        return HealthResult(
            check_name="campaign_feed",
            state=ABSTAIN,
            value=None,
            detail="Instantly get_daily_analytics returned None — API unreachable or exhausted retries",
        )

    sent = data.get("total_sent", 0) or 0
    opened = data.get("total_opened", 0) or 0
    bounced = data.get("total_bounced", 0) or 0
    complained = data.get("total_complained", 0) or 0
    return HealthResult(
        check_name="campaign_feed",
        state=VALUE,
        value={
            "sent": sent,
            "opened": opened,
            "bounced": bounced,
            "complained": complained,
        },
        detail=f"sent={sent} opened={opened} bounced={bounced} complained={complained}",
    )
