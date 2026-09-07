"""Read-only lookup of Owner Visibility Score for card enrichment.

The OVS engine + `owner_visibility_scores` table are Dev-2's (Task 2.1), landing
on a different line than this branch's base. So this read is deliberately
loosely coupled and defensive: if the table doesn't exist yet (or the firm has
no score row), it returns None and the card simply omits the OVS section
("show if available", v2 §3.1.2/§3.1.3 DoD). No import of the engine.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text


def fetch_latest_ovs(session, company_id: Optional[str]) -> Optional[dict]:
    """Latest Owner Visibility Score for a company, or None.

    Returns {score_total, county_rank, peers: [{company_name, stronger_signals}]}
    where peers reflect the firm's 3 weakest categories (named local competitors
    who beat it there). None when the table is absent or no score exists.
    """
    if not company_id:
        return None
    # Table may not exist on this DB yet — guard rather than raise.
    if session.execute(text("SELECT to_regclass('owner_visibility_scores')")).scalar() is None:
        return None

    row = session.execute(
        text(
            "SELECT score_total, county_rank, peer_comparisons "
            "FROM owner_visibility_scores WHERE company_id = :cid "
            "ORDER BY month_key DESC LIMIT 1"
        ),
        {"cid": company_id},
    ).mappings().first()
    if row is None:
        return None

    peers = row["peer_comparisons"] if isinstance(row["peer_comparisons"], list) else []
    return {
        "score_total": row["score_total"],
        "county_rank": row["county_rank"],
        "peers": [
            {"company_name": p.get("company_name", ""), "stronger_signals": p.get("stronger_signals", [])}
            for p in peers
            if isinstance(p, dict)
        ][:3],
    }


def ovs_card_lines(ovs: Optional[dict]) -> list[str]:
    """Render an OVS dict into card lines. Empty list when no score available."""
    if not ovs:
        return []
    lines = []
    score = ovs.get("score_total")
    rank = ovs.get("county_rank")
    head = f"*OVS* {score}/100" if score is not None else "*OVS* —"
    if rank:
        head += f"  ·  county rank #{rank}"
    lines.append(head)
    for p in ovs.get("peers", []):
        name = p.get("company_name") or "a local peer"
        sig = ", ".join(p.get("stronger_signals", [])) or "overall"
        lines.append(f"• {name} stronger on: {sig}")
    return lines
