"""50/50 split math and the 60-day clawback decision (Subtask 1.2.2).

Pure — no session, no I/O, no Stripe. This is the ONLY place either rule is
expressed; migrations/apply_settlement_ledger.py's trigger enforces the
result at the DB layer but does not compute it.

Both pricing bases from settlement_offer_config are supported here because
the blueprint contradicts itself: its printed settlement DDL bills
total_bounty_cents per door, but its own pricing registry prices
`appt_standard` as a flat fee "Any door count. Replaces all door-band
pricing." Which basis applies to which offer is a commercial decision for
the client, not something this module resolves — it only computes
correctly under whichever basis a given offer_code is configured with.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

PER_DOOR = "PER_DOOR"
FLAT_PER_AGREEMENT = "FLAT_PER_AGREEMENT"

CHARGE = "CHARGE"
VOID_CLAWBACK = "VOID_CLAWBACK"
DEFER_UNVERIFIED = "DEFER_UNVERIFIED"


class SettlementNotEnabledError(RuntimeError):
	"""offer_code has settlement_enabled = FALSE — nothing may be charged."""


class SettlementNotPricedError(RuntimeError):
	"""offer_code's configured pricing_basis has no amount set for it."""


@dataclass(frozen=True)
class OfferTerms:
	offer_code: str
	settlement_enabled: bool
	pricing_basis: str
	per_door_bounty_cents: int | None
	flat_bounty_cents: int | None
	installment_1_bps: int
	clawback_window_days: int


@dataclass(frozen=True)
class SplitPlan:
	total_bounty_cents: int
	installment_1_cents: int
	installment_2_cents: int
	installment_2_scheduled_for: datetime


def compute_split(terms: OfferTerms, *, door_count: int, door_signed_at: datetime) -> SplitPlan:
	"""Resolve total_bounty_cents by pricing_basis, then split 50/50 (or
	whatever installment_1_bps is configured to) with the remainder always
	landing on installment 2 — so an odd-cent bounty can never violate the
	ck_settlement_split_sums CHECK (installment_1 + installment_2 = total)."""
	if not terms.settlement_enabled:
		raise SettlementNotEnabledError(f"offer_code={terms.offer_code!r} is not settlement_enabled")

	if terms.pricing_basis == PER_DOOR:
		if terms.per_door_bounty_cents is None:
			raise SettlementNotPricedError(
				f"offer_code={terms.offer_code!r} is PER_DOOR but per_door_bounty_cents is unset"
			)
		total_bounty_cents = terms.per_door_bounty_cents * door_count
	elif terms.pricing_basis == FLAT_PER_AGREEMENT:
		if terms.flat_bounty_cents is None:
			raise SettlementNotPricedError(
				f"offer_code={terms.offer_code!r} is FLAT_PER_AGREEMENT but flat_bounty_cents is unset"
			)
		# door_count is deliberately NOT a multiplier under this basis — it is
		# recorded on the ledger and printed on the Evidence Packet as
		# evidence only.
		total_bounty_cents = terms.flat_bounty_cents
	else:
		raise ValueError(f"unknown pricing_basis {terms.pricing_basis!r}")

	installment_1_cents = total_bounty_cents * terms.installment_1_bps // 10_000
	installment_2_cents = total_bounty_cents - installment_1_cents

	return SplitPlan(
		total_bounty_cents=total_bounty_cents,
		installment_1_cents=installment_1_cents,
		installment_2_cents=installment_2_cents,
		installment_2_scheduled_for=door_signed_at + timedelta(days=terms.clawback_window_days),
	)


def decide_installment_2(
	*,
	door_signed_at: datetime,
	clawback_window_days: int,
	terminated_at: datetime | None,
	agreement_still_active: bool | None,
	as_of: datetime,
) -> str:
	"""The only place the 60-day clawback rule is expressed.

	agreement_still_active is the PmsProvider's tri-state day-60
	re-verification result: True (still active), False (confirmed
	terminated), or None (couldn't determine). A None must never be read as
	either CHARGE or VOID_CLAWBACK — src/services/settlement/clawback.py
	must never cache or otherwise treat it as a definite answer.

	terminated_at, when set, is authoritative regardless of what the
	provider currently reports (a booking-ingest-style event may have
	recorded the termination already) — it takes precedence over
	agreement_still_active.

	Boundary: termination exactly AT the day-60 deadline (terminated_at ==
	door_signed_at + clawback_window_days) is treated as OUTSIDE the
	clawback window — the window is "terminated before day 60 voids it",
	so a termination landing exactly on day 60 no longer voids and the
	charge proceeds if the provider confirms active, or defers if it
	cannot be determined.
	"""
	clawback_deadline = door_signed_at + timedelta(days=clawback_window_days)

	if terminated_at is not None and terminated_at < clawback_deadline:
		return VOID_CLAWBACK
	if agreement_still_active is None:
		return DEFER_UNVERIFIED
	if agreement_still_active is False:
		return VOID_CLAWBACK
	return CHARGE
