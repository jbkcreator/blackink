"""Cold-sequence enrollment sweep — enrolls eligible promoted prospects.

This is the production trigger the outbound sequencer was missing: it finds
contacts that are ready for cold outreach and hands each to
src/services/sequence_enrollment.py::enroll_contact(), which creates the
sequence_run and enqueues the five touch work orders. Everything downstream
(the approval-card sweep, work-order execution, dispatch, send) was already
scheduled; nothing fed the front of it until now.

Runs per tenant, as the RLS-subject app role: `run_sweep(client_id)` opens a
get_db_context(client_id=...) so the candidate query sees only that client's
allocated contacts (contacts are RLS-scoped via companies.owning_client_id).
The all-clients cron entry (scripts/cron_enrollment_sweep_all_clients.py)
loops `clients WHERE is_active` and calls this once per client — the same
shape as scripts/cron_work_orders_sweep_all_clients.py.

Eligibility is deliberately strict and defers the hard filtering to layers
that already exist:
  - allocated to this client (companies.owning_client_id) — RLS + the query,
  - email verified (contacts.email_status = 'VERIFIED') — filtered here so a
    vendor-unverified contact is skipped cheaply rather than enrolled and
    then blocked at every send by the compliance gate,
  - not globally opted out, has an email,
  - no ACTIVE or still-cooling sequence_run (may_enroll re-checks this
    cross-client on a system connection, so the query predicate is only an
    optimization, never the sole guard).
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text

from src.core.database import get_db_context
from src.services.sequence_enrollment import enroll_contact

logger = logging.getLogger(__name__)

# Cap contacts enrolled per client per tick. Enrollment only creates runs +
# QUEUED work orders; per-mailbox/per-client send caps (mailbox_dispatcher)
# still gate actual delivery downstream. The cap keeps a first run over a
# freshly-loaded prospect pool from creating an unbounded burst of work
# orders in one transaction.
ENROLLMENT_BATCH_LIMIT = 200

_ELIGIBLE_CONTACTS_SQL = """
    SELECT c.contact_id, c.email, c.first_name, co.company_name
    FROM contacts c
    JOIN companies co ON co.company_id = c.company_id
    WHERE co.owning_client_id = :client_id
      AND c.email_status = 'VERIFIED'
      AND c.is_opted_out = FALSE
      AND c.email IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sequence_runs sr
          WHERE sr.contact_id = c.contact_id
            AND (sr.status = 'ACTIVE'
                 OR (sr.cooling_until IS NOT NULL AND sr.cooling_until > NOW()))
      )
    ORDER BY c.contact_id
    LIMIT :limit
"""


def run_sweep(client_id: str, *, limit: int = ENROLLMENT_BATCH_LIMIT) -> int:
    """Enroll up to `limit` eligible contacts for one client. Returns the
    number newly enrolled. Never raises for an individual contact — a single
    contact's enrollment failure is logged and the sweep moves on."""
    enrolled = 0
    with get_db_context(client_id=client_id) as session:
        rows = session.execute(
            text(_ELIGIBLE_CONTACTS_SQL),
            {"client_id": client_id, "limit": limit},
        ).fetchall()

        for row in rows:
            try:
                run_id = enroll_contact(
                    session,
                    client_id,
                    row.contact_id,
                    row.email,
                    first_name=row.first_name,
                    company_name=row.company_name,
                )
            except Exception:
                logger.exception(
                    "enrollment_sweep: enroll failed client_id=%s contact_id=%s",
                    client_id, row.contact_id,
                )
                continue
            if run_id:
                enrolled += 1

    logger.info(
        "enrollment_sweep: client_id=%s enrolled=%d (candidates=%d)",
        client_id, enrolled, len(rows),
    )
    return enrolled


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Enroll eligible contacts into the cold sequence for one client.")
    parser.add_argument(
        "--client-id",
        required=True,
        help="Tenant to enroll for. Required — an unscoped run would see zero "
             "rows under RLS rather than fail loudly (same posture as the "
             "work-orders CLI). Use scripts.cron_enrollment_sweep_all_clients "
             "to run every active client.",
    )
    parser.add_argument("--limit", type=int, default=ENROLLMENT_BATCH_LIMIT)
    args = parser.parse_args()
    run_sweep(args.client_id, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
