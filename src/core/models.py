"""SQLAlchemy declarative models — schema source of truth.

Convention mirrors Forced Action's src/core/models.py: VARCHAR + CheckConstraint
instead of native Postgres ENUM (native enums fight the idempotent
`ADD COLUMN IF NOT EXISTS` migration style — altering an enum type is not
trivially idempotent). Verified via Base.metadata.create_all() in
tests/test_schema.py, never used for runtime queries (those go through
sqlalchemy.text() with named binds per project convention).

Week 1 scope: County, Client, CountyAllocation, ClientPmBook, Company,
Contact, PmProfile. RawProspectCompany/RawProspectContact, Event,
ComplianceGateCheck, OwnerEntity/OwnerEntityLink were already brought
forward from the original Week 2 plan during Week 0 build-out — see
C:\\Users\\HEU-Vishnu\\.claude\\plans\\dev-1-data-synthetic-fox.md.
"""

from datetime import datetime, date
from typing import Optional

from sqlalchemy.dialects.postgresql import ARRAY

from sqlalchemy import (
	CheckConstraint,
	Date,
	DateTime,
	ForeignKey,
	Index,
	Integer,
	BigInteger,
	Numeric,
	SmallInteger,
	String,
	Text,
	Boolean,
	UniqueConstraint,
	func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
	pass


# ============================================================================
# REFERENCE / CONFIG TABLES
# ============================================================================

class County(Base):
	"""Canonical county reference list — avoids free-text county typos
	across companies / county_allocations / client_pm_books. The unit of
	territory, allocation, and contract exclusivity is the county, never a
	metro area (client's explicit correction — see plan doc)."""

	__tablename__ = "counties"

	county_slug: Mapped[str] = mapped_column(String(60), primary_key=True)
	county_name: Mapped[str] = mapped_column(String(100), nullable=False)
	state: Mapped[str] = mapped_column(String(2), nullable=False)


class UsAreaCodeTimezone(Base):
	"""US NANP phone area code -> IANA timezone, used to compute quiet hours
	(9pm-8am recipient local time, Week 1 Subtask 1.2.2). Representative
	seed set, not the full ~300-code NANP list — an area code missing here
	fails closed (SMS withheld) rather than assumed clear."""

	__tablename__ = "us_area_code_timezones"

	area_code: Mapped[str] = mapped_column(String(3), primary_key=True)
	iana_timezone: Mapped[str] = mapped_column(String(50), nullable=False)


class Client(Base):
	"""Tenant registry. Deny-by-default config resolution (get_client_config)
	reads is_active/suspended_at here — see src/services/client_config.py."""

	__tablename__ = "clients"

	client_id: Mapped[str] = mapped_column(String(40), primary_key=True)
	display_name: Mapped[str] = mapped_column(String(200), nullable=False)
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
	plan_tier: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
	contract_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
	contract_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
	daily_send_ceiling: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	# Set by an admin suspend action. get_client_config() must invalidate its
	# cache synchronously in the same transaction as this being set — a
	# suspended client's send ability must stop in seconds, not up to the
	# cache TTL later.
	suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)


class CountyAllocation(Base):
	"""Prospect-pool outreach-rights assignment: which client's campaign
	currently owns a county's PROSPECT pool. Distinct from ClientPmBook /
	the permanent non-poach lock below — this one carries a 30-day
	reassessment window and governs companies NOT yet on anyone's managed
	book. Adapted from the blueprint's Metro Allocation Algorithm
	(county-scoped per the client's correction), NOT the removed seat SKUs.

	Exactly one non-superseded row per county at a time — enforced by the
	partial unique index below, not just application convention.
	"""

	__tablename__ = "county_allocations"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	county_slug: Mapped[str] = mapped_column(
		String(60), ForeignKey("counties.county_slug"), nullable=False, index=True
	)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	# App-computed at allocation time (allocated_at + settings.county_allocation_reassess_days),
	# not a DB default — the reassessment window is a runtime-configurable setting.
	reassess_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	allocation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	# NULL = currently active. Set when a later allocation replaces this one —
	# history is kept, never deleted.
	superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

	__table_args__ = (
		Index(
			"uq_county_allocations_active_county",
			"county_slug",
			unique=True,
			postgresql_where=(superseded_at.is_(None)),
		),
	)


