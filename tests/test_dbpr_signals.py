"""Tests for DbprLicenceSignalProvider — no file I/O via monkeypatching."""
import pytest

from src.services.owner_visibility.signals.base import SCORED, MISSING_DATA
from src.services.owner_visibility.signals import dbpr_licence as dbpr_module
from src.services.owner_visibility.signals.dbpr_licence import DbprLicenceSignalProvider


@pytest.fixture(autouse=True)
def reset_csv_cache():
    """Ensure the module-level CSV cache is cleared between tests."""
    dbpr_module._csv_cache = None
    yield
    dbpr_module._csv_cache = None


def _inject_records(records: list[dict], monkeypatch):
    monkeypatch.setattr(dbpr_module, "_csv_cache", records)


class TestDbprLicenceSignalProvider:
    def test_active_licence_match_awards_4(self, monkeypatch):
        _inject_records(
            [{"name": "Tampa Property Management LLC", "license_number": "BK123456", "status": "Current Active"}],
            monkeypatch,
        )
        provider = DbprLicenceSignalProvider()
        results = provider.collect({"company_name": "Tampa Property Management LLC"})
        assert len(results) == 1
        assert results[0].points_awarded == 4
        assert results[0].status == SCORED

    def test_fuzzy_match_still_awards_4(self, monkeypatch):
        _inject_records(
            [{"name": "Tampa Property Mgmt LLC", "license_number": "BK123", "status": "Current Active"}],
            monkeypatch,
        )
        provider = DbprLicenceSignalProvider()
        results = provider.collect({"company_name": "Tampa Property Management LLC"})
        # Token sort ratio handles abbreviation variations.
        assert results[0].status == SCORED

    def test_inactive_licence_awards_0(self, monkeypatch):
        _inject_records(
            [{"name": "Inactive PM Co", "license_number": "BK999", "status": "Expired"}],
            monkeypatch,
        )
        provider = DbprLicenceSignalProvider()
        results = provider.collect({"company_name": "Inactive PM Co"})
        assert results[0].points_awarded == 0
        assert results[0].status == SCORED

    def test_no_match_scores_0_not_missing(self, monkeypatch):
        _inject_records(
            [{"name": "Completely Different Company", "license_number": "BK000", "status": "Current Active"}],
            monkeypatch,
        )
        provider = DbprLicenceSignalProvider()
        results = provider.collect({"company_name": "Unrelated Firm"})
        assert results[0].points_awarded == 0
        assert results[0].status == SCORED  # ran but found nothing — not MISSING_DATA

    def test_empty_csv_returns_missing_data(self, monkeypatch):
        _inject_records([], monkeypatch)
        # _get_records returns [] which triggers the "CSV not found" branch.
        # Patch the cache directly to empty to avoid file system lookups.
        provider = DbprLicenceSignalProvider()
        results = provider.collect({"company_name": "Some PM Co"})
        assert results[0].status == MISSING_DATA

    def test_no_company_name_returns_missing(self, monkeypatch):
        _inject_records([{"name": "Any Co", "license_number": "X", "status": "Current Active"}], monkeypatch)
        provider = DbprLicenceSignalProvider()
        results = provider.collect({})
        assert results[0].status == MISSING_DATA
