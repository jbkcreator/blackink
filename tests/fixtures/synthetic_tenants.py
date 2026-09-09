"""Synthetic canary tenants for the adversarial leakage test.

Seeds two isolated clients (_leakcanary_a, _leakcanary_b) plus a company +
contact each, tagged with a distinguishing domain suffix, and tears them
down after the test. Setup/teardown use the BYPASSRLS system session (needs
cross-tenant visibility to create both canaries) — the actual leakage
probes in test_tenant_isolation.py use the RLS-subject app session, which
is the point of the test.

Never run against a database holding real prospect/client data — this is
CI/isolated-test-DB only.
"""

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.core.models import Client, Company, Contact
from src.loaders.base import BaseIngestLoader

CANARY_A = "_leakcanary_a"
CANARY_B = "_leakcanary_b"
CANARY_COUNTY = "hillsborough_fl"
CANARY_DOMAIN_SUFFIX = ".leakcanary.test"


@pytest.fixture
def canary_tenants():
	created = {}
	with get_system_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			session.merge(Client(client_id=cid, display_name=cid, is_active=True))
		session.flush()

		for cid in (CANARY_A, CANARY_B):
			domain = f"{cid}{CANARY_DOMAIN_SUFFIX}"
			company = Company(
				company_id=BaseIngestLoader.compute_company_id(domain),
				company_name=f"Canary Co {cid}",
				domain=domain,
				county_slug=CANARY_COUNTY,
				owning_client_id=cid,
			)
			session.add(company)
			session.flush()
			contact = Contact(
				company_id=company.company_id,
				contact_role_type="OWNER_BROKER_MD",
				email=f"owner@{domain}",
			)
			session.add(contact)
			session.flush()
			created[cid] = {"company_id": company.company_id, "contact_id": contact.contact_id, "domain": domain}

	yield created

	# events is append-only — DELETE is REVOKEd from blackink_app AND
	# blackink_system (same posture as appointments/settlement_transactions),
	# so this one statement needs the table-owner role. Subtask 1.2.2 tests
	# are the first callers through this fixture that log an event
	# (door_signed) against a canary client_id — without this, events'
	# client_id FK blocks the DELETE below and leaves the canary
	# company/client stuck for every subsequent test run.
	with get_owner_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": cid})

	with get_system_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			company_id = created[cid]["company_id"]
			session.execute(text("DELETE FROM contacts WHERE company_id = :cid"), {"cid": company_id})
			session.execute(text("DELETE FROM companies WHERE company_id = :cid"), {"cid": company_id})
			session.execute(text("DELETE FROM clients WHERE client_id = :cid"), {"cid": cid})
