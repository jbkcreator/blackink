"""Permanent Demo Friday Sandbox — realistic, non-destructive seed data for
live sales demos. Blueprint §3.1.6 / split-doc Subtask 4.1.3.

Runs as blackink_system (BYPASSRLS): this provisions a client's own data
across a fixed client_id, which is exactly the internal-batch-job shape
get_system_db_context() exists for (same class of job as promotion_sweep.py).

Idempotent via ON CONFLICT DO UPDATE / DO NOTHING throughout — re-running
this refreshes/tops up the sandbox, it never wipes it (blueprint: "safe to
update but not wipe").

    PYTHONPATH=. python -m src.tasks.seed_demo_sandbox
"""

from __future__ import annotations

import hashlib
import logging
import random

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.events import log_event

logger = logging.getLogger(__name__)

SANDBOX_CLIENT_ID = "DEMO_FRIDAY_SANDBOX"

# v2 spec correction: sandbox scope narrowed from all 4 target counties to
# Hillsborough and Pinellas only (Tasks/Updated_client spec/Week1_Tasks_Dev_
# Split_v2.md, Subtask 4.1.1). _METRO_BY_COUNTY below is unused dead code
# independent of this change (a pre-existing, separately-flagged minor).
_COUNTIES = ["hillsborough_fl", "pinellas_fl"]
_METRO_BY_COUNTY = {
    "hillsborough_fl": "Tampa",
    "pinellas_fl": "St Petersburg",
    "orange_fl": "Orlando",
    "miami_dade_fl": "Miami-Dade",
}
_NAME_STEMS = [
    "Sunbelt", "Gulfshore", "Palmetto", "Bayview", "Coastal Oak", "Magnolia",
    "Harborline", "Citrus Grove", "Lakeside", "Bayshore", "Emerald Coast",
    "Sawgrass", "Ibis", "Egret Point", "Pelican Bay", "Seminole Trail",
    "Cypress Point", "Anchor Bay", "Silver Palm", "Vista Bay", "Sable Ridge",
    "Windward", "Blue Heron", "Tidewater", "Osprey Landing", "Marlin Cove",
    "Live Oak", "Turtle Cove", "Redbird", "Golden Isles", "Shoreline",
    "Halcyon", "Camino Real", "Regatta", "Coquina", "Beacon Hill",
    "Southport", "Riverwalk", "Bellwood", "Highpoint",
]
_NAME_SUFFIXES = ["Property Management", "Realty & Management", "PM Group", "Residential Management", "Property Partners"]

# Realistic-sounding contact names — the split doc's DoD includes a
# "realism check: no placeholder names", which "Owner Sunbelt" would fail
# as surely as "Test Company 1" does.
_FIRST_NAMES = ["Marcus", "Elena", "Trevor", "Priya", "Dana", "Luis", "Kendra", "Omar", "Bethany", "Grant"]
_LAST_NAMES = ["Delgado", "Whitfield", "Nakamura", "Okonkwo", "Brennan", "Vasquez", "Lindqvist", "Boyd", "Ferrara", "Achebe"]

# Fixed seed, applied PER CALL via a local Random instance — NOT a module
# level random.seed(). A module-level seed is consumed once at import, so a
# second call in the same process returns different door counts, which
# would make every re-run of this seeder rewrite every row with new numbers
# and quietly break the idempotency this script promises.
_SEED = 4211


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("&", "and")


def build_mock_companies() -> list[dict]:
    rng = random.Random(_SEED)
    companies = []
    for i, stem in enumerate(_NAME_STEMS):
        county = _COUNTIES[i % len(_COUNTIES)]
        name = f"{stem} {_NAME_SUFFIXES[i % len(_NAME_SUFFIXES)]}"
        domain = f"{_slugify(stem)}pm.com"
        company_id = hashlib.sha256(domain.encode("utf-8")).hexdigest()
        companies.append(
            {
                "company_id": company_id,
                "company_name": name,
                "website": f"https://www.{domain}",
                "domain": domain,
                "county_slug": county,
                "door_count_est": rng.randint(15, 400),
                "current_pm_software": rng.choice(["AppFolio", "Buildium", "Propertyware", "Rent Manager", "Unknown"]),
            }
        )
    return companies


