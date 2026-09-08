"""Wires resolve_sit_charge() and apply_pending_credits_to_invoice() into a
real production path (PR #37 review — blocking findings 1 and 2: both
functions existed but nothing ever called them, so rules 1/2/4/5 never
reached an actual Stripe invoice, only a database row).

charge_sit_for_appointment() is the one function that turns an ATTENDED,
billable appointment into a real Stripe invoice:
  1. resolve_sit_charge() — idempotent; a re-claimed/retried appointment
     returns its already-recorded charge rather than re-deciding it.
  2. Requires clients.stripe_customer_id. Nothing in this repo provisions
     that column yet (no client-portal onboarding exists) — a client
     without one is marked appointments.billing_blocked_reason =
     'NO_STRIPE_CUSTOMER' and skipped, never crashed on or guessed.
  3. Creates a Stripe invoice for the sit charge (skipping the line item
     entirely when the charge is the $0 free first sit — Stripe rejects a
     zero-amount invoice item), applies this client's PENDING billing
     credits for the CURRENT billing period onto the SAME invoice via
     apply_pending_credits_to_invoice(), then finalizes it.
  4. No payment method capture exists for a client's own billing account
     either — create_invoice() falls back to Stripe's send_invoice
     collection (Stripe emails/hosts the invoice) rather than attempting an
     automatic charge with nothing to charge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.billing.invoice_apply import apply_pending_credits_to_invoice
from src.services.billing.sit_billing import resolve_sit_charge
from src.services.settlement.gateway import LiveStripeGateway, StripeGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SitInvoiceOutcome:
    status: str  # 'INVOICED' | 'ALREADY_INVOICED' | 'BLOCKED'
    reason: Optional[str] = None
    stripe_invoice_id: Optional[str] = None


def charge_sit_for_appointment(
    session: Session,
    *,
    client_id: str,
    appointment_id: str,
    as_of: datetime,
    gateway: Optional[StripeGateway] = None,
) -> SitInvoiceOutcome:
    row = session.execute(
        text(
            "SELECT a.is_billable, a.billed_offer_code, a.billed_amount_cents, "
            "       c.stripe_customer_id "
            "FROM appointments a JOIN clients c ON c.client_id = a.client_id "
            "WHERE a.client_id = :client_id AND a.appointment_id = :appointment_id"
        ),
        {"client_id": client_id, "appointment_id": appointment_id},
    ).first()
    if row is None:
        raise ValueError(f"no such appointment {appointment_id!r} for client {client_id!r}")

    if not row.stripe_customer_id:
        session.execute(
            text(
                "UPDATE appointments SET billing_blocked_reason = 'NO_STRIPE_CUSTOMER' "
                "WHERE client_id = :client_id AND appointment_id = :appointment_id"
            ),
            {"client_id": client_id, "appointment_id": appointment_id},
        )
        logger.warning(
            "billing.sit_invoice: client=%s has no stripe_customer_id — appointment=%s BLOCKED",
            client_id, appointment_id,
        )
        return SitInvoiceOutcome(status="BLOCKED", reason="NO_STRIPE_CUSTOMER")

    if row.billed_offer_code is not None:
        # Already billed by an earlier call — resolve_sit_charge() would
        # return the SAME recorded charge (it's idempotent), but a second
        # Stripe invoice must never be created for it. This function is only
        # ever meant to be called once per appointment by the sweep's own
        # claim query (billed_offer_code IS NULL); a call outside that path
        # (a manual retry, a re-processed message) is a pure no-op here.
        return SitInvoiceOutcome(status="ALREADY_INVOICED")

    charge = resolve_sit_charge(session, client_id=client_id, appointment_id=appointment_id, as_of=as_of)

    gw = gateway or LiveStripeGateway()
    idempotency_prefix = f"sit-invoice|{client_id}|{appointment_id}"

    invoice = gw.create_invoice(
        stripe_customer_id=row.stripe_customer_id,
        default_payment_method_id=None,
        metadata={"client_id": client_id, "appointment_id": str(appointment_id), "purpose": "sit_charge"},
        idempotency_key=f"{idempotency_prefix}|invoice",
    )

    if charge.amount_cents > 0:
        gw.add_invoice_item(
            stripe_invoice_id=invoice.stripe_invoice_id,
            stripe_customer_id=row.stripe_customer_id,
            amount_cents=charge.amount_cents,
            description=f"Blackink appointment sit — {charge.offer_code} ({appointment_id})",
            idempotency_key=f"{idempotency_prefix}|item",
        )

    billing_period = date(as_of.year, as_of.month, 1)
    apply_pending_credits_to_invoice(
        session,
        client_id=client_id,
        billing_period=billing_period,
        stripe_invoice_id=invoice.stripe_invoice_id,
        stripe_customer_id=row.stripe_customer_id,
        as_of=as_of,
        gateway=gw,
    )

    gw.finalize_invoice(stripe_invoice_id=invoice.stripe_invoice_id, idempotency_key=f"{idempotency_prefix}|finalize")

    logger.info(
        "billing.sit_invoice: client=%s appointment=%s offer_code=%s amount_cents=%d invoice=%s",
        client_id, appointment_id, charge.offer_code, charge.amount_cents, invoice.stripe_invoice_id,
    )
    return SitInvoiceOutcome(status="INVOICED", stripe_invoice_id=invoice.stripe_invoice_id)
