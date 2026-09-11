"""Unit tests for S-8's fix to Evidence Packet §2 (Engagement & Booking
Record) — the gap line must be conditional on real events existing, not an
unconditional literal."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.settlement.evidence import _section_2_engagement


def _fake_session(*, touches_row, booking_row, engagement_row):
    session = MagicMock()

    def _execute(stmt, params=None, *a, **kw):
        sql = str(stmt)
        result = MagicMock()
        if "FROM sequence_runs" in sql:
            result.first.return_value = touches_row
        elif "FROM bookings" in sql:
            result.first.return_value = booking_row
        elif "FROM events" in sql:
            result.first.return_value = engagement_row
        else:
            result.first.return_value = None
        return result

    session.execute.side_effect = _execute
    return session


def test_gap_present_when_no_engagement_events_exist():
    session = _fake_session(
        touches_row=SimpleNamespace(n=3, last_touch="2026-01-01"),
        booking_row=None,
        engagement_row=SimpleNamespace(opens=0, clicks=0, replies=0, first_open=None),
    )
    section = _section_2_engagement(session, SimpleNamespace(company_id="co-1"))
    assert section.gaps == ("DATA GAP: no open/click/reply events recorded for this company's contacts.",)
    labels = [f.label for f in section.fields]
    assert "Total opens" not in labels


def test_gap_absent_and_real_fields_present_when_engagement_exists():
    session = _fake_session(
        touches_row=SimpleNamespace(n=3, last_touch="2026-01-01"),
        booking_row=None,
        engagement_row=SimpleNamespace(opens=2, clicks=1, replies=1, first_open="2026-01-02"),
    )
    section = _section_2_engagement(session, SimpleNamespace(company_id="co-1"))
    assert section.gaps == ()
    by_label = {f.label: f.value for f in section.fields}
    assert by_label["Total opens"] == "2"
    assert by_label["Total clicks"] == "1"
    assert by_label["Replied"] == "Yes"
    assert by_label["First open"] == "2026-01-02"


def test_replied_only_still_counts_as_engagement():
    """Opens/clicks can be zero while a reply still exists (e.g. the
    recipient never fetched the pixel — Apple MPP aside — but did reply)."""
    session = _fake_session(
        touches_row=SimpleNamespace(n=1, last_touch="2026-01-01"),
        booking_row=None,
        engagement_row=SimpleNamespace(opens=0, clicks=0, replies=1, first_open=None),
    )
    section = _section_2_engagement(session, SimpleNamespace(company_id="co-1"))
    assert section.gaps == ()
    by_label = {f.label: f.value for f in section.fields}
    assert by_label["Replied"] == "Yes"
