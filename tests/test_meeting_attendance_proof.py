"""Attendance-proof provider seam — the tri-state stub and threshold rule."""
from datetime import datetime, timezone

from src.services.meeting_attendance_proof import (
	MIN_QUALIFYING_MEETING_MINUTES,
	AttendanceProof,
	StubMeetingAttendanceProvider,
	get_attendance_provider,
)


def test_stub_provider_returns_none():
	# The only wired provider today fabricates no reading — a not-yet-built
	# 4-rule billing gate must treat this as "unproven", never "absent".
	provider = StubMeetingAttendanceProvider()
	assert provider.fetch_proof(
		client_id="acme", contact_id=1, meeting_occurred_at=datetime.now(timezone.utc)
	) is None


def test_default_provider_is_stub():
	assert isinstance(get_attendance_provider(), StubMeetingAttendanceProvider)


def test_meets_threshold_requires_both_present_and_min_duration():
	now = datetime.now(timezone.utc)
	long_both = AttendanceProof("ref", "GOOGLE_MEET", MIN_QUALIFYING_MEETING_MINUTES, True, now)
	assert long_both.meets_threshold is True

	short_both = AttendanceProof("ref", "GOOGLE_MEET", MIN_QUALIFYING_MEETING_MINUTES - 1, True, now)
	assert short_both.meets_threshold is False

	long_solo = AttendanceProof("ref", "GOOGLE_MEET", 30, False, now)
	assert long_solo.meets_threshold is False
