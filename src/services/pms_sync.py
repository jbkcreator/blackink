"""PMS verification provider (Subtask 1.2.2).

No PMS integration is contracted anywhere in this repo — the blueprint's
"nightly PMS sync confirms door_signed" has never had a real data source
(see migrations/apply_pms_agreements.py's docstring). This follows the
same tri-state vendor-stub convention as src/services/compliance_gate.py's
DncProvider: an ABC whose stub returns None ("couldn't determine") rather
than a false True/False, so callers must never treat "no answer" as either
a positive or negative signal.

With StubPmsProvider wired in (the only implementation today),
fetch_new_agreements() and is_agreement_active() both return None — so out
of the box no settlement opens from a real door_signed event, and no day-60
re-verification ever charges or claws back on a guess. Landing a real PMS
integration is a follow-up that implements this ABC for a specific,
contracted PMS vendor; it is not a schema or pipeline change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class AgreementRecord:
	"""One newly-signed management agreement as reported by a PMS sync."""

	external_ref: str
	owner_email: Optional[str]
	owner_domain: Optional[str]
	pms_property_ref: Optional[str]
	door_count: int
	door_signed_at: datetime
	terminated_at: Optional[datetime]


class PmsProvider(ABC):
	"""Tri-state contract, same shape as compliance_gate.py's DncProvider:
	True/False is a definite answer; None means the PMS could not be
	reached or the agreement's status could not be determined — NEVER to
	be read as "not signed" / "not terminated" by any caller."""

	@abstractmethod
	def fetch_new_agreements(self, client_id: str) -> Optional[list[AgreementRecord]]:
		"""Return newly-confirmed door_signed agreements since the last poll,
		or None if the PMS could not be reached at all this sweep tick."""

	@abstractmethod
	def is_agreement_active(self, external_ref: str) -> Optional[bool]:
		"""True: still an active management agreement. False: confirmed
		terminated. None: could not be determined this sweep tick — the
		caller must defer, never charge or clawback on this reading."""


class StubPmsProvider(PmsProvider):
	"""No PMS vendor is contracted. Always returns "couldn't determine" —
	this is what keeps the day-0 trigger and day-60 re-verification from
	fabricating an answer in the absence of a real integration."""

	def fetch_new_agreements(self, client_id: str) -> Optional[list[AgreementRecord]]:
		return None

	def is_agreement_active(self, external_ref: str) -> Optional[bool]:
		return None