def build_mock_contacts_for_company(company: dict) -> list[dict]:
    # Names derived deterministically from the company_id hash so a re-run
    # produces identical contacts (same reason as the seeded rng above).
    seed_int = int(company["company_id"][:8], 16)
    owner_first = _FIRST_NAMES[seed_int % len(_FIRST_NAMES)]
    owner_last = _LAST_NAMES[seed_int % len(_LAST_NAMES)]
    ops_first = _FIRST_NAMES[(seed_int // 7) % len(_FIRST_NAMES)]
    ops_last = _LAST_NAMES[(seed_int // 11) % len(_LAST_NAMES)]
    # Reserved-range fake numbers (+1813555 prefix, same as the old hardcoded
    # pair) with the last 4 digits derived from the hash so different
    # companies don't all show the same two phone numbers on a live demo.
    owner_phone = f"+1813555{seed_int % 10000:04d}"
    ops_phone = f"+1813555{(seed_int // 13) % 10000:04d}"
    return [
        {
            "company_id": company["company_id"],
            "contact_role_type": "OWNER_BROKER_MD",
            "first_name": owner_first,
            "last_name": owner_last,
            "title": "Managing Broker",
            "email": f"{owner_first.lower()}.{owner_last.lower()}@{company['domain']}",
            "phone": owner_phone,
        },
        {
            "company_id": company["company_id"],
            "contact_role_type": "OFFICE_MANAGER_OPS",
            "first_name": ops_first,
            "last_name": ops_last,
            "title": "Operations Manager",
            "email": f"ops.{ops_last.lower()}@{company['domain']}",
            "phone": ops_phone,
        },
    ]


_UPSERT_CLIENT = """
    INSERT INTO clients (client_id, display_name, is_active, plan_tier)
    VALUES (:client_id, 'Demo Friday Sandbox', TRUE, 'internal')
    ON CONFLICT (client_id) DO UPDATE SET display_name = EXCLUDED.display_name
"""

_UPSERT_COMPANY = """
    INSERT INTO companies (company_id, company_name, website, domain, county_slug, door_count_est, current_pm_software, status, owning_client_id)
    VALUES (:company_id, :company_name, :website, :domain, :county_slug, :door_count_est, :current_pm_software, 'CLIENT', :owning_client_id)
    ON CONFLICT (company_id) DO UPDATE SET
        door_count_est = EXCLUDED.door_count_est,
        current_pm_software = EXCLUDED.current_pm_software,
        owning_client_id = EXCLUDED.owning_client_id
"""

_UPSERT_CONTACT = """
    INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, title, email, phone, compliance_eligibility)
    VALUES (:company_id, :contact_role_type, :first_name, :last_name, :title, :email, :phone, 'TRANSACTIONAL_SMS_ONLY')
    ON CONFLICT (company_id, contact_role_type) DO UPDATE SET
        email = EXCLUDED.email, phone = EXCLUDED.phone
"""

_SELECT_EXISTING_OWNER = """
    SELECT owning_client_id FROM companies WHERE company_id = :company_id
"""


def _existing_owner(session, company_id: str) -> "str | None":
    row = session.execute(text(_SELECT_EXISTING_OWNER), {"company_id": company_id}).mappings().first()
    return row["owning_client_id"] if row else None


def main() -> int:
    """company_id is sha256(domain) — globally unique and deterministic. If
    one of this seeder's 40 domains happens to collide with a REAL customer's
    company (already owned by a different client_id), the upsert below must
    NOT touch that row: touching it would silently reassign a real
    customer's company (and let a real contact's email/phone be overwritten
    by fake sandbox data) to the demo tenant — a cross-tenant data-integrity
    and disclosure bug, not a cosmetic one. Every collision is skipped
    entirely (company AND its contacts, never a partial write) and logged."""
    companies = build_mock_companies()
    seeded: list[dict] = []
    skipped_domains: list[str] = []

    with get_system_db_context() as session:
        session.execute(text(_UPSERT_CLIENT), {"client_id": SANDBOX_CLIENT_ID})
        for company in companies:
            owner = _existing_owner(session, company["company_id"])
            if owner is not None and owner != SANDBOX_CLIENT_ID:
                skipped_domains.append(company["domain"])
                continue
            session.execute(text(_UPSERT_COMPANY), dict(company, owning_client_id=SANDBOX_CLIENT_ID))
            for contact in build_mock_contacts_for_company(company):
                session.execute(text(_UPSERT_CONTACT), contact)
            seeded.append(company)

    if skipped_domains:
        logger.warning(
            "seed_demo_sandbox: skipped %d domain collision(s) already owned by another client — "
            "left untouched: %s",
            len(skipped_domains),
            skipped_domains,
        )

    if seeded:
        for i in range(20):
            company = seeded[i % len(seeded)]
            # Recipient matches the contact actually seeded for this company —
            # a dashboard showing touches to owner@... while the contacts table
            # holds marcus.delgado@... is exactly the kind of detail that gets
            # noticed on a shared screen during a live demo.
            owner_contact = build_mock_contacts_for_company(company)[0]
            log_event(
                SANDBOX_CLIENT_ID,
                "outbound_touch_dispatched",
                entity_type="company",
                entity_id=company["company_id"],
                payload={
                    "touch_step": (i % 5) + 1,
                    "channel": "email",
                    "recipient_email": owner_contact["email"],
                    "template_version": "v1.4_speed_audit_video",
                    "sending_domain": "growth-blackink.com",
                    "mailbox_id": f"mbx_{(i % 6) + 1:02d}",
                },
            )
        for i in range(min(5, len(seeded))):
            company = seeded[i]
            log_event(
                SANDBOX_CLIENT_ID,
                "meeting_booked",
                entity_type="company",
                entity_id=company["company_id"],
                payload={"door_count_est": company["door_count_est"], "source": "sandbox_seed"},
            )

    events_logged = (20 + min(5, len(seeded))) if seeded else 0
    print(
        f"seed_demo_sandbox: done — {len(seeded)} companies, {len(seeded) * 2} contacts, "
        f"{events_logged} mock events"
        + (f", {len(skipped_domains)} collision(s) skipped" if skipped_domains else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
