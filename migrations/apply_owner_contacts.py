"""
Provision owner_contacts (Dev 3, Subtask 3.2.1 — Inbound Booking Engine).

Residential property-owner identity — distinct from `contacts`, which is
capped at exactly two PM-firm staff roles per prospected company
(OWNER_BROKER_MD / OFFICE_MANAGER_OPS, see src/core/models.py's Contact
docstring) and cannot represent a property owner at all, and distinct
from `owner_entities`, which is unpopulated LLC/beneficial-owner dedup
scaffolding with no name/email/phone columns (see that model's own
docstring). Neither Blackink_Source_of_Truth.md nor the blueprint defines
an owner-identity schema for the booking flow — this table is this
session's own engineering decision, not a client requirement, made after
confirming both existing candidates were the wrong fit.

owner_entity_id is a nullable forward-link into the existing
owner_entity_links-style dedup scaffold, left unpopulated by this plan
(same as owner_entities itself) — the door is open for a future dedup
job, not built here.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_owner_contacts.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS owner_contacts (
		owner_contact_id  BIGSERIAL    PRIMARY KEY,
		client_id         VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		full_name         VARCHAR(200) NOT NULL,
		email             VARCHAR(255),
		phone             VARCHAR(20),
		source            VARCHAR(30)  NOT NULL DEFAULT 'CALENDAR_BOOKING',
		owner_entity_id   BIGINT       REFERENCES owner_entities(id),
		created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_owner_contacts_client ON owner_contacts (client_id)",
	# Matching lookups are always (client_id, email) — see booking_ingest.py.
	"CREATE INDEX IF NOT EXISTS ix_owner_contacts_client_email ON owner_contacts (client_id, email)",
	# DELETE granted alongside SELECT/INSERT/UPDATE — same convention as
	# contacts/companies, needed for test-fixture teardown, not just
	# app-role runtime writes.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_contacts TO blackink_app",
	"GRANT USAGE ON SEQUENCE owner_contacts_owner_contact_id_seq TO blackink_app",
	# calendar_sync_worker.py / booking_confirmation_sender.py run as blackink_system
	# and need to read/write owner_contacts too (e.g. link_booking_to_owner()).
	"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_contacts TO blackink_system",
	"GRANT USAGE ON SEQUENCE owner_contacts_owner_contact_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_owner_contacts: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
