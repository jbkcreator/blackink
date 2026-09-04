"""Schema smoke test — Base.metadata.create_all() against a clean DB.

Mirrors Forced Action's tests/test_2b_schema.py. Dev/CI convenience only;
production schema changes go through migrations/apply_<name>.py, never
create_all() (see src/core/database.py's create_all_tables() docstring).

Requires DATABASE_URL (or DATABASE_URL_APP) to point at a disposable test
database — this test creates real tables.
"""

from sqlalchemy import inspect

from src.core.database import Database
from src.core.models import Base


def test_create_all_tables_succeeds():
	db = Database()
	db.create_all_tables()

	inspector = inspect(db.engine)
	table_names = set(inspector.get_table_names())

	expected = {
		"counties",
		"clients",
		"county_allocations",
		"client_pm_books",
		"companies",
		"contacts",
		"pm_profiles",
	}
	missing = expected - table_names
	assert not missing, f"Expected tables missing after create_all(): {missing}"


def test_contacts_enforces_two_roles_per_company():
	"""UNIQUE(company_id, contact_role_type) is declared on the model —
	assert it made it into the actual table constraints, not just the ORM."""
	db = Database()
	inspector = inspect(db.engine)
	constraints = inspector.get_unique_constraints("contacts")
	names = {c["name"] for c in constraints}
	assert "uq_contacts_company_role" in names


def test_county_allocations_enforces_one_active_per_county():
	"""Partial unique index enforcing exactly one non-superseded allocation
	per county — the actual exclusivity mechanism, not just convention."""
	db = Database()
	inspector = inspect(db.engine)
	indexes = inspector.get_indexes("county_allocations")
	names = {i["name"] for i in indexes}
	assert "uq_county_allocations_active_county" in names
