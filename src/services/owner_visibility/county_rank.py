"""County rank calculator for the Owner Visibility Score engine.

Pure function — no I/O. Given a list of scored-row dicts for a single
(county_slug, month_key) batch, computes:
  - county_rank        (1 = best; None below data floor)
  - county_percentile  (0–100; None below data floor)
  - peer_comparisons   list of 2–3 named peers with higher scores that
                       outperform the target on its 3 weakest signal categories
  - is_published       True when county_rank <= PUBLISH_TOP_N

Data floor: a row needs at least MIN_SCORED_SIGNALS signals with
status == 'SCORED' to receive a rank. Rows below the floor are stored
but show "insufficient data" in the PDF report.
"""

from __future__ import annotations

from typing import Any

_PUBLISH_TOP_N = 25
_MIN_SCORED_SIGNALS = 3
_PEER_COUNT = 3  # how many peers to surface per firm


def _scored_signal_count(signal_detail: dict[str, Any] | None) -> int:
    """Count signals that have status SCORED (regardless of points awarded)."""
    if not signal_detail:
        return 0
    return sum(1 for v in signal_detail.values() if isinstance(v, dict) and v.get("status") == "SCORED")


def _weakest_categories(signal_detail: dict[str, Any] | None, n: int = 3) -> list[str]:
    """Return the n signal names where the firm scored the lowest fraction of possible points."""
    if not signal_detail:
        return []
    scored = [
        (name, d)
        for name, d in signal_detail.items()
        if isinstance(d, dict) and d.get("status") == "SCORED" and d.get("points_possible", 0) > 0
    ]
    # Sort by fraction awarded ascending — worst first.
    scored.sort(key=lambda x: x[1]["points_awarded"] / x[1]["points_possible"])
    return [name for name, _ in scored[:n]]


def _build_peer(peer_row: dict[str, Any], weak_categories: list[str]) -> dict[str, Any]:
    """Build a peer comparison dict highlighting peer's stronger signals."""
    detail = peer_row.get("signal_detail") or {}
    stronger: list[str] = []
    for cat in weak_categories:
        peer_signal = detail.get(cat)
        if isinstance(peer_signal, dict) and peer_signal.get("status") == "SCORED":
            pts_awarded = peer_signal.get("points_awarded", 0)
            pts_possible = peer_signal.get("points_possible", 1)
            if pts_awarded > 0 and pts_possible > 0:
                stronger.append(cat)
    return {
        "company_name": peer_row.get("company_name", ""),
        "score_total": peer_row.get("score_total", 0),
        "stronger_signals": stronger,
    }


def calculate_county_ranks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank all scored rows for one (county_slug, month_key) batch.

    Each input dict must have:
      company_id, company_name, score_total, signal_detail (dict), data_gaps (list)

    Returns the same list with county_rank, county_percentile, peer_comparisons,
    and is_published added in-place. Original list order is preserved.
    """
    if not rows:
        return rows

    # Split into above-floor and below-floor sets.
    above_floor: list[dict[str, Any]] = []
    below_floor: list[dict[str, Any]] = []
    for row in rows:
        if _scored_signal_count(row.get("signal_detail")) >= _MIN_SCORED_SIGNALS:
            above_floor.append(row)
        else:
            below_floor.append(row)

    # Rows below the floor get no rank.
    for row in below_floor:
        row["county_rank"] = None
        row["county_percentile"] = None
        row["peer_comparisons"] = []
        row["is_published"] = False

    if not above_floor:
        return rows

    # Sort descending by score_total; ties broken by data_coverage_pct descending
    # (higher data coverage wins — spec 2.1.2 tie rule), then company_id for stability.
    above_floor.sort(
        key=lambda r: (-r["score_total"], -(r.get("data_coverage_pct") or 0), r["company_id"])
    )

    n = len(above_floor)

    for rank_idx, row in enumerate(above_floor):
        rank = rank_idx + 1  # 1-based
        # Single-firm county: the sole firm is the top in its county → 100th percentile.
        percentile = 100 if n == 1 else round((n - rank) / (n - 1) * 100)

        row["county_rank"] = rank
        row["county_percentile"] = percentile
        row["is_published"] = rank <= _PUBLISH_TOP_N

        # Peer comparisons — firms ranked above this one that score higher.
        weak = _weakest_categories(row.get("signal_detail"))
        peers_above = above_floor[:rank_idx]  # all rows ranked better
        # Prefer peers that are strong on this firm's weak categories.
        peers_above_sorted = sorted(
            peers_above,
            key=lambda p: (
                -sum(
                    1
                    for cat in weak
                    if isinstance((p.get("signal_detail") or {}).get(cat), dict)
                    and (p["signal_detail"][cat].get("points_awarded") or 0) > 0
                ),
                -p["score_total"],
            ),
        )
        row["peer_comparisons"] = [
            _build_peer(p, weak) for p in peers_above_sorted[:_PEER_COUNT]
        ]

    return rows
