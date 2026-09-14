"""Regression guard for src/api/metrics_router.py's copy of the digest SQL.

Group D / D-4: this router had the identical event-name/payload-key bug as
src/tasks/daily_digest.py — see tests/test_daily_digest.py for the full
explanation. Mirrors that file's SQL-text assertions since this is a second,
independently-maintained copy of the same query.
"""
from src.api.metrics_router import _METRICS_SQL


def test_metrics_sql_uses_owner_visibility_score_calculated_not_ghost_shopper():
    assert "owner_visibility_score_calculated" in _METRICS_SQL
    assert "owner_score_generated" not in _METRICS_SQL


def test_metrics_sql_county_rank_reports_uses_county_slug_not_county():
    assert "county_slug" in _METRICS_SQL
    assert "payload->>'county'" not in _METRICS_SQL.replace("payload->>'county_slug'", "")


def test_metrics_sql_open_click_reply_rate_wired_to_real_events_with_nullif_guard():
    """S-8 producers now exist, so the engagement rates are real divisions over
    the cold-email denominator (not literal NULL), guarded by NULLIF so an empty
    window reads "n/a" rather than a misleading 0%. Mirrors the identical query
    in src/tasks/daily_digest.py."""
    assert "NULL::numeric AS open_rate_pct" not in _METRICS_SQL
    assert "event_type = 'email_opened'" in _METRICS_SQL
    assert "event_type = 'email_clicked'" in _METRICS_SQL
    assert "event_type = 'email_replied'" in _METRICS_SQL
    assert _METRICS_SQL.count("NULLIF((SELECT COUNT(*) FROM dispatch_cohort), 0)") == 3


def test_metrics_router_reuses_dispatch_cohort_query():
    from src.tasks.daily_digest import _METRICS_SQL as digest_sql

    assert _METRICS_SQL == digest_sql


def test_metrics_sql_excludes_the_demo_sandbox_client():
    # Excluded via the central DEMO_CLIENT_IDS registry (PR #53 finding 1),
    # which also covers DEMO_CLIENT_WINS.
    from src.core.demo_clients import DEMO_CLIENT_IDS
    assert "client_id <> ALL(:demo_client_ids)" in _METRICS_SQL
    assert "DEMO_FRIDAY_SANDBOX" in DEMO_CLIENT_IDS
