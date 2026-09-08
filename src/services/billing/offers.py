"""entitlement_offers reads + the founding-flag rate-migration entry point
(Subtask 1.2.3, rules 5 and 6).

Mirrors src/services/settlement/ledger.py::load_offer_terms's dataclass shape
and error convention — an unknown/disabled offer is a caller-visible None /
typed error, never a silent default price.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.events import log_event

logger = logging.getLogger(__name__)


class OfferNotEnabledError(RuntimeError):
	"""offer_code exists but is_enabled = FALSE — nothing may be charged."""


@dataclass(frozen=True)
class Offer:
	offer_code: str
	display_name: str
	price_cents: int
	per_sit_cents: Optional[int]
	billing_model: str
	monthly_cap: Optional[int]
	is_default: bool
	is_enabled: bool


def load_offer(session: Session, offer_code: str) -> Optional[Offer]:
	row = session.execute(
		text(
			"SELECT offer_code, display_name, price_cents, per_sit_cents, billing_model, "
			"       monthly_cap, is_default, is_enabled "
			"FROM entitlement_offers WHERE offer_code = :offer_code"
		),
		{"offer_code": offer_code},
	).first()
	if row is None:
		return None
	return Offer(
		offer_code=row.offer_code,
		display_name=row.display_name,
		price_cents=row.price_cents,
		per_sit_cents=row.per_sit_cents,
		billing_model=row.billing_model,
		monthly_cap=row.monthly_cap,
		is_default=row.is_default,
		is_enabled=row.is_enabled,
	)


def create_client_entitlement(
	session: Session, *, client_id: str, offer_code: str, activated_at: Optional[datetime] = None
) -> int:
	"""The one write path that provisions a client_entitlements row.
	Snapshots entitlement_offers.price_cents (as it stands right now) into
	locked_price_cents — the per-account effective price that
	apply_rate_migration() below reads and updates. Without this snapshot,
	rule 6 ("founding accounts never move") has nothing to freeze: a single
	shared entitlement_offers.price_cents row was the ONLY price in an
	earlier version of this module, so every account — founding or not —
	moved on every rate migration by construction. locked_price_cents is
	what makes "unchanged" a real, checkable state per account."""
	offer = load_offer(session, offer_code)
	if offer is None:
		raise ValueError(f"unknown offer_code={offer_code!r}")
	row = session.execute(
		text(
			"INSERT INTO client_entitlements (client_id, offer_code, activated_at, locked_price_cents) "
			"VALUES (:client_id, :offer_code, COALESCE(:activated_at, NOW()), :locked_price_cents) "
			"RETURNING entitlement_id"
		),
		{"client_id": client_id, "offer_code": offer_code, "activated_at": activated_at, "locked_price_cents": offer.price_cents},
	).one()
	return row.entitlement_id


@dataclass(frozen=True)
class RateMigrationResult:
	offer_code: str
	new_price_cents: int
	clients_updated: int
	founding_skipped: int


def apply_rate_migration(
	session: Session, *, offer_code: str, new_price_cents: int, as_of: datetime
) -> RateMigrationResult:
	"""Rule 6 — 'founding = true' flag. Updates entitlement_offers.price_cents
	(the list price shown on the offer sheet / applied to any NEW
	entitlement created after this point) AND every non-founding ACTIVE
	client_entitlements.locked_price_cents for this offer — but leaves a
	FOUNDING client's locked_price_cents completely untouched, so its
	per-account effective price genuinely does not move.

	A founding client's entitlement was created (create_client_entitlement,
	above) with locked_price_cents snapshotted from whatever the list price
	was at signup — this function never rewrites that value for a founding
	row, at this migration or any future one. That snapshot, never touched
	again, IS "founding accounts never move" — there is no other place in
	this schema a per-account price could live.

	Note on the DoD's own wording: "Billing job contains a `WHERE founding =
	TRUE` predicate that skips price migration" describes what the predicate
	is FOR, not its literal form — the UPDATE below carries
	`WHERE NOT c.founding`, which is the clause that actually EXCLUDES
	founding rows from being written.
	"""
	offer = load_offer(session, offer_code)
	if offer is None:
		raise ValueError(f"unknown offer_code={offer_code!r}")

	session.execute(
		text("UPDATE entitlement_offers SET price_cents = :price_cents, updated_at = NOW() WHERE offer_code = :offer_code"),
		{"price_cents": new_price_cents, "offer_code": offer_code},
	)

	updated = session.execute(
		text(
			"UPDATE client_entitlements ce SET locked_price_cents = :price_cents, updated_at = :as_of "
			"FROM clients c "
			"WHERE ce.client_id = c.client_id AND ce.offer_code = :offer_code AND ce.status = 'ACTIVE' "
			"  AND NOT c.founding"
		),
		{"price_cents": new_price_cents, "as_of": as_of, "offer_code": offer_code},
	)
	founding_count = session.execute(
		text(
			"SELECT COUNT(*) AS n FROM client_entitlements ce JOIN clients c ON c.client_id = ce.client_id "
			"WHERE ce.offer_code = :offer_code AND ce.status = 'ACTIVE' AND c.founding"
		),
		{"offer_code": offer_code},
	).one()

	result = RateMigrationResult(
		offer_code=offer_code,
		new_price_cents=new_price_cents,
		clients_updated=updated.rowcount,
		founding_skipped=founding_count.n,
	)
	log_event(
		"BLACKINK_INTERNAL_SALES", "rate_migration_applied", entity_type="entitlement_offer",
		entity_id=offer_code,
		payload={
			"offer_code": offer_code, "new_price_cents": new_price_cents,
			"clients_updated": result.clients_updated, "founding_skipped": result.founding_skipped,
		},
		session=session,
	)
	logger.info(
		"billing.apply_rate_migration: offer=%s new_price_cents=%d clients_updated=%d founding_skipped=%d",
		offer_code, new_price_cents, result.clients_updated, result.founding_skipped,
	)
	return result