class ClientPmBook(Base):
	"""A client's real, PMS-synced currently-managed portfolio. This — not
	CountyAllocation — is what the PERMANENT non-poach check
	(is_claimed_by_other_client) queries. No expiry/TTL: a company stays
	protected for as long as it's in this table, driven by the nightly PMS
	read-sync, never by a timer."""

	__tablename__ = "client_pm_books"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	owner_domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
	owner_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
	synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(
			"owner_domain IS NOT NULL OR owner_email IS NOT NULL",
			name="ck_client_pm_books_has_identifier",
		),
	)


# ============================================================================
# CORE ENTITY SCHEMA (global, deduplicated — see plan doc §Key decision 1)
# ============================================================================

_COMPANY_STATUSES = "'PROSPECTING','ENGAGED','DEMO_BOOKED','CLIENT','EXCLUDED'"


class Company(Base):
	"""Global, deduplicated reference row — one per real-world company, not
	one per client. company_id is a deterministic SHA-256 hex digest of the
	normalized domain, computed in application code BEFORE insert (never
	DB-generated) so ingestion can upsert idempotently.

	*** DO NOT use `gen_random_uuid()` for company_id. ***
	The blueprint's own raw migration SQL (Section 3.1.1) contradicts its
	own prose (Section 1.A) on this point — the prose specifies a
	deterministic hash, the SQL specifies gen_random_uuid(). A random UUID
	silently defeats the entire dedup / non-poach-join design this schema
	depends on. See plan doc §Key decision 1 for the full explanation.

	owning_client_id reflects the county's CURRENT active CountyAllocation
	(prospect-pool assignment), not a permanent lock — see CountyAllocation
	vs. ClientPmBook docstrings above.
	"""

	__tablename__ = "companies"

	company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
	company_name: Mapped[str] = mapped_column(Text, nullable=False)
	website: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
	domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
	county_slug: Mapped[str] = mapped_column(
		String(60), ForeignKey("counties.county_slug"), nullable=False, index=True
	)
	door_count_est: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	current_pm_software: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROSPECTING")
	# Cheap heuristic (door_count_est + LLC/Inc/Corp-style name suffix), NOT
	# real beneficial-owner resolution — see OwnerEntity docstring and the
	# Dev 1 plan's "Entity resolution — scope boundary" section. Populated
	# at promotion time by src/tasks/promotion_sweep.py.
	entity_type: Mapped[str] = mapped_column(String(30), nullable=False, default="single_property_owner")
	# Nullable: a freshly-promoted company in a not-yet-allocated county has no owner yet.
	owning_client_id: Mapped[Optional[str]] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=True, index=True
	)
	# Populated later by a future entity-resolution job (out of Dev 1 scope —
	# see OwnerEntity docstring). Additive-only, starts NULL.
	owner_entity_id: Mapped[Optional[int]] = mapped_column(
		BigInteger, ForeignKey("owner_entities.id"), nullable=True, index=True
	)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)

	__table_args__ = (
		CheckConstraint(f"status IN ({_COMPANY_STATUSES})", name="ck_companies_status"),
		CheckConstraint(
			"entity_type IN ('single_property_owner','llc_portfolio_owner')",
			name="ck_companies_entity_type",
		),
		Index("ix_companies_county_status", "county_slug", "status"),
		# Week 1 Subtask 1.1.1's idx_companies_domain requirement — named
		# explicitly. domain already carries a UNIQUE constraint (which
		# creates its own index, companies_domain_key), but the sprint doc's
		# DoD checks for this literal index name via \di, so it's declared
		# separately rather than relying on the constraint's auto-named one.
		Index("idx_companies_domain", "domain"),
	)


_CONTACT_ROLES = "'OWNER_BROKER_MD','OFFICE_MANAGER_OPS'"
_EMAIL_STATUSES = "'VERIFIED','ESTIMATED','UNVERIFIED','BOUNCED'"
_PHONE_TYPES = "'MOBILE','DIRECT_WORK','OFFICE_LANDLINE'"
_COMPLIANCE_ELIGIBILITY = "'EMAIL_COLD_ELIGIBLE','TRANSACTIONAL_SMS_ONLY','BLOCKED'"


