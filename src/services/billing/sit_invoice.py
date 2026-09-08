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
     'NO_STRIPE_CUSTOMER' and skipped. The block is NOT permanent (PR #37
     second review finding #4): src/tasks/billing_sweep.py's claim query
     reclaims a row the moment clients.stripe_customer_id is populated, and
     this function clears billing_blocked_reason back to NULL on a
     successful invoice, rather than leaving stale reason text on a row
     that billed fine.
  3. Creates a Stripe invoice for the sit charge (skipping the line item
     entirely when the charge is the $0 free first sit — Stripe rejects a
     zero-amount invoice item), applies this client's PENDING billing
     credits up to the current billing period onto the SAME invoice via
     apply_pending_credits_to_invoice(), then finalizes it.
  4. No payment method capture exists for a client's own billing account
     either — create_invoice() falls back to Stripe's send_invoice
     collection (Stripe emails/hosts the invoice) rather than attempting an
     automatic charge with nothing to charge.

Durable reconciliation (PR #37 second review finding #8):
`appointments.stripe_invoice_id` is persisted in its OWN nested savepoint
immediately after `create_invoice` returns, before add_invoice_item/
apply_credits/finalize run. Critically, `src/tasks/billing_sweep.py`'s
`run_sit_invoice_sweep` does NOT wrap the call to this function in its own
enclosing savepoint (unlike its other two sweeps) — a savepoint rolled back
on exception discards EVERY write since it began, so an ENCLOSING savepoint
around this whole function would silently undo this savepoint's already-
released write the moment a LATER step (add_invoice_item, finalize) raised,
defeating the entire point. With no such enclosing savepoint, a plain
application/transport exception (the realistic Stripe-call failure mode)
leaves the already-executed writes in this transaction healthy, and the
sweep's own try/except around the whole call is what keeps one bad row from
stopping the batch.

A second, easy-to-miss half of this same finding: `billed_offer_code` is set
by `resolve_sit_charge()` — a decision purely about WHICH offer/price
applies, made and durably committed independently of whether any Stripe call
ever succeeds. Before this fix, `billed_offer_code IS NOT NULL` alone meant
"never touch this appointment again" both for the claim query AND for this
function's own early-return — so a crash between `resolve_sit_charge()` and
`finalize_invoice()` left the sit billed in our own ledger with an
incomplete (or nonexistent) Stripe invoice, and NO further sweep tick would
ever retry it. `appointments.sit_invoice_finalized_at` is the fix: set ONLY
once `finalize_invoice()` actually returns successfully, checked (alongside
`billed_offer_code`) by both the claim query and this function's own
short-circuit — a billed-but-not-yet-finalized row stays reclaimable.

Together these two mechanisms are what makes "a retry must not create
another invoice" true: the stored `stripe_invoice_id` prevents a duplicate
`create_invoice` call, and the pinned per-object idempotency keys below
prevent Stripe itself from double-creating even if this repo's own state
were somehow inconsistent, for as long as Stripe's own ~24h idempotency-key
retention holds. A genuine process crash (not a raised exception, e.g.
SIGKILL) between `create_invoice` returning and this savepoint's own commit
to disk is the one scenario neither mechanism covers — only a truly separate
committed transaction boundary protects against that, which this savepoint
(nested within the sweep's still-open outer transaction) is not.
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
            "SELECT a.is_billable, a.billed_offer_code, a.billed_amount_cents, a.stripe_invoice_id, "
            "       a.sit_invoice_finalized_at, c.stripe_customer_id "
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

    if row.billed_offer_code is not None and row.sit_invoice_finalized_at is not None:
        # Already billed AND the Stripe invoice was actually finalized — a
        # second invoice must never be created for it. Checking
        # billed_offer_code ALONE here was the deeper half of PR #37 second
        # review finding #8: that column is set by resolve_sit_charge(),
        # independent of and well before any Stripe call, so it is not proof
        # the Stripe side is done — see the module docstring. A row that is
        # billed but NOT yet finalized falls through below to resume/finish
        # it, exactly like a brand-new row.
        return SitInvoiceOutcome(status="ALREADY_INVOICED")

    charge = resolve_sit_charge(session, client_id=client_id, appointment_id=appointment_id, as_of=as_of)

    gw = gateway or LiveStripeGateway()
    idempotency_prefix = f"sit-invoice|{client_id}|{appointment_id}"

    if row.stripe_invoice_id:
        # Resuming a prior attempt that created the invoice but raised before
        # billed_offer_code was set (PR #37 second review finding #8) —
        # reuse the stored invoice rather than calling create_invoice again.
        stripe_invoice_id = row.stripe_invoice_id
        logger.info(
            "billing.sit_invoice: client=%s appointment=%s resuming existing invoice=%s",
            client_id, appointment_id, stripe_invoice_id,
        )
    else:
        invoice = gw.create_invoice(
            stripe_customer_id=row.stripe_customer_id,
            default_payment_method_id=None,
            metadata={"client_id": client_id, "appointment_id": str(appointment_id), "purpose": "sit_charge"},
            idempotency_key=f"{idempotency_prefix}|invoice",
        )
        stripe_invoice_id = invoice.stripe_invoice_id
        # Persisted in its own savepoint so it survives a LATER exception in
        # this same call (add_invoice_item/apply_credits/finalize below) —
        # see the module docstring for exactly what this does and doesn't
        # protect against.
        with session.begin_nested():
            session.execute(
                text(
                    "UPDATE appointments SET stripe_invoice_id = :invoice_id "
                    "WHERE client_id = :client_id AND appointment_id = :appointment_id"
                ),
                {"invoice_id": stripe_invoice_id, "client_id": client_id, "appointment_id": appointment_id},
            )

    if charge.amount_cents > 0:
        gw.add_invoice_item(
            stripe_invoice_id=stripe_invoice_id,
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
        stripe_invoice_id=stripe_invoice_id,
        stripe_customer_id=row.stripe_customer_id,
        as_of=as_of,
        gateway=gw,
    )

    gw.finalize_invoice(stripe_invoice_id=stripe_invoice_id, idempotency_key=f"{idempotency_prefix}|finalize")

    # Only NOW is the Stripe side actually complete — stamp the finalization
    # marker (PR #37 second review finding #8) so the claim query's
    # `billed_offer_code IS NOT NULL AND sit_invoice_finalized_at IS NULL`
    # reclaim branch stops matching this row, and clear a stale block now
    # that this appointment has actually billed (finding #4) — a lingering
    # billing_blocked_reason would misreport why on any dashboard/support
    # query that reads the column directly.
    session.execute(
        text(
            "UPDATE appointments SET sit_invoice_finalized_at = :as_of, billing_blocked_reason = NULL "
            "WHERE client_id = :client_id AND appointment_id = :appointment_id"
        ),
        {"as_of": as_of, "client_id": client_id, "appointment_id": appointment_id},
    )

    logger.info(
        "billing.sit_invoice: client=%s appointment=%s offer_code=%s amount_cents=%d invoice=%s",
        client_id, appointment_id, charge.offer_code, charge.amount_cents, stripe_invoice_id,
    )
    return SitInvoiceOutcome(status="INVOICED", stripe_invoice_id=stripe_invoice_id)
