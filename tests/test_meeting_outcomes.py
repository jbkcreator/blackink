"""Regression tests for the meeting_outcomes freshness guard.

Verifies that submitting or correcting an older meeting does not overwrite
prospect intelligence (PM software, door count, objections) that was already
captured from a more recent meeting.

All tests run without a live database — FakeSession evaluates the
MAX(meeting_occurred_at) guard in Python using the same logic the real
Postgres queries apply.
"""

from datetime import datetime, timezone

import pytest

from src.services.meeting_outcomes import (
    MeetingOutcomeRecord,
    record_meeting_outcome,
)


# ---------------------------------------------------------------------------
# Fake session infrastructure
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one(self):
        return self._value


class FakeSession:
    """In-memory stand-in that evaluates MAX queries against stored outcomes.

    Interprets each execute() call by matching the SQL text, making it
    sensitive to the exact statement strings in meeting_outcomes.py — if
    those strings change, update the matching conditions here accordingly.
    """

    def __init__(self):
        self._outcomes = []   # list of {contact_id, company_id, meeting_occurred_at}
        self._next_id = 1
        self.contact_state = {}  # contact_id -> {"prospect_objections": ...}
        self.company_state = {}  # company_id -> {"current_pm_software": ..., "door_count_est": ...}

    def execute(self, stmt, params=None):
        sql = str(stmt).strip()
        params = params or {}

        if "INSERT INTO meeting_outcomes" in sql:
            oid = self._next_id
            self._next_id += 1
            self._outcomes.append({
                "contact_id": params["contact_id"],
                "company_id": params["company_id"],
                "meeting_occurred_at": params["meeting_occurred_at"],
            })
            return _FakeResult(oid)

        if "MAX(meeting_occurred_at) FROM meeting_outcomes" in sql and "contact_id = :cid" in sql:
            cid = params["cid"]
            times = [o["meeting_occurred_at"] for o in self._outcomes if o["contact_id"] == cid]
            return _FakeResult(max(times) if times else None)

        if "MAX(meeting_occurred_at) FROM meeting_outcomes" in sql and "company_id = :coid" in sql:
            coid = params["coid"]
            times = [o["meeting_occurred_at"] for o in self._outcomes if o["company_id"] == coid]
            return _FakeResult(max(times) if times else None)

        if "UPDATE contacts" in sql:
            cid = params["cid"]
            self.contact_state.setdefault(cid, {})["prospect_objections"] = params.get("objections")
            return _FakeResult(None)

        if "UPDATE companies" in sql:
            coid = params["coid"]
            s = self.company_state.setdefault(coid, {})
            if params.get("pm_software") is not None:
                s["current_pm_software"] = params["pm_software"]
            if params.get("door_count") is not None:
                s["door_count_est"] = params["door_count"]
            return _FakeResult(None)

        return _FakeResult(None)


def _rec(**overrides) -> MeetingOutcomeRecord:
    base = dict(
        client_id="client_abc",
        contact_id=1,
        company_id="co_xyz",
        meeting_occurred_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        attendance_status="ATTENDED",
        pm_software_stated="AppFolio",
        door_count_stated=80,
        objections_stated="Price concern",
    )
    base.update(overrides)
    return MeetingOutcomeRecord(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_submission_mirrors_unconditionally():
    session = FakeSession()
    record_meeting_outcome(session, _rec(
        pm_software_stated="AppFolio",
        door_count_stated=80,
        objections_stated="Too busy right now",
    ))

    assert session.company_state["co_xyz"]["current_pm_software"] == "AppFolio"
    assert session.company_state["co_xyz"]["door_count_est"] == 80
    assert session.contact_state[1]["prospect_objections"] == "Too busy right now"


def test_newer_submission_overwrites_older_in_order():
    session = FakeSession()
    ts_a = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)

    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_a,
        pm_software_stated="AppFolio",
        door_count_stated=80,
        objections_stated="First objection",
    ))
    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_b,
        pm_software_stated="Buildium",
        door_count_stated=120,
        objections_stated="Second objection",
    ))

    assert session.company_state["co_xyz"]["current_pm_software"] == "Buildium"
    assert session.company_state["co_xyz"]["door_count_est"] == 120
    assert session.contact_state[1]["prospect_objections"] == "Second objection"


def test_stale_submission_does_not_overwrite_newer_intelligence():
    """Core regression: submitting or correcting an older meeting must not
    clobber PM software, door count, or objections already captured from a
    more recent meeting."""
    session = FakeSession()
    ts_newer = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    ts_older = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    # Newer meeting submitted first (normal flow)
    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_newer,
        pm_software_stated="Buildium",
        door_count_stated=120,
        objections_stated="Will think about it",
    ))
    # Late entry or correction of an earlier meeting submitted second
    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_older,
        pm_software_stated="AppFolio",
        door_count_stated=40,
        objections_stated="Stale objection from old meeting",
    ))

    assert session.company_state["co_xyz"]["current_pm_software"] == "Buildium", (
        "Stale submission must not overwrite PM software captured from the newer meeting"
    )
    assert session.company_state["co_xyz"]["door_count_est"] == 120, (
        "Stale submission must not overwrite door count captured from the newer meeting"
    )
    assert session.contact_state[1]["prospect_objections"] == "Will think about it", (
        "Stale submission must not overwrite objections captured from the newer meeting"
    )


def test_stale_submission_still_inserts_its_own_row():
    """Even when the mirror is suppressed, the outcome row itself is always
    recorded — the historical record is complete regardless of submission order."""
    session = FakeSession()
    ts_newer = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    ts_older = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    record_meeting_outcome(session, _rec(meeting_occurred_at=ts_newer))
    record_meeting_outcome(session, _rec(meeting_occurred_at=ts_older))

    assert len(session._outcomes) == 2


def test_equal_timestamps_both_mirror():
    """Two meetings at the exact same timestamp — neither should suppress the
    other's mirror (>= guard, not >)."""
    session = FakeSession()
    ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    record_meeting_outcome(session, _rec(meeting_occurred_at=ts, pm_software_stated="AppFolio"))
    record_meeting_outcome(session, _rec(meeting_occurred_at=ts, pm_software_stated="Buildium"))

    # Both fired; last write wins
    assert session.company_state["co_xyz"]["current_pm_software"] == "Buildium"


def test_null_stated_fields_do_not_erase_existing_values():
    """A submission where the rep did not capture PM software or door count
    (None) must not NULL out values already on the company row.
    The COALESCE in _mirror_company handles this at the SQL level; the
    FakeSession simulates it by skipping None updates."""
    session = FakeSession()
    ts_a = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)

    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_a,
        pm_software_stated="AppFolio",
        door_count_stated=80,
    ))
    record_meeting_outcome(session, _rec(
        meeting_occurred_at=ts_b,
        pm_software_stated=None,
        door_count_stated=None,
    ))

    assert session.company_state["co_xyz"].get("current_pm_software") == "AppFolio"
    assert session.company_state["co_xyz"].get("door_count_est") == 80


def test_invalid_attendance_status_raises():
    session = FakeSession()
    with pytest.raises(ValueError, match="attendance_status"):
        record_meeting_outcome(session, _rec(attendance_status="MAYBE"))


def test_outcome_id_increments():
    session = FakeSession()
    ts_a = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    id_a = record_meeting_outcome(session, _rec(meeting_occurred_at=ts_a))
    id_b = record_meeting_outcome(session, _rec(meeting_occurred_at=ts_b))

    assert id_b > id_a