class Contact(Base):
	"""Global, deduplicated — exactly two roles per company, enforced at the
	DB level via UniqueConstraint(company_id, contact_role_type), not just
	convention. last_outbound_touch_at is deliberately global (not per-client)
	— the 14-day cooldown is a platform-wide contact-fatigue guard, not a
	per-client one."""

	__tablename__ = "contacts"

	contact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	company_id: Mapped[str] = mapped_column(
		String(64),
		ForeignKey("companies.company_id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)
	contact_role_type: Mapped[str] = mapped_column(String(20), nullable=False)
	first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	title: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
	email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	email_status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNVERIFIED")
	phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
	phone_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
	linkedin_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
	is_opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	# NULL = never checked (distinct from FALSE = checked, not clean). The
	# compliance gate must ABSTAIN on NULL or stale dnc_checked_at, never
	# treat NULL as "assume clean".
	dnc_clean: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
	dnc_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	suppression_state: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	compliance_eligibility: Mapped[str] = mapped_column(String(30), nullable=False, default="BLOCKED")
	last_outbound_touch_at: Mapped[Optional[datetime]] = mapped_column(
		DateTime(timezone=True), nullable=True
	)
	prospect_objections: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	# Week 1 Subtask 1.2.2 (Warm-Channel Waterfall) — literal field names from
	# the master blueprint's CI-enforced predicate (§3.0.4): SMS eligibility
	# requires inbound_sms_count > 0 OR booked_appointment_id IS NOT NULL.
	booked_appointment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	inbound_sms_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)

	__table_args__ = (
		CheckConstraint(f"contact_role_type IN ({_CONTACT_ROLES})", name="ck_contacts_role_type"),
		CheckConstraint(f"email_status IN ({_EMAIL_STATUSES})", name="ck_contacts_email_status"),
		CheckConstraint(
			f"phone_type IS NULL OR phone_type IN ({_PHONE_TYPES})", name="ck_contacts_phone_type"
		),
		CheckConstraint(
			f"compliance_eligibility IN ({_COMPLIANCE_ELIGIBILITY})",
			name="ck_contacts_compliance_eligibility",
		),
		CheckConstraint(
			r"phone IS NULL OR phone ~ '^\+[1-9]\d{1,14}$'", name="ck_contacts_phone_e164"
		),
		UniqueConstraint("company_id", "contact_role_type", name="uq_contacts_company_role"),
		Index("ix_contacts_email", "email", unique=True, postgresql_where=(email.isnot(None))),
		# Week 1 Subtask 1.1.1's idx_contacts_lookup requirement.
		Index("idx_contacts_lookup", "email", "company_id", "compliance_eligibility"),
	)


class PmProfile(Base):
	"""One operating profile per client company (Week 1, Subtask 1.1.1).

	geographic_coverage_counties is an ARRAY of county_slug values, NOT the
	blueprint's literal geographic_coverage_polygon JSONB lat/lng field — the
	client's own correction ("the unit is the COUNTY... there is no metro
	layer") makes territory a set of counties, not geographic coordinates.
	County membership is validated app-side against counties.county_slug;
	Postgres has no native FK-on-array-element constraint.

	Tenant-scoped via the parent company's owning_client_id (join mode, same
	as Contact) — see config/tenant_policies.py."""

	__tablename__ = "pm_profiles"

	profile_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	company_id: Mapped[str] = mapped_column(
		String(64),
		ForeignKey("companies.company_id", ondelete="CASCADE"),
		nullable=False,
		unique=True,
	)
	specialty_tags: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
	languages_supported: Mapped[list] = mapped_column(
		ARRAY(Text), nullable=False, default=lambda: ["English"]
	)
	asset_class_strengths: Mapped[list] = mapped_column(
		ARRAY(Text), nullable=False, default=lambda: ["Single Family", "Small Multifamily"]
	)
	geographic_coverage_counties: Mapped[list] = mapped_column(ARRAY(String(60)), nullable=False, default=list)
	historical_close_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
	average_speed_to_lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	show_rate_percentage: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)


