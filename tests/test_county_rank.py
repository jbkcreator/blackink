"""Tests for the county rank calculator (no DB or HTTP required)."""
import pytest
from src.services.owner_visibility.county_rank import calculate_county_ranks


def _make_row(
    company_id: str,
    company_name: str,
    score_total: int,
    scored_signals: int = 5,
    signal_detail: dict | None = None,
    data_coverage_pct: int = 50,
) -> dict:
    """Build a minimal scored-row dict for testing."""
    if signal_detail is None:
        signal_detail = {
            f"website_sig_{i}": {
                "signal_name": f"website_sig_{i}",
                "points_awarded": score_total // max(scored_signals, 1),
                "points_possible": 10,
                "status": "SCORED",
                "detail": "ok",
            }
            for i in range(scored_signals)
        }
    return {
        "score_id": hash(company_id),
        "company_id": company_id,
        "company_name": company_name,
        "score_total": score_total,
        "signal_detail": signal_detail,
        "data_gaps": [],
        "data_coverage_pct": data_coverage_pct,
    }


class TestCountyRankOrdering:
    def test_rank_order_is_by_score_total(self):
        rows = [
            _make_row("c1", "Firm A", score_total=30),
            _make_row("c2", "Firm B", score_total=80),
            _make_row("c3", "Firm C", score_total=50),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c2"]["county_rank"] == 1
        assert by_id["c3"]["county_rank"] == 2
        assert by_id["c1"]["county_rank"] == 3

    def test_single_firm_gets_rank_1_percentile_100(self):
        rows = [_make_row("c1", "Solo Firm", score_total=42)]
        result = calculate_county_ranks(rows)
        assert result[0]["county_rank"] == 1
        assert result[0]["county_percentile"] == 100

    def test_tie_breaking_primary_by_data_coverage_pct(self):
        # Same score_total; higher data_coverage_pct wins.
        rows = [
            _make_row("zzz", "Z Corp", score_total=50, data_coverage_pct=60),
            _make_row("aaa", "A Corp", score_total=50, data_coverage_pct=40),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["zzz"]["county_rank"] == 1
        assert by_id["aaa"]["county_rank"] == 2

    def test_tie_breaking_secondary_by_company_id_when_coverage_equal(self):
        rows = [
            _make_row("zzz", "Z Corp", score_total=50, data_coverage_pct=50),
            _make_row("aaa", "A Corp", score_total=50, data_coverage_pct=50),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        # Same score and coverage — alphabetically earlier company_id gets rank 1.
        assert by_id["aaa"]["county_rank"] == 1
        assert by_id["zzz"]["county_rank"] == 2

    def test_empty_input_returns_empty(self):
        assert calculate_county_ranks([]) == []


class TestPercentile:
    def test_top_firm_is_100th_percentile(self):
        rows = [_make_row(f"c{i}", f"Firm {i}", score_total=100 - i * 10) for i in range(5)]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c0"]["county_percentile"] == 100

    def test_bottom_firm_is_0th_percentile(self):
        rows = [_make_row(f"c{i}", f"Firm {i}", score_total=100 - i * 10) for i in range(5)]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c4"]["county_percentile"] == 0

    def test_percentile_is_symmetric_for_two_firms(self):
        rows = [
            _make_row("c1", "High", score_total=80),
            _make_row("c2", "Low", score_total=20),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c1"]["county_percentile"] == 100
        assert by_id["c2"]["county_percentile"] == 0


class TestDataFloor:
    def test_below_floor_gets_no_rank(self):
        # Only 2 SCORED signals — below MIN_SCORED_SIGNALS of 3.
        detail = {
            "website_owner_page": {"status": "SCORED", "points_awarded": 10, "points_possible": 14, "detail": ""},
            "website_contact_info": {"status": "SCORED", "points_awarded": 5, "points_possible": 10, "detail": ""},
        }
        rows = [_make_row("c1", "Thin Data Co", score_total=15, signal_detail=detail)]
        result = calculate_county_ranks(rows)
        assert result[0]["county_rank"] is None
        assert result[0]["county_percentile"] is None
        assert result[0]["is_published"] is False

    def test_exactly_at_floor_receives_rank(self):
        # Exactly 3 SCORED signals — meets the floor.
        detail = {
            f"website_sig_{i}": {"status": "SCORED", "points_awarded": 5, "points_possible": 10, "detail": ""}
            for i in range(3)
        }
        rows = [_make_row("c1", "Just Enough Co", score_total=15, signal_detail=detail)]
        result = calculate_county_ranks(rows)
        assert result[0]["county_rank"] == 1

    def test_missing_data_signals_do_not_count_toward_floor(self):
        # 5 signals but only 2 are SCORED; rest are MISSING_DATA.
        detail = {
            "website_owner_page":   {"status": "SCORED",       "points_awarded": 14, "points_possible": 14, "detail": ""},
            "website_contact_info": {"status": "SCORED",       "points_awarded": 10, "points_possible": 10, "detail": ""},
            "dbpr_active_licence":  {"status": "MISSING_DATA", "points_awarded": 0,  "points_possible": 4,  "detail": ""},
            "google_rating":        {"status": "MISSING_DATA", "points_awarded": 0,  "points_possible": 20, "detail": ""},
            "google_review_volume": {"status": "MISSING_DATA", "points_awarded": 0,  "points_possible": 16, "detail": ""},
        }
        rows = [_make_row("c1", "Data Gap Co", score_total=24, signal_detail=detail)]
        result = calculate_county_ranks(rows)
        assert result[0]["county_rank"] is None

    def test_mixed_floor_only_above_floor_ranked(self):
        good_detail = {
            f"sig_{i}": {"status": "SCORED", "points_awarded": 5, "points_possible": 10, "detail": ""}
            for i in range(5)
        }
        thin_detail = {
            "sig_0": {"status": "SCORED", "points_awarded": 5, "points_possible": 10, "detail": ""},
        }
        rows = [
            _make_row("c1", "Good Co", score_total=40, signal_detail=good_detail),
            _make_row("c2", "Thin Co", score_total=5,  signal_detail=thin_detail),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c1"]["county_rank"] == 1
        assert by_id["c2"]["county_rank"] is None


class TestPublishFlag:
    def test_top_25_are_published(self):
        rows = [_make_row(f"c{i:02d}", f"Firm {i}", score_total=100 - i) for i in range(30)]
        result = calculate_county_ranks(rows)
        published = [r for r in result if r.get("is_published")]
        not_published = [r for r in result if not r.get("is_published")]
        assert len(published) == 25
        assert all(r["county_rank"] <= 25 for r in published)
        assert len(not_published) == 5
        assert all(r["county_rank"] > 25 for r in not_published)

    def test_exactly_25_firms_all_published(self):
        rows = [_make_row(f"c{i:02d}", f"Firm {i}", score_total=100 - i) for i in range(25)]
        result = calculate_county_ranks(rows)
        assert all(r["is_published"] for r in result)


class TestPeerComparisons:
    def test_rank_1_has_no_peers(self):
        rows = [
            _make_row("c1", "Top Firm", score_total=90),
            _make_row("c2", "Mid Firm", score_total=60),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["c1"]["peer_comparisons"] == []

    def test_peers_have_higher_score_than_target(self):
        rows = [
            _make_row("c1", "Firm A", score_total=80),
            _make_row("c2", "Firm B", score_total=60),
            _make_row("c3", "Firm C", score_total=40),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        # Firm C (rank 3) should have peers from above it only.
        peer_names = {p["company_name"] for p in by_id["c3"]["peer_comparisons"]}
        assert "Firm C" not in peer_names
        assert peer_names.issubset({"Firm A", "Firm B"})

    def test_peer_count_capped_at_3(self):
        rows = [_make_row(f"c{i:02d}", f"Firm {i}", score_total=100 - i * 5) for i in range(10)]
        result = calculate_county_ranks(rows)
        # Last firm (rank 10) has 9 possible peers — should return at most 3.
        last = next(r for r in result if r["county_rank"] == 10)
        assert len(last["peer_comparisons"]) <= 3

    def test_peer_stronger_signals_reflects_target_weaknesses(self):
        # Target is weak on website_contact_info; peer is strong there.
        target_detail = {
            "website_owner_page":   {"status": "SCORED", "points_awarded": 14, "points_possible": 14, "detail": ""},
            "website_contact_info": {"status": "SCORED", "points_awarded": 0,  "points_possible": 10, "detail": ""},
            "website_tech_health":  {"status": "SCORED", "points_awarded": 0,  "points_possible": 6,  "detail": ""},
            "website_after_hours":  {"status": "SCORED", "points_awarded": 0,  "points_possible": 8,  "detail": ""},
        }
        peer_detail = {
            "website_owner_page":   {"status": "SCORED", "points_awarded": 14, "points_possible": 14, "detail": ""},
            "website_contact_info": {"status": "SCORED", "points_awarded": 10, "points_possible": 10, "detail": ""},
            "website_tech_health":  {"status": "SCORED", "points_awarded": 6,  "points_possible": 6,  "detail": ""},
            "website_after_hours":  {"status": "SCORED", "points_awarded": 8,  "points_possible": 8,  "detail": ""},
        }
        rows = [
            _make_row("peer", "Strong Peer", score_total=38, signal_detail=peer_detail),
            _make_row("target", "Weak Target", score_total=14, signal_detail=target_detail),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        peers = by_id["target"]["peer_comparisons"]
        assert len(peers) == 1
        assert peers[0]["company_name"] == "Strong Peer"
        # website_contact_info should appear in stronger_signals.
        assert "website_contact_info" in peers[0]["stronger_signals"]
