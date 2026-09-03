"""Tests for Google Places signal providers — no live API calls."""
import math

import pytest

from src.services.owner_visibility.signals.base import SCORED, MISSING_DATA
from src.services.owner_visibility.signals.google_places import (
    StubGooglePlacesProvider,
    GooglePlacesProvider,
    _score_rating,
    _score_volume,
    _score_recency,
    _score_completeness,
    _score_response_rate,
    build_google_places_provider,
)


# ── Stub provider ─────────────────────────────────────────────────────────────

class TestStubGooglePlacesProvider:
    def test_returns_five_missing_data_signals(self):
        provider = StubGooglePlacesProvider()
        results = provider.collect({"company_name": "Test PM", "domain": "testpm.com"})
        assert len(results) == 5
        assert all(r.status == MISSING_DATA for r in results)
        assert all(r.points_awarded == 0 for r in results)

    def test_signal_names_have_google_prefix(self):
        results = StubGooglePlacesProvider().collect({})
        assert all(r.signal_name.startswith("google_") for r in results)


# ── Individual scorer unit tests ──────────────────────────────────────────────

class TestScoreRating:
    def test_five_stars_awards_20(self):
        assert _score_rating(5.0).points_awarded == 20

    def test_one_star_awards_0(self):
        assert _score_rating(1.0).points_awarded == 0

    def test_three_stars_awards_10(self):
        result = _score_rating(3.0)
        assert result.points_awarded == 10

    def test_none_returns_missing(self):
        assert _score_rating(None).status == MISSING_DATA


class TestScoreVolume:
    def test_zero_reviews_awards_0(self):
        assert _score_volume(0).points_awarded == 0

    def test_none_returns_missing(self):
        assert _score_volume(None).status == MISSING_DATA

    def test_large_count_caps_at_16(self):
        result = _score_volume(10000)
        assert result.points_awarded == 16

    def test_one_review_awards_low_pts(self):
        result = _score_volume(1)
        # log2(2) * 2 = 2
        assert result.points_awarded == 2

    def test_score_increases_with_count(self):
        pts_10 = _score_volume(10).points_awarded
        pts_100 = _score_volume(100).points_awarded
        assert pts_100 > pts_10


class TestScoreRecency:
    def test_all_recent_awards_8(self):
        assert _score_recency(1.0).points_awarded == 8

    def test_none_recent_awards_0(self):
        assert _score_recency(0.0).points_awarded == 0

    def test_half_recent_awards_4(self):
        assert _score_recency(0.5).points_awarded == 4

    def test_none_returns_missing(self):
        assert _score_recency(None).status == MISSING_DATA


class TestScoreCompleteness:
    def test_all_present_awards_8(self):
        result = _score_completeness(hours_set=True, photo_count=5, has_description=True)
        assert result.points_awarded == 8

    def test_nothing_present_awards_0(self):
        result = _score_completeness(hours_set=False, photo_count=0, has_description=False)
        assert result.points_awarded == 0

    def test_hours_only_awards_3(self):
        result = _score_completeness(hours_set=True, photo_count=0, has_description=False)
        assert result.points_awarded == 3

    def test_exactly_3_photos_awards_photo_pts(self):
        result = _score_completeness(hours_set=False, photo_count=3, has_description=False)
        assert result.points_awarded == 3

    def test_2_photos_no_photo_pts(self):
        result = _score_completeness(hours_set=False, photo_count=2, has_description=False)
        assert result.points_awarded == 0


class TestScoreResponseRate:
    def test_full_response_rate_awards_6(self):
        assert _score_response_rate(1.0).points_awarded == 6

    def test_zero_rate_awards_0(self):
        assert _score_response_rate(0.0).points_awarded == 0

    def test_none_returns_missing(self):
        assert _score_response_rate(None).status == MISSING_DATA


# ── build_google_places_provider selects stub when no key ────────────────────

class TestBuildProvider:
    def test_no_key_returns_stub(self, monkeypatch):
        from config import settings as settings_module
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.google_places_api_key = None
        monkeypatch.setattr(settings_module, "settings", mock_settings)

        # Patch get_settings() too since build_google_places_provider calls it.
        import src.services.owner_visibility.signals.google_places as gp_module
        monkeypatch.setattr(gp_module, "get_settings", lambda: mock_settings)

        provider = build_google_places_provider()
        assert isinstance(provider, StubGooglePlacesProvider)
