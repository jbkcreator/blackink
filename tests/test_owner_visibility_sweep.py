"""Tests for owner_visibility_sweep — no live DB, no live HTTP, no live API."""
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone

import pytest

from src.services.owner_visibility.signals.base import SignalResult, SCORED, MISSING_DATA
from src.services.owner_visibility.score_calculator import ScoreBreakdown


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_company_row(**kwargs):
    defaults = {
        "company_id": "abc123",
        "company_name": "Test PM LLC",
        "domain": "testpm.com",
        "website": "https://testpm.com",
        "county_slug": "hillsborough_fl",
        "google_place_id": None,
    }
    defaults.update(kwargs)
    row = MagicMock()
    for k, v in defaults.items():
        setattr(row, k, v)
    return row


# ── _current_month_key ────────────────────────────────────────────────────────

class TestCurrentMonthKey:
    def test_format(self):
        from src.tasks.owner_visibility_sweep import _current_month_key
        key = _current_month_key()
        # Must match YYYY-MM
        import re
        assert re.match(r"^\d{4}-\d{2}$", key)


# ── run_sweep integration (all dependencies stubbed) ─────────────────────────

class TestRunSweep:
    def _stub_signals(self) -> list[SignalResult]:
        return [
            SignalResult("website_owner_page", 14, 14, SCORED, "test"),
            SignalResult("website_contact_info", 10, 10, SCORED, "test"),
            SignalResult("website_tech_health", 6, 6, SCORED, "test"),
            SignalResult("website_after_hours", 8, 8, SCORED, "test"),
            SignalResult("dbpr_active_licence", 4, 4, SCORED, "test"),
            SignalResult("google_rating", 0, 20, MISSING_DATA, "stub"),
            SignalResult("google_review_volume", 0, 16, MISSING_DATA, "stub"),
            SignalResult("google_review_recency", 0, 8, MISSING_DATA, "stub"),
            SignalResult("google_biz_completeness", 0, 8, MISSING_DATA, "stub"),
            SignalResult("google_response_rate", 0, 6, MISSING_DATA, "stub"),
        ]

    def test_run_sweep_upserts_one_row_per_company(self):
        rows = [_make_company_row(), _make_company_row(company_id="def456", domain="other.com")]

        fake_db = MagicMock()
        fake_db.__enter__ = MagicMock(return_value=fake_db)
        fake_db.__exit__ = MagicMock(return_value=False)
        fake_db.execute.return_value.fetchall.return_value = rows

        stub_signals = self._stub_signals()

        with patch("src.tasks.owner_visibility_sweep.get_system_db_context", return_value=fake_db), \
             patch("src.tasks.owner_visibility_sweep.WebsiteSignalProvider") as MockWebsite, \
             patch("src.tasks.owner_visibility_sweep.DbprLicenceSignalProvider") as MockDbpr, \
             patch("src.tasks.owner_visibility_sweep.build_google_places_provider") as MockGoogle:

            MockWebsite.return_value.collect.return_value = stub_signals[:4]
            MockDbpr.return_value.collect.return_value = [stub_signals[4]]
            MockGoogle.return_value.collect.return_value = stub_signals[5:]

            from src.tasks.owner_visibility_sweep import run_sweep
            count = run_sweep()

        assert count == 2

    def test_run_sweep_returns_0_on_empty_company_list(self):
        fake_db = MagicMock()
        fake_db.__enter__ = MagicMock(return_value=fake_db)
        fake_db.__exit__ = MagicMock(return_value=False)
        fake_db.execute.return_value.fetchall.return_value = []

        with patch("src.tasks.owner_visibility_sweep.get_system_db_context", return_value=fake_db), \
             patch("src.tasks.owner_visibility_sweep.WebsiteSignalProvider"), \
             patch("src.tasks.owner_visibility_sweep.DbprLicenceSignalProvider"), \
             patch("src.tasks.owner_visibility_sweep.build_google_places_provider"):

            from src.tasks.owner_visibility_sweep import run_sweep
            count = run_sweep()

        assert count == 0


# ── main() exit codes ─────────────────────────────────────────────────────────

class TestMain:
    def test_main_returns_0_on_success(self):
        with patch("src.tasks.owner_visibility_sweep.run_sweep", return_value=5):
            from src.tasks.owner_visibility_sweep import main
            assert main() == 0

    def test_main_returns_1_on_exception(self):
        with patch("src.tasks.owner_visibility_sweep.run_sweep", side_effect=RuntimeError("boom")):
            from src.tasks.owner_visibility_sweep import main
            assert main() == 1
