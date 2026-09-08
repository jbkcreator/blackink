"""Integration-style tests for OVS county ranking — Subtask 2.1.2.

All tests use only the pure calculate_county_ranks() function and hand-
crafted row dicts — no DB or HTTP required. They cover the DoD scenarios
from Week1_Tasks_Dev_Split_v2.md §2.1.2:

  - 30-firm county seeded with distinct scores; top-25 boundary correct
  - Firm with only 2 scored signals stays below data floor
  - Events payload carries score_total, data_coverage_pct, county_rank,
    county_percentile, peer_comparisons (spot-checked via a fake _update call)
  - Tie broken by data_coverage_pct DESC before company_id
"""
import json
import unittest.mock as mock

import pytest

from src.services.owner_visibility.county_rank import calculate_county_ranks


# ── Row builder ──────────────────────────────────────────────────────────────

def _make_row(
    company_id: str,
    score_total: int,
    scored_signals: int = 8,
    data_coverage_pct: int | None = None,
    owning_client_id: str | None = None,
) -> dict:
    """Minimal row dict matching what _update_county_ranks() passes to calculate_county_ranks()."""
    signal_detail = {
        f"sig_{i}": {
            "status": "SCORED",
            "points_awarded": score_total // max(scored_signals, 1),
            "points_possible": 12,
            "detail": "ok",
        }
        for i in range(scored_signals)
    }
    if data_coverage_pct is None:
        data_coverage_pct = round(scored_signals / 10 * 100)
    return {
        "score_id":          abs(hash(company_id)),
        "company_id":        company_id,
        "company_name":      f"Firm {company_id}",
        "score_total":       score_total,
        "signal_detail":     signal_detail,
        "data_coverage_pct": data_coverage_pct,
        "owning_client_id":  owning_client_id,
    }


# ── 30-firm county — top-25 boundary ─────────────────────────────────────────