# ============================================================================
# ENTITY RESOLUTION — schema scaffolding only, NOT populated by Dev 1.
# Structural analog of Forced Action's BuyerEntity/BuyerEntityLink
# (src/core/models.py ~line 9121). Additive-only: companies.owner_entity_id
# starts NULL, and this exists so a future Prospecting Agent workstream
# (full LLC beneficial-owner piercing, the Owner Score formula) doesn't need
# a breaking migration to land. See Dev 1 plan's "Entity resolution — scope
# boundary" section.
# ============================================================================

_OWNER_ENTITY_TYPES = "'INDIVIDUAL','LLC','TRUST','CORPORATE','REIT'"


class OwnerEntity(Base):
	__tablename__ = "owner_entities"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
	entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
	portfolio_door_count_est: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	confidence_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	verification_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unverified")
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)

	__table_args__ = (
		CheckConstraint(f"entity_type IN ({_OWNER_ENTITY_TYPES})", name="ck_owner_entities_type"),
	)


class OwnerEntityLink(Base):
	"""One canonical entity per source record — UniqueConstraint(source_table,
	source_id) mirrors FA's uq_buyer_entity_link_source."""

	__tablename__ = "owner_entity_links"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	owner_entity_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("owner_entities.id", ondelete="CASCADE"), nullable=False, index=True
	)
	source_table: Mapped[str] = mapped_column(String(30), nullable=False)
	source_id: Mapped[str] = mapped_column(String(64), nullable=False)
	match_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	match_method: Mapped[str] = mapped_column(String(30), nullable=False)
	linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint("source_table IN ('companies','contacts')", name="ck_owner_entity_links_source"),
		UniqueConstraint("source_table", "source_id", name="uq_owner_entity_link_source"),
	)


# ============================================================================
# INGESTION STAGING (Akrash handoff) — see Dev 1 plan §Key decision 6.
# Two tables, not one flat row: each contact needs independently trackable
# status to support "promote company with the one clean contact" (confirmed
# behavior). No client_id on either table — Akrash has no visibility into
# the client roster; ownership is assigned only at promotion time via the
# county-allocation join.
# ============================================================================

_VALIDATION_STATUSES = "'pending','cleared','quarantined','rejected'"


class RawProspectCompany(Base):
	__tablename__ = "raw_prospect_companies"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	# Same hash function as Company.company_id (BaseIngestLoader.compute_company_id),
	# computed at ingest so promotion is a natural match, not a fuzzy one.
	company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
	company_name: Mapped[str] = mapped_column(Text, nullable=False)
	domain: Mapped[str] = mapped_column(String(255), nullable=False)
	# Nullable: a NULL blocks promotion rather than guessing a county.
	county_slug: Mapped[Optional[str]] = mapped_column(
		String(60), ForeignKey("counties.county_slug"), nullable=True
	)
	door_count_est: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	door_count_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	source_channel: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
	source_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	submitted_by: Mapped[str] = mapped_column(String(100), nullable=False)
	enrichment_provider: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	enrichment_timestamp: Mapped[Optional[datetime]] = mapped_column(
		DateTime(timezone=True), nullable=True
	)
	# Full original payload, verbatim — traceability when structured columns
	# don't capture everything Akrash sent.
	raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
	reject_reason_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
	promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	promoted_company_id: Mapped[Optional[str]] = mapped_column(
		String(64), ForeignKey("companies.company_id"), nullable=True
	)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(f"validation_status IN ({_VALIDATION_STATUSES})", name="ck_raw_prospect_companies_status"),
		Index("ix_raw_prospect_companies_status", "validation_status"),
	)


class RawProspectContact(Base):
	__tablename__ = "raw_prospect_contacts"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	company_ref_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("raw_prospect_companies.id"), nullable=False, index=True
	)
	role: Mapped[str] = mapped_column(String(20), nullable=False)
	first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	title: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
	email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
	source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
	reject_reason_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(f"role IN ({_CONTACT_ROLES})", name="ck_raw_prospect_contacts_role"),
		CheckConstraint(
			f"validation_status IN ({_VALIDATION_STATUSES})", name="ck_raw_prospect_contacts_status"
		),
	)


# ============================================================================
# SHARED LEDGER / AUDIT TABLES
# ============================================================================

