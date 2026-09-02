"""Ghost-Shopper audit report — deterministic, no LLM.

Reads PM speed metrics written by the Ghost-Shopper crawler into the events
ledger and exposes them as structured data for:
  - Outbound template merge tags: {audit_speed}, {loss_dollars}
  - Week 2 Dynamic Evidence Packet PDF (4-section composition)

Data flow:
  Ghost-Shopper crawler → events (event_type='ghost_shopper_audit',
  payload={'audit_speed_score_sec': N, 'audit_loss_dollars_est': N})
  → get_audit_metrics() → AuditMetrics
  → build_audit_context() → dict for outbound_templates.resolve_tags()

No LLM involved. All calculations are deterministic — the crawler owns the
loss estimate formula (response latency × lead volume × avg deal value).
This module only reads, formats, and asserts completeness.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_AUDIT_EVENT_TYPE = "ghost_shopper_audit"

# Industry benchmark: PMs that respond within 1 hour win 72% of inquiries
# (internal heuristic — used only for the speed grade label, not the loss $).
_BENCHMARK_RESPONSE_SEC = 3600  # 1 hour


@dataclass(frozen=True)
class AuditMetrics:
    contact_id: int
    audit_event_id: str
    speed_score_sec: float          # response latency captured by crawler
    loss_dollars_est: float         # annual revenue loss estimate
    speed_grade: str                # FAST / AVERAGE / SLOW / CRITICAL
    speed_label: str                # human-readable, e.g. "4m 32s"


class AuditDataMissing(ValueError):
    """Contact has no completed ghost-shopper audit — not ready for outreach."""


# ---------------------------------------------------------------------------
# Speed grade — deterministic bucketing
# ---------------------------------------------------------------------------

def _speed_grade(speed_sec: float) -> str:
    if speed_sec <= 300:       # ≤5 min
        return "FAST"
    if speed_sec <= 3600:      # ≤1 hour
        return "AVERAGE"
    if speed_sec <= 86400:     # ≤24 hours
        return "SLOW"
    return "CRITICAL"          # >24 hours


def _speed_label(speed_sec: float) -> str:
    """Convert seconds to concise human label for merge tags."""
    sec = int(speed_sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


# ---------------------------------------------------------------------------
# DB reader
# ---------------------------------------------------------------------------

def get_audit_metrics(session: Session, contact_id: int) -> Optional[AuditMetrics]:
    """Return the most recent ghost-shopper audit metrics for a contact.

    Returns None if no audit has been completed yet (crawler hasn't run).
    """
    row = session.execute(
        text(
            "SELECT id, payload "
            "FROM events "
            "WHERE entity_type = 'contact' "
            "  AND entity_id = :entity_id "
            "  AND event_type = :event_type "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"entity_id": str(contact_id), "event_type": _AUDIT_EVENT_TYPE},
    ).fetchone()

    if row is None:
        return None

    event_id, payload = row
    payload = payload or {}

    speed_sec = payload.get("audit_speed_score_sec")
    loss_est = payload.get("audit_loss_dollars_est")

    if speed_sec is None or loss_est is None:
        logger.warning(
            "audit_report: incomplete payload for contact_id=%s event_id=%s",
            contact_id, event_id,
        )
        return None

    speed_sec = float(speed_sec)
    loss_est = float(loss_est)

    return AuditMetrics(
        contact_id=contact_id,
        audit_event_id=str(event_id),
        speed_score_sec=speed_sec,
        loss_dollars_est=loss_est,
        speed_grade=_speed_grade(speed_sec),
        speed_label=_speed_label(speed_sec),
    )


def assert_audit_complete(contact_id: int, metrics: Optional[AuditMetrics]) -> AuditMetrics:
    """Raise AuditDataMissing if metrics are absent.

    Called before outbound dispatch — a contact with no audit data must not
    receive Email 1 (Speed Loss Audit) because the merge tags would be empty.
    """
    if metrics is None:
        raise AuditDataMissing(
            f"contact_id={contact_id} has no completed ghost-shopper audit. "
            "Contact is not ready for outbound dispatch — crawler must run first."
        )
    return metrics


# ---------------------------------------------------------------------------
# Merge tag context builder
# ---------------------------------------------------------------------------

def build_audit_context(metrics: AuditMetrics) -> dict:
    """Return dict for outbound_templates.resolve_tags() merge substitution.

    Keys match ALLOWED_TAGS in outbound_templates.py:
      {audit_speed}  → e.g. "4h 12m"
      {loss_dollars} → e.g. "47200"
    """
    return {
        "audit_speed": metrics.speed_label,
        "loss_dollars": str(int(metrics.loss_dollars_est)),
    }


# ---------------------------------------------------------------------------
# Evidence packet section builders (composable for Week 2 PDF)
# ---------------------------------------------------------------------------

def section_speed_summary(metrics: AuditMetrics) -> dict:
    """Section 1 of the Dynamic Evidence Packet: speed audit result."""
    return {
        "section": "speed_summary",
        "speed_score_sec": metrics.speed_score_sec,
        "speed_label": metrics.speed_label,
        "speed_grade": metrics.speed_grade,
        "benchmark_sec": _BENCHMARK_RESPONSE_SEC,
        "delta_sec": max(0.0, metrics.speed_score_sec - _BENCHMARK_RESPONSE_SEC),
    }


def section_loss_estimate(metrics: AuditMetrics) -> dict:
    """Section 2 of the Dynamic Evidence Packet: annual revenue loss estimate."""
    return {
        "section": "loss_estimate",
        "loss_dollars_est": metrics.loss_dollars_est,
        "loss_label": f"${int(metrics.loss_dollars_est):,}",
    }
