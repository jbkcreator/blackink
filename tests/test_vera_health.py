"""Unit tests for Vera health checks.

Covers the tri-state VALUE/UNKNOWN/ABSTAIN contract and the silent-zero
invariant — that a failed or unconfigured source never returns VALUE(0).

No live DB or Instantly connection required: all DB calls use FakeSession,
all Instantly calls are patched.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.vera.health_result import ABSTAIN, UNKNOWN, VALUE, HealthResult


# ── HealthResult construction ─────────────────────────────────────────────────

def test_health_result_value_state():
    r = HealthResult("pm_feed", VALUE, {"synced_last_7d": 3}, "ok")
    assert r.state == VALUE
    assert r.value == {"synced_last_7d": 3}


def test_health_result_unknown_state():
    r = HealthResult("campaign_feed", UNKNOWN, None, "not configured")
    assert r.state == UNKNOWN
    assert r.value is None


def test_health_result_abstain_state():
    r = HealthResult("pipeline", ABSTAIN, None, "DB error")
    assert r.state == ABSTAIN
    assert r.value is None


def test_health_result_rejects_invalid_state():
    with pytest.raises(ValueError, match="state must be one of"):
        HealthResult("x", "PASS", None, "bad")


def test_health_result_rejects_non_none_value_on_unknown():
    with pytest.raises(ValueError, match="value must be None"):
        HealthResult("x", UNKNOWN, {"count": 0}, "bad")


def test_health_result_rejects_non_none_value_on_abstain():
    with pytest.raises(ValueError, match="value must be None"):
        HealthResult("x", ABSTAIN, 0, "bad")


# ── pm_feed check ─────────────────────────────────────────────────────────────

class _FakeRow:
    def __init__(self, *cols):
        self._cols = cols

    def __getitem__(self, i):
        return self._cols[i]


class _FakePmSession:
    def __init__(self, synced, total):
        self._synced = synced
        self._total = total

    def execute(self, stmt, *args, **kwargs):
        class _Res:
            def fetchone(inner_self):
                return _FakeRow(self._synced, self._total)
        return _Res()


class _BrokenSession:
    def execute(self, *a, **kw):
        raise RuntimeError("DB is down")


def test_pm_feed_value_on_success():
    from src.agents.vera.checks.pm_feed import check_pm_feed
    sess = _FakePmSession(synced=4, total=10)
    r = check_pm_feed(sess)
    assert r.state == VALUE
    assert r.value["synced_last_7d"] == 4
    assert r.value["total_with_books"] == 10


def test_pm_feed_value_zero_is_real_reading():
    """Zero synced is a real reading — not a silent failure."""
    from src.agents.vera.checks.pm_feed import check_pm_feed
    sess = _FakePmSession(synced=0, total=0)
    r = check_pm_feed(sess)
    assert r.state == VALUE
    assert r.value["synced_last_7d"] == 0


def test_pm_feed_abstain_on_db_error():
    from src.agents.vera.checks.pm_feed import check_pm_feed
    r = check_pm_feed(_BrokenSession())
    assert r.state == ABSTAIN
    assert r.value is None
    assert "DB query failed" in r.detail


def test_pm_feed_abstain_has_no_value():
    """The silent-zero invariant: ABSTAIN must never carry value=0."""
    from src.agents.vera.checks.pm_feed import check_pm_feed
    r = check_pm_feed(_BrokenSession())
    assert r.value is None


# ── campaign_feed check ───────────────────────────────────────────────────────

def _settings_disabled():
    return SimpleNamespace(instantly_enabled=False, instantly_api_key=None)


def _settings_enabled():
    key = MagicMock()
    key.get_secret_value.return_value = "test-key"
    return SimpleNamespace(instantly_enabled=True, instantly_api_key=key)


def test_campaign_feed_unknown_when_disabled():
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_disabled()):
        r = check_campaign_feed()
    assert r.state == UNKNOWN
    assert r.value is None


def test_campaign_feed_unknown_has_no_value():
    """The silent-zero invariant: UNKNOWN must never carry value={'sent': 0}."""
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_disabled()):
        r = check_campaign_feed()
    assert r.value is None


def test_campaign_feed_abstain_when_api_returns_none():
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    mock_svc = MagicMock()
    mock_svc.return_value.get_daily_analytics.return_value = None
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_enabled()), \
         patch("src.agents.vera.checks.campaign_feed.InstantlyService", mock_svc):
        r = check_campaign_feed()
    assert r.state == ABSTAIN
    assert r.value is None


def test_campaign_feed_abstain_has_no_value():
    """The silent-zero invariant: ABSTAIN must never carry value={'sent': 0}."""
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    mock_svc = MagicMock()
    mock_svc.return_value.get_daily_analytics.return_value = None
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_enabled()), \
         patch("src.agents.vera.checks.campaign_feed.InstantlyService", mock_svc):
        r = check_campaign_feed()
    assert r.value is None


def test_campaign_feed_abstain_when_api_raises():
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    mock_svc = MagicMock()
    mock_svc.return_value.get_daily_analytics.side_effect = RuntimeError("network error")
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_enabled()), \
         patch("src.agents.vera.checks.campaign_feed.InstantlyService", mock_svc):
        r = check_campaign_feed()
    assert r.state == ABSTAIN
    assert r.value is None


def test_campaign_feed_value_on_success():
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    api_data = {"total_sent": 50, "total_opened": 10, "total_bounced": 2, "total_complained": 0}
    mock_svc = MagicMock()
    mock_svc.return_value.get_daily_analytics.return_value = api_data
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_enabled()), \
         patch("src.agents.vera.checks.campaign_feed.InstantlyService", mock_svc):
        r = check_campaign_feed()
    assert r.state == VALUE
    assert r.value["sent"] == 50
    assert r.value["bounced"] == 2


def test_campaign_feed_value_zero_sends_is_real():
    """Zero sends is a real reading — not a silent failure."""
    from src.agents.vera.checks.campaign_feed import check_campaign_feed
    api_data = {"total_sent": 0, "total_opened": 0, "total_bounced": 0, "total_complained": 0}
    mock_svc = MagicMock()
    mock_svc.return_value.get_daily_analytics.return_value = api_data
    with patch("src.agents.vera.checks.campaign_feed.get_settings", return_value=_settings_enabled()), \
         patch("src.agents.vera.checks.campaign_feed.InstantlyService", mock_svc):
        r = check_campaign_feed()
    assert r.state == VALUE
    assert r.value["sent"] == 0


# ── pipeline check ────────────────────────────────────────────────────────────

class _FakePipelineSession:
    def __init__(self, pending, cleared, quarantined, rejected, promoted):
        self._status = _FakeRow(pending, cleared, quarantined, rejected)
        self._promoted = promoted

    def execute(self, stmt, *a, **kw):
        sql = str(stmt).lower()
        if "raw_prospect_companies" in sql:
            class _Res:
                def fetchone(inner_self):
                    return self._status
            return _Res()
        else:
            class _ScalarRes:
                def scalar(inner_self):
                    return self._promoted
            return _ScalarRes()


def test_pipeline_value_on_success():
    from src.agents.vera.checks.pipeline import check_pipeline_health
    sess = _FakePipelineSession(pending=10, cleared=5, quarantined=2, rejected=1, promoted=8)
    r = check_pipeline_health(sess)
    assert r.state == VALUE
    assert r.value["pending"] == 10
    assert r.value["cleared"] == 5
    assert r.value["quarantined"] == 2
    assert r.value["rejected"] == 1
    assert r.value["promoted_last_24h"] == 8


def test_pipeline_value_all_zeros_is_real():
    """All-zero pipeline counts are a valid reading (new or empty system)."""
    from src.agents.vera.checks.pipeline import check_pipeline_health
    sess = _FakePipelineSession(pending=0, cleared=0, quarantined=0, rejected=0, promoted=0)
    r = check_pipeline_health(sess)
    assert r.state == VALUE
    assert r.value["pending"] == 0


def test_pipeline_abstain_on_db_error():
    from src.agents.vera.checks.pipeline import check_pipeline_health
    r = check_pipeline_health(_BrokenSession())
    assert r.state == ABSTAIN
    assert r.value is None


def test_pipeline_abstain_has_no_value():
    """The silent-zero invariant: ABSTAIN must not carry pipeline counts of zero."""
    from src.agents.vera.checks.pipeline import check_pipeline_health
    r = check_pipeline_health(_BrokenSession())
    assert r.value is None


# ── runner ────────────────────────────────────────────────────────────────────

def test_runner_returns_three_results():
    """runner.run_health_checks() must return one result per check."""
    from src.agents.vera.runner import run_health_checks

    pm_result = HealthResult("pm_feed", VALUE, {"synced_last_7d": 2, "total_with_books": 5}, "ok")
    pl_result = HealthResult("pipeline", VALUE, {"pending": 0, "cleared": 0, "quarantined": 0,
                                                 "rejected": 0, "promoted_last_24h": 3}, "ok")
    cf_result = HealthResult("campaign_feed", UNKNOWN, None, "not configured")

    @contextmanager
    def _scope():
        yield MagicMock()

    mock_db = MagicMock()
    mock_db.system_session_scope.return_value = _scope()

    with patch("src.agents.vera.runner.Database", return_value=mock_db), \
         patch("src.agents.vera.runner.check_pm_feed", return_value=pm_result), \
         patch("src.agents.vera.runner.check_pipeline_health", return_value=pl_result), \
         patch("src.agents.vera.runner.check_campaign_feed", return_value=cf_result):
        results = run_health_checks()

    assert len(results) == 3
    names = {r.check_name for r in results}
    assert names == {"pm_feed", "pipeline", "campaign_feed"}


def test_runner_abstains_both_db_checks_on_session_failure():
    """If system_session_scope() raises, both DB-backed checks must ABSTAIN."""
    from src.agents.vera.runner import run_health_checks

    mock_db = MagicMock()
    mock_db.system_session_scope.side_effect = RuntimeError("DB unreachable")

    cf_result = HealthResult("campaign_feed", UNKNOWN, None, "not configured")

    with patch("src.agents.vera.runner.Database", return_value=mock_db), \
         patch("src.agents.vera.runner.check_campaign_feed", return_value=cf_result):
        results = run_health_checks()

    db_results = [r for r in results if r.check_name in ("pm_feed", "pipeline")]
    assert all(r.state == ABSTAIN for r in db_results)
    assert all(r.value is None for r in db_results)
