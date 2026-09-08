"""Applies PENDING billing_credits to a real Stripe invoice as a
negative-amount invoice item (Subtask 1.2.3, rules 1 and 4).

Reuses the existing six-method StripeGateway ABC
(src/services/settlement/gateway.py) — no new Stripe API surface, no second
gateway. add_invoice_item's amount_cents already passes straight through to
Stripe's invoice_items.create, and Stripe invoice items accept a negative
amount as a credit line, so no gateway change was needed — only this new
caller.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.settlement.gateway import LiveStripeGateway, StripeGateway

logger = logging.getLogger(__name__)


def apply_pending_credits_to_invoice(
	session: Session,
	*,
	client_id: str,
	stripe_invoice_id: str,
	stripe_customer_id: str,
	as_of: datetime,
	gateway: Optional[StripeGateway] = None,
) -> int:
	"""Adds one negative invoice item per PENDING billing_credits row for
	client_id onto the already-created (draft) invoice stripe_invoice_id,
	then marks each row APPLIED with the resulting stripe_invoice_item_id.
	Idempotency key is pinned to the credit_id alone — a retried sweep
	re-derives the same key rather than creating a second Stripe object,
	same convention as settlement/charge.py.

	Returns the number of credits applied. A credit already APPLIED/VOIDED is
	never re-applied (the WHERE clause below only ever selects PENDING rows)."""
	gw = gateway or LiveStripeGateway()
	credits = session.execute(
		text(
			"SELECT credit_id, credit_type, amount_cents FROM billing_credits "
			"WHERE client_id = :client_id AND status = 'PENDING' "
			"ORDER BY credit_id FOR UPDATE SKIP LOCKED"
		),
		{"client_id": client_id},
	).all()

	applied = 0
	for credit in credits:
		idempotency_key = f"billing-credit|{credit.credit_id}"
		gw.add_invoice_item(
			stripe_invoice_id=stripe_invoice_id,
			stripe_customer_id=stripe_customer_id,
			amount_cents=-credit.amount_cents,
			description=f"Blackink {credit.credit_type.replace('_', ' ').title()} — credit {credit.credit_id}",
			idempotency_key=idempotency_key,
		)
		# StripeGateway.add_invoice_item() returns None (the ABC exposes no
		# item-id lookup) — stripe_invoice_item_id stores the parent invoice id
		# instead, which is still enough to find the credit line on the Stripe
		# dashboard/invoice.
		session.execute(
			text(
				"UPDATE billing_credits SET status = 'APPLIED', "
				"stripe_invoice_item_id = :stripe_invoice_id, applied_at = :as_of "
				"WHERE credit_id = :credit_id"
			),
			{"stripe_invoice_id": stripe_invoice_id, "as_of": as_of, "credit_id": credit.credit_id},
		)
		applied += 1

	logger.info("billing.invoice_apply: applied %d credit(s) to invoice=%s (client=%s)", applied, stripe_invoice_id, client_id)
	return applied
