"""Rule 3 — 60-day guarantee (Subtask 1.2.3).

"If an Owner Growth or Full County account has fewer than four attended
qualified sits at day 60, the next month's subscription bills at $0.
One-time per account." — Respond accounts are explicitly excluded
(docs/Sept04_New_Items_Triage.md, "60-Day Guarantee" item, DoD line
"Only Owner Growth and Full County accounts are eligible — Respond accounts
excluded").

DB-only override (per the decision recorded in the plan): this writes a
subscription_overrides row a billing job is expected to honor. No real
Stripe Subscription price-swap or coupon exists yet — that is a separate,
later ticket, stated plainly in CLAUDE.md.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.events import log_event

logger = logging.getLogger(__name__)

_ELIGIBLE_OFFER_CODES = ("owner_growth", "full_county")
_MIN_QUALIFYING_SITS = 4
_GUARANTEE_WINDOW_DAYS = 60


def evaluate_sixty_day_guarantee(session: Session, *, client_id: str, as_of: datetime) -> bool:
	"""Returns True iff a NEW subscription_overrides row was written this
	call. Idempotent: an entitlement with guarantee_applied already TRUE, or
	one not on an eligible offer, or not yet at day 60, is a no-op — never an
	exception, since the sweep re-evaluates every eligible row on every tick
	until the flag flips."""
	entitlement = session.execute(
		text(
			"SELECT entitlement_id, offer_code, activated_at, guarantee_applied "
			"FROM client_entitlements "
			"WHERE client_id = :client_id AND status = 'ACTIVE' "
			"  AND offer_code = ANY(:eligible_offers) "
			"ORDER BY activated_at LIMIT 1"
		),
		{"client_id": client_id, "eligible_offers": list(_ELIGIBLE_OFFER_CODES)},
	).first()
	if entitlement is None:
		return False
	if entitlement.guarantee_applied:
		return False

	day_sixty = entitlement.activated_at + timedelta(days=_GUARANTEE_WINDOW_DAYS)
	if as_of < day_sixty:
		return False

	qualifying_sits = session.execute(
		text(
			"SELECT COUNT(*) AS n FROM appointments "
			"WHERE client_id = :client_id AND state = 'ATTENDED' AND is_billable "
			"  AND scheduled_for >= :window_start AND scheduled_for < :window_end"
		),
		{"client_id": client_id, "window_start": entitlement.activated_at, "window_end": day_sixty},
	).one()

	with session.begin_nested():
		session.execute(
			text(
				"UPDATE client_entitlements SET guarantee_applied = TRUE, guarantee_checked_at = :as_of, "
				"updated_at = :as_of WHERE entitlement_id = :entitlement_id AND guarantee_applied = FALSE"
			),
			{"as_of": as_of, "entitlement_id": entitlement.entitlement_id},
		)

		if qualifying_sits.n >= _MIN_QUALIFYING_SITS:
			logger.info(
				"billing.guarantee: client=%s has %d qualifying sits (>= %d) — no override",
				client_id, qualifying_sits.n, _MIN_QUALIFYING_SITS,
			)
			return False

		next_period = _next_billing_period(as_of)
		session.execute(
			text(
				"INSERT INTO subscription_overrides (client_id, billing_period, override_price_cents, reason) "
				"VALUES (:client_id, :billing_period, 0, 'SIXTY_DAY_GUARANTEE') "
				"ON CONFLICT (client_id, billing_period) DO NOTHING"
			),
			{"client_id": client_id, "billing_period": next_period},
		)

	log_event(
		client_id, "sixty_day_guarantee_applied", entity_type="client_entitlement", entity_id=str(entitlement.entitlement_id),
		payload={"client_id": client_id, "attended_sit_count": qualifying_sits.n, "billing_period": next_period.isoformat()},
		session=session,
	)
	logger.info(
		"billing.guarantee: client=%s has %d qualifying sits (< %d) — $0 override for %s",
		client_id, qualifying_sits.n, _MIN_QUALIFYING_SITS, next_period,
	)
	return True


def _next_billing_period(as_of: datetime) -> date:
	if as_of.month == 12:
		return date(as_of.year + 1, 1, 1)
	return date(as_of.year, as_of.month + 1, 1)
