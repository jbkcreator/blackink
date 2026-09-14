"""Meeting-attendance proof provider — evidence a demo actually happened.

Rule 2 of the future 4-rule billing gate (Source of Truth item O-10) requires
proof that a meeting really occurred, for at least a minimum duration, with
both parties present — not merely a human's "Held" attestation. No mechanism
for that proof is contracted yet: the decided direction is a conference-
provider duration read (Google Meet / Zoom) layered on the calendar OAuth
tokens the booking engine already holds, with the human attestation as the
fallback when no conference record exists.

This module is the seam for that decision. It follows the same tri-state
vendor-stub convention as src/services/pms_sync.py's PmsProvider and
compliance_gate.py's DncProvider: a provider returns a real reading, or
`None` ("couldn't determine") — never a fabricated present/absent that a
downstream gate could misread as proof. `StubMeetingAttendanceProvider`
(the only implementation today) always returns None, so nothing can claim
verified attendance until a real conference-duration provider is wired in.

The 4-rule billing gate does not exist yet (Week 4). This provides the
capture point and storage so that gate has real evidence to read from,
rather than needing to invent both the schema and the mechanism later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Blueprint Rule-2 threshold: a demo shorter than this does not count as a
# real, billable meeting even if both parties briefly joined.
MIN_QUALIFYING_MEETING_MINUTES = 12


@dataclass(frozen=True)
class AttendanceProof:
	"""One conference provider's reading of a meeting.

	`proof_ref` is the external record's own id/url (e.g. a Google Meet
	conferenceRecord name or a Zoom meeting UUID) — the durable pointer a
	dispute reviewer can follow back to the source, never a value this repo
	fabricates. `both_parties_present` and `duration_minutes` are the raw
	facts; `meets_threshold` is derived, not stored as opinion.
	"""

	proof_ref: str
	proof_source: str  # e.g. "GOOGLE_MEET", "ZOOM", "MANUAL_ATTESTATION"
	duration_minutes: int
	both_parties_present: bool
	verified_at: datetime

	@property
	def meets_threshold(self) -> bool:
		return self.both_parties_present and self.duration_minutes >= MIN_QUALIFYING_MEETING_MINUTES


class MeetingAttendanceProvider(ABC):
	"""Tri-state contract, same shape as pms_sync.py's PmsProvider:
	an AttendanceProof is a definite reading; None means no conference
	record could be found or read for this meeting — NEVER to be read as
	"nobody attended" by any caller. A billing gate must treat None as
	"unproven, do not bill on this rule", not as a negative."""

	@abstractmethod
	def fetch_proof(
		self,
		*,
		client_id: str,
		contact_id: int,
		meeting_occurred_at: datetime,
		conference_ref: Optional[str] = None,
	) -> Optional[AttendanceProof]:
		"""Return attendance proof for the given meeting, or None if no
		conference record could be located/read this attempt."""


class StubMeetingAttendanceProvider(MeetingAttendanceProvider):
	"""No conference-duration integration is wired yet. Always returns
	None — this is what keeps the (not-yet-built) 4-rule billing gate from
	fabricating attendance proof before a real Google Meet / Zoom duration
	read exists. Implementing this ABC for a specific conference provider is
	the follow-up; it is not a schema change."""

	def fetch_proof(
		self,
		*,
		client_id: str,
		contact_id: int,
		meeting_occurred_at: datetime,
		conference_ref: Optional[str] = None,
	) -> Optional[AttendanceProof]:
		return None


def get_attendance_provider() -> MeetingAttendanceProvider:
	"""Single resolution point for the active provider. Returns the stub
	until a real conference-duration provider is contracted and wired here,
	mirroring how compliance_gate.py resolves its DNC/verification stubs."""
	return StubMeetingAttendanceProvider()
