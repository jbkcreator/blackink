"""Tests for src/services/ovs_lookup.py — guarded OVS read + card rendering."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.services.ovs_lookup import fetch_latest_ovs, ovs_card_lines


def _session(regclass, row):
    s = MagicMock()
    scalar_res = MagicMock()
    scalar_res.scalar.return_value = regclass
    row_res = MagicMock()
    row_res.mappings.return_value.first.return_value = row
    s.execute.side_effect = [scalar_res, row_res]
    return s


def test_fetch_none_when_company_id_missing():
    assert fetch_latest_ovs(MagicMock(), None) is None


def test_fetch_none_when_table_absent():
    s = MagicMock()
    res = MagicMock()
    res.scalar.return_value = None  # to_regclass → None ⇒ table absent
    s.execute.return_value = res
    assert fetch_latest_ovs(s, "co-1") is None


def test_fetch_none_when_no_score_row():
    s = _session(regclass="owner_visibility_scores", row=None)
    assert fetch_latest_ovs(s, "co-1") is None


def test_fetch_returns_score_rank_and_peers():
    row = {
        "score_total": 76,
        "county_rank": 4,
        "peer_comparisons": [
            {"company_name": "Rival PM", "stronger_signals": ["review_response_rate"]},
        ],
    }
    s = _session(regclass="owner_visibility_scores", row=row)
    ovs = fetch_latest_ovs(s, "co-1")
    assert ovs["score_total"] == 76
    assert ovs["county_rank"] == 4
    assert ovs["peers"][0]["company_name"] == "Rival PM"


def test_card_lines_empty_when_no_ovs():
    assert ovs_card_lines(None) == []


def test_card_lines_render_score_rank_peers():
    lines = ovs_card_lines({
        "score_total": 76, "county_rank": 4,
        "peers": [{"company_name": "Rival PM", "stronger_signals": ["reviews"]}],
    })
    text = "\n".join(lines)
    assert "76/100" in text
    assert "#4" in text
    assert "Rival PM" in text
