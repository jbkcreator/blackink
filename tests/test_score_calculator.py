"""Tests for score_calculator.py — pure function, no I/O."""
import pytest

from src.services.owner_visibility.signals.base import SignalResult, SCORED, MISSING_DATA, SKIPPED
from src.services.owner_visibility.score_calculator import calculate_score, ScoreBreakdown


def _scored(name: str, pts: int, possible: int) -> SignalResult:
    return SignalResult(name, pts, possible, SCORED, "test")


def _missing(name: str, possible: int) -> SignalResult:
    return SignalResult(name, 0, possible, MISSING_DATA, "test")


def _skipped(name: str, possible: int) -> SignalResult:
    return SignalResult(name, 0, possible, SKIPPED, "test")


class TestCalculateScore:
    def test_all_scored_sums_correctly(self):
        signals = [
            _scored("website_owner_page", 14, 14),
            _scored("website_contact_info", 7, 10),
            _scored("website_tech_health", 6, 6),
            _scored("website_after_hours", 0, 8),
            _scored("dbpr_active_licence", 4, 4),
            _scored("google_rating", 16, 20),
            _scored("google_review_volume", 8, 16),
            _scored("google_review_recency", 4, 8),
            _scored("google_biz_completeness", 5, 8),
            _scored("google_response_rate", 3, 6),
        ]
        result = calculate_score(signals)
        assert result.score_website == 27   # 14+7+6+0
        assert result.score_dbpr == 4
        assert result.score_google == 36    # 16+8+4+5+3
        assert result.score_total == 67
        assert result.data_gaps == []

    def test_missing_data_goes_to_gaps_not_score(self):
        signals = [
            _scored("website_owner_page", 14, 14),
            _missing("website_contact_info", 10),
            _scored("dbpr_active_licence", 4, 4),
            _missing("google_rating", 20),
        ]
        result = calculate_score(signals)
        assert result.score_website == 14
        assert result.score_dbpr == 4
        assert result.score_google == 0
        assert set(result.data_gaps) == {"website_contact_info", "google_rating"}

    def test_skipped_signals_excluded_from_gaps(self):
        signals = [
            _scored("website_owner_page", 0, 14),
            _skipped("google_rating", 20),
        ]
        result = calculate_score(signals)
        assert result.data_gaps == []
        assert result.score_google == 0

    def test_score_capped_at_100(self):
        # Manufacture a scenario where sub-bucket totals exceed 100.
        signals = [_scored(f"website_x_{i}", 20, 20) for i in range(6)]
        result = calculate_score(signals)
        assert result.score_total == 100

    def test_empty_signals_returns_zero(self):
        result = calculate_score([])
        assert result == ScoreBreakdown(
            score_total=0,
            score_website=0,
            score_dbpr=0,
            score_google=0,
            signal_detail={},
            data_gaps=[],
        )

    def test_duplicate_signal_names_raise(self):
        signals = [
            _scored("website_owner_page", 5, 14),
            _scored("website_owner_page", 5, 14),
        ]
        with pytest.raises(ValueError, match="Duplicate signal names"):
            calculate_score(signals)

    def test_signal_detail_contains_all_signals(self):
        signals = [
            _scored("website_owner_page", 10, 14),
            _missing("dbpr_active_licence", 4),
        ]
        result = calculate_score(signals)
        assert "website_owner_page" in result.signal_detail
        assert "dbpr_active_licence" in result.signal_detail
        assert result.signal_detail["website_owner_page"]["points_awarded"] == 10

    def test_unrecognised_prefix_counts_in_total(self):
        # A future provider with a new prefix should still contribute to score_total.
        signals = [_scored("narpm_membership", 10, 10)]
        result = calculate_score(signals)
        assert result.score_total == 10
        assert result.score_website == 0
        assert result.score_dbpr == 0
        assert result.score_google == 0