class Event(Base):
	"""Append-only, client_id-scoped shared ledger — every other subsystem
	reads from and writes to this."""

	__tablename__ = "events"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	event_type: Mapped[str] = mapped_column(String(60), nullable=False)
	entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
	entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
	payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	actor: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		Index("ix_events_client_created", "client_id", "created_at"),
		Index("ix_events_entity", "entity_type", "entity_id"),
		# Named to satisfy Week 1 Subtask 1.1.1's idx_events_client_type
		# requirement; columns are (client_id, event_type, created_at) — this
		# schema uses created_at, not the blueprint's occurred_at (no such
		# column exists here), and event_type/entity_type/entity_id are the
		# generic polymorphic design kept from Week 0 (see plan doc's
		# contradiction ledger, item 3).
		Index("idx_events_client_type", "client_id", "event_type", "created_at"),
	)


class ComplianceGateCheck(Base):
	"""Append-only audit trail for the deterministic compliance gate — a
	permanent, queryable answer to 'why didn't this contact get emailed'."""

	__tablename__ = "compliance_gate_checks"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	contact_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("contacts.contact_id"), nullable=False, index=True
	)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	check_name: Mapped[str] = mapped_column(String(60), nullable=False)
	status: Mapped[str] = mapped_column(String(10), nullable=False)
	detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint("status IN ('PASS','FAIL','ABSTAIN')", name="ck_compliance_gate_checks_status"),
	)


class SmsDispatchLog(Base):
	"""DB-layer backstop of the three-layer cold-SMS block (Week 1 Subtask
	1.2.3, master blueprint §3.1.2/§3.0.4). The CHECK constraint enforces
	the same predicate as campaign_readiness_gate.is_engaged() directly at
	the database engine, independent of the application-layer linter in
	src/services/sms_dispatch.py. No SMS vendor is contracted yet (same
	situation as the DNC vendor) — this table exists because neither the
	blueprint nor the DoD gives a schema for "an outbound SMS record"."""

	__tablename__ = "sms_dispatch_log"

	dispatch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	contact_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("contacts.contact_id"), nullable=False, index=True
	)
	inbound_sms_count_at_send: Mapped[int] = mapped_column(Integer, nullable=False)
	booked_appointment_id_at_send: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	status: Mapped[str] = mapped_column(String(20), nullable=False, default="SENT")
	provider_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(
			"status IN ('PENDING','SENT','FAILED','BLOCKED','UNKNOWN')", name="ck_sms_dispatch_log_status"
		),
		CheckConstraint(
			"inbound_sms_count_at_send > 0 OR booked_appointment_id_at_send IS NOT NULL",
			name="ck_sms_dispatch_log_not_cold",
		),
		UniqueConstraint("client_id", "idempotency_key", name="uq_sms_dispatch_log_client_idempotency_key"),
	)


# ============================================================================
# DELIVERABILITY INFRASTRUCTURE (20 domains / 40 mailboxes)
# client_id NULL = Blackink self-marketing (5 of the 20 domains). NULL vs.
# non-NULL structurally enforces "never pooled across clients" — any
# "domains available to client X" query is WHERE client_id = X OR client_id
# IS NULL. DNS/SPF/DKIM/DMARC setup itself is a manual runbook (Dev 1 plan
# §Key decision 5) — these tables only track state.
# ============================================================================

class SendingDomain(Base):
	__tablename__ = "sending_domains"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
	client_id: Mapped[Optional[str]] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=True, index=True
	)
	cluster_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	spf_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	dkim_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	dmarc_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	warmup_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
	health_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	quarantine_state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
	quarantined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	quarantine_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	is_reserve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(
			"warmup_status IN ('not_started','warming','warmed','paused')",
			name="ck_sending_domains_warmup_status",
		),
		CheckConstraint(
			"quarantine_state IN ('active','quarantined','reserve')",
			name="ck_sending_domains_quarantine_state",
		),
	)


class Mailbox(Base):
	__tablename__ = "mailboxes"

	id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	domain_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("sending_domains.id"), nullable=False, index=True
	)
	mailbox_address: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
	# Denormalized from domain_id.client_id for query convenience — must
	# always equal the parent domain's client_id, enforced app-side (see
	# src/services/deliverability.py).
	client_id: Mapped[Optional[str]] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=True, index=True
	)
	instantly_account_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	warmup_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
	health_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	quarantine_state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(
			"warmup_status IN ('not_started','warming','warmed','paused')",
			name="ck_mailboxes_warmup_status",
		),
		CheckConstraint(
			"quarantine_state IN ('active','quarantined','reserve')", name="ck_mailboxes_quarantine_state"
		),
	)


