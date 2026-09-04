"""Dev-only seeder for the post-booking "Log Outcome" trigger card
(Addendum to Subtask 3.2.1). Creates one due INTERNAL_SALES_DEMO booking
with a PENDING meeting_outcome_prompt_jobs row, so the running
meeting_outcome_prompt_sender sweep posts the card within one tick.

    ENV_FILE=.env.local PYTHONPATH=. python scripts/dev_seed_outcome_card.py --rep U0BN27JB8CW

Idempotent-ish: it clears any prior demo rows (same fixed ids /
external_calendar_id) before re-seeding, so it can be run repeatedly.
Never import this from application code — it writes test data directly.
"""
import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context

_CLIENT = "BLACKINK_INTERNAL_SALES"
_COMPANY_ID = "demo-outcome-co"
_EMAIL = "demo-outcome@example.com"
_EXT_CAL = "rep-demo"


def _clear() -> None:
	with get_owner_db_context() as s:
		s.execute(text(
			"DELETE FROM meeting_outcome_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE calendar_connection_id IN "
			"(SELECT connection_id FROM calendar_connections WHERE client_id = :c AND external_calendar_id = :e))"
		), {"c": _CLIENT, "e": _EXT_CAL})
		s.execute(text("DELETE FROM meeting_outcomes WHERE contact_id IN "
			"(SELECT contact_id FROM contacts WHERE email = :e)"), {"e": _EMAIL})
		s.execute(text("DELETE FROM agent_work_orders WHERE client_id = :c AND entity_type = 'booking'"), {"c": _CLIENT})
	with get_system_db_context() as s:
		s.execute(text(
			"DELETE FROM bookings WHERE calendar_connection_id IN "
			"(SELECT connection_id FROM calendar_connections WHERE client_id = :c AND external_calendar_id = :e)"
		), {"c": _CLIENT, "e": _EXT_CAL})
		s.execute(text("DELETE FROM calendar_connections WHERE client_id = :c AND external_calendar_id = :e"),
				  {"c": _CLIENT, "e": _EXT_CAL})


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--rep", required=True, help="Slack member ID of the assigned closer (also put this in BLACKINK_GLOBAL_APPROVERS)")
	args = ap.parse_args()

	_clear()

	with get_owner_db_context() as s:
		s.execute(text(
			"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
			"VALUES (:cid, 'Demo Outcome PM', 'demo-outcome.example.com', (SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') "
			"ON CONFLICT (company_id) DO NOTHING"
		), {"cid": _COMPANY_ID})
		contact_id = s.execute(text(
			"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
			"VALUES (:cid, 'OWNER_BROKER_MD', 'Dana', 'Prospect', :email, '+14075550100') "
			"ON CONFLICT (company_id, contact_role_type) DO UPDATE SET first_name = 'Dana' RETURNING contact_id"
		), {"cid": _COMPANY_ID, "email": _EMAIL}).scalar()

	with get_system_db_context() as s:
		conn_id = s.execute(text(
			"INSERT INTO calendar_connections "
			"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
			" verification_secret, initial_sync_done, rep_slack_user_id) "
			"VALUES (:c, 'GOOGLE', 'INTERNAL_SALES_DEMO', :e, 'sub-demo', 'secret', TRUE, :rep) RETURNING connection_id"
		), {"c": _CLIENT, "e": _EXT_CAL, "rep": args.rep}).scalar()
		scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
		booking_id = s.execute(text(
			"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, event_status, "
			"scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
			"VALUES (:c, 'GOOGLE', :conn, 'evt-demo', 'CONFIRMED', :sch, '{}', :co, :ct, 'MATCHED') RETURNING booking_id"
		), {"c": _CLIENT, "conn": conn_id, "sch": scheduled_at, "co": _COMPANY_ID, "ct": contact_id}).scalar()
		s.execute(text(
			"INSERT INTO meeting_outcome_prompt_jobs (client_id, booking_id, scheduled_for, status) "
			"VALUES (:c, :b, :sch, 'PENDING')"
		), {"c": _CLIENT, "b": booking_id, "sch": scheduled_at})

	print(f"seeded booking_id={booking_id} contact_id={contact_id} rep={args.rep} — card posts on the next sweep tick (~60s)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
