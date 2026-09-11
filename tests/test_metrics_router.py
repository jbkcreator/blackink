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


def test_metrics_sql_open_click_reply_rate_are_null_not_a_fabricated_zero():
    assert "NULL::numeric AS open_rate_pct" in _METRICS_SQL
    assert "NULL::numeric AS click_rate_pct" in _METRICS_SQL
    assert "NULL::numeric AS reply_rate_pct" in _METRICS_SQL
    assert "email_opened" not in _METRICS_SQL
    assert "email_clicked" not in _METRICS_SQL
    assert "email_replied" not in _METRICS_SQL


def test_metrics_sql_excludes_the_demo_sandbox_client():
    assert "DEMO_FRIDAY_SANDBOX" in _METRICS_SQL