class AgentWorkOrder(Base):
	"""The durable row behind every Slack action card (Dev 3 plan §3.3).
	action_id is APPLICATION-GENERATED (uuid4 in src/services/work_orders),
	never the DEFAULT below — see this table's migration
	(migrations/apply_agent_work_orders.py) for why: it is part of the
	payload-hash preimage (src/services/slack/payload_hash.py), so the
	digest cannot be computed until action_id is known.

	client_id references clients(client_id), NOT companies(company_id) —
	the blueprint's own §5.3 raw SQL has the latter, which is wrong on its
	face; same class of blueprint/code contradiction as Company.company_id
	above."""

	__tablename__ = "agent_work_orders"

	action_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
	client_id: Mapped[str] = mapped_column(String(40), ForeignKey("clients.client_id"), nullable=False)
	entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
	entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
	opportunity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
	action_class: Mapped[str] = mapped_column(String(100), nullable=False)
	autonomy_band: Mapped[str] = mapped_column(String(20), nullable=False)
	risk_class: Mapped[str] = mapped_column(String(20), nullable=False)
	confidence_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
	recipient: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	config_fingerprint: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
	hash_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
	status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
	idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
	slack_channel_id: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
	slack_message_ts: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
	decided_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
	decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	execution_receipt: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
	error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)

	__table_args__ = (
		UniqueConstraint("client_id", "idempotency_key", name="uq_agent_work_orders_idem"),
		CheckConstraint(
			"status IN ('QUEUED','APPROVED','REJECTED','SNOOZED','SKIPPED','DONE','EXECUTING','FAILED')",
			name="ck_agent_work_orders_status",
		),
		CheckConstraint(
			"autonomy_band IN ('BAND_1_OBSERVE','BAND_2_ONE_TAP','BAND_3_AUTO')",
			name="ck_agent_work_orders_band",
		),
		CheckConstraint(
			"risk_class IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="ck_agent_work_orders_risk"
		),
		Index("ix_awo_client_status", "client_id", "status"),
		Index("ix_awo_entity", "entity_type", "entity_id"),
		Index("ix_awo_due", "status", "due_at"),
	)


# ============================================================================
# MEETING OUTCOMES (post-demo Slack form — Week 2 / Sprint 2A)
# Separate from appointment_outcomes (billing verification). This table
# captures intelligence gathered during the meeting itself: PM software,
# door count, stated objections, next action. The freshness guard in
# src/services/meeting_outcomes.py ensures mirrored fields on contacts and
# companies always reflect the most recent meeting, not whichever was
# submitted last.
# ============================================================================

_ATTENDANCE_STATUSES = "'ATTENDED','NO_SHOW','RESCHEDULED','CANCELLED'"


class MeetingOutcome(Base):
	__tablename__ = "meeting_outcomes"

	outcome_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
	client_id: Mapped[str] = mapped_column(
		String(40), ForeignKey("clients.client_id"), nullable=False, index=True
	)
	contact_id: Mapped[int] = mapped_column(
		BigInteger, ForeignKey("contacts.contact_id"), nullable=False, index=True
	)
	# Denormalized from contact_id.company_id for efficient company-level MAX queries
	# (avoids a join in the freshness guard). Must always match the contact's company.
	company_id: Mapped[str] = mapped_column(
		String(64), ForeignKey("companies.company_id"), nullable=False, index=True
	)
	meeting_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	attendance_status: Mapped[str] = mapped_column(String(20), nullable=False)
	pm_software_stated: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	door_count_stated: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
	objections_stated: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	next_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	submitted_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

	__table_args__ = (
		CheckConstraint(
			f"attendance_status IN ({_ATTENDANCE_STATUSES})",
			name="ck_meeting_outcomes_attendance",
		),
		Index("ix_meeting_outcomes_contact_occurred", "contact_id", "meeting_occurred_at"),
		Index("ix_meeting_outcomes_company_occurred", "company_id", "meeting_occurred_at"),
	)