class TestThirtyFirmCounty:
    def _build_county(self) -> list[dict]:
        # 30 firms with distinct scores 70–99 (all well above data floor).
        return [_make_row(f"co{i:02d}", score_total=99 - i) for i in range(30)]

    def test_rank_1_is_highest_score(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["co00"]["county_rank"] == 1

    def test_rank_30_is_lowest_score(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["co29"]["county_rank"] == 30

    def test_exactly_25_firms_published(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        published = [r for r in result if r["is_published"]]
        assert len(published) == 25
        assert all(r["county_rank"] <= 25 for r in published)

    def test_firms_26_through_30_not_published(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        unpublished = [r for r in result if not r["is_published"]]
        assert len(unpublished) == 5
        assert all(r["county_rank"] > 25 for r in unpublished)

    def test_all_30_firms_receive_a_rank(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        assert all(r["county_rank"] is not None for r in result)

    def test_rank_1_is_100th_percentile(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        top = next(r for r in result if r["county_rank"] == 1)
        assert top["county_percentile"] == 100

    def test_rank_30_is_0th_percentile(self):
        rows = self._build_county()
        result = calculate_county_ranks(rows)
        bottom = next(r for r in result if r["county_rank"] == 30)
        assert bottom["county_percentile"] == 0


# ── Data floor (< 3 SCORED signals) ──────────────────────────────────────────

class TestInsufficientData:
    def test_two_signal_firm_gets_no_rank(self):
        rows = [
            _make_row("thin", score_total=20, scored_signals=2),
            _make_row("good", score_total=60, scored_signals=8),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["thin"]["county_rank"] is None
        assert by_id["thin"]["county_percentile"] is None
        assert by_id["thin"]["is_published"] is False

    def test_two_signal_firm_does_not_affect_good_firms_rank(self):
        rows = [
            _make_row("thin", score_total=99, scored_signals=2),  # high score but no floor
            _make_row("good", score_total=60, scored_signals=8),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["good"]["county_rank"] == 1

    def test_all_below_floor_produces_no_ranked_rows(self):
        rows = [_make_row(f"c{i}", score_total=40, scored_signals=1) for i in range(5)]
        result = calculate_county_ranks(rows)
        assert all(r["county_rank"] is None for r in result)


# ── Tie-breaking by data_coverage_pct ────────────────────────────────────────

class TestTieBreakerCoverage:
    def test_higher_coverage_wins_on_tied_score(self):
        rows = [
            _make_row("lo_cov", score_total=50, data_coverage_pct=30),
            _make_row("hi_cov", score_total=50, data_coverage_pct=80),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["hi_cov"]["county_rank"] == 1
        assert by_id["lo_cov"]["county_rank"] == 2

    def test_company_id_is_final_tiebreaker_when_coverage_also_tied(self):
        rows = [
            _make_row("zzz", score_total=50, data_coverage_pct=70),
            _make_row("aaa", score_total=50, data_coverage_pct=70),
        ]
        result = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in result}
        assert by_id["aaa"]["county_rank"] == 1
        assert by_id["zzz"]["county_rank"] == 2


# ── Events payload structure ──────────────────────────────────────────────────

class TestEventsPayload:
    """Verify the payload the sweep writes to the events table.

    We call calculate_county_ranks() directly and then simulate the payload-
    building logic from _update_county_ranks() to confirm the required fields
    are present and correctly typed — no DB required.
    """

    def _build_payload(self, row: dict, county_slug: str, month_key: str) -> dict:
        return {
            "score_total":       row["score_total"],
            "county_slug":       county_slug,
            "data_coverage_pct": row["data_coverage_pct"],
            "county_rank":       row["county_rank"],
            "county_percentile": row["county_percentile"],
            "is_published":      row.get("is_published", False),
            "peer_comparisons":  row["peer_comparisons"][:3],
            "month_key":         month_key,
        }

    def test_event_payload_has_required_fields(self):
        rows = [
            _make_row("c1", score_total=75, owning_client_id="tenant-001"),
            _make_row("c2", score_total=50, owning_client_id="tenant-001"),
        ]
        ranked = calculate_county_ranks(rows)
        by_id = {r["company_id"]: r for r in ranked}

        payload = self._build_payload(by_id["c1"], county_slug="orange", month_key="2026-09")
        assert payload["county_rank"] == 1
        assert payload["county_slug"] == "orange"
        assert payload["month_key"] == "2026-09"
        assert isinstance(payload["data_coverage_pct"], int)
        assert isinstance(payload["peer_comparisons"], list)
        assert isinstance(payload["is_published"], bool)

    def test_event_payload_is_json_serialisable(self):
        rows = [_make_row(f"c{i}", score_total=80 - i * 5) for i in range(5)]
        ranked = calculate_county_ranks(rows)
        for row in ranked:
            if row["county_rank"] is None:
                continue
            payload = self._build_payload(row, county_slug="miami-dade", month_key="2026-09")
            json.dumps(payload)  # raises if not serialisable

    def test_unowned_firms_would_be_skipped_in_events(self):
        rows = [
            _make_row("owned",    score_total=80, owning_client_id="tenant-001"),
            _make_row("prospect", score_total=60, owning_client_id=None),
        ]
        ranked = calculate_county_ranks(rows)
        firms_with_events = [
            r for r in ranked
            if r.get("owning_client_id") and r["county_rank"] is not None
        ]
        assert len(firms_with_events) == 1
        assert firms_with_events[0]["company_id"] == "owned"

    def test_peer_comparisons_capped_at_3_in_payload(self):
        rows = [_make_row(f"c{i:02d}", score_total=100 - i * 5) for i in range(10)]
        ranked = calculate_county_ranks(rows)
        last = next(r for r in ranked if r["county_rank"] == 10)
        payload = self._build_payload(last, county_slug="broward", month_key="2026-09")
        assert len(payload["peer_comparisons"]) <= 3
