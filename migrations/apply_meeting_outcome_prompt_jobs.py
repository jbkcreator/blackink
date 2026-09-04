"""
Provision meeting_outcome_prompt_jobs (Addendum to Subtask 3.2.1 —
Post-Booking "Log Outcome" Trigger Card).

One row per INTERNAL_SALES_DEMO booking, fired at exactly
bookings.scheduled_at (the meeting's own start time — never a guessed end
time) by src/tasks/meeting_outcome_prompt_sender.py, which posts a "Log
Outcome" card to #blackink-setter. Sibling to no_show_prompt_jobs (see
apply_no_show_prompt_jobs.py) — same SKIP LOCKED claim shape, its own table
because the outcome vocabulary and the follow-up lifecycle differ.

Scoped to INTERNAL_SALES_DEMO only: meeting_outcomes.contact_id/company_id
are NOT NULL FKs to contacts/companies, and only a sales-demo booking
carries target_contact_id/target_company_id. A CLIENT_OWNER_BOOKING links to
owner_contacts and has no contacts row at all, so a "closer logs a demo
outcome" row is unrepresentable for one.

The card itself is a real agent_work_orders row (work_order_action_id below),
so its SHA-256 payload/recipient/config binding comes from the existing
src/services/slack/payload_hash.py rather than a second implementation. The
24-hour card expiry is NOT part of that hash (adding a timestamp window to
the FIXED preimage would change every digest and force a HASH_VERSION bump) —
it is an explicit created_at + 24h check at click and submit time.

Status lifecycle: PENDING -> SENDING -> SENT, with:
  SKIPPED   - scheduled_for already past when the job was created, or an
              outcome for this booking was already recorded elsewhere (3.2.3's
              "Mark No-Show" card in #blackink-command fired first).
  BLOCKED   - a required input is missing, so the card cannot be posted
              attributably: the booking's target_contact_id/target_company_id
              is unresolved (PENDING_RECONCILIATION), or the rep's
              calendar_connections row has no rep_slack_user_id. Both are
              re-checked and auto-promoted back to PENDING by the sweep's
              self-heal step once the underlying data appears — the same
              pattern src/services/show_rate_reminders.py uses for
              MISSING_OVS_*.
  CANCELLED - the booking itself was cancelled before this job fired.
  EXPIRED   - the card's own 24-hour window closed with no outcome logged.
              No further reminder pings, matching how every other expired
              interactive card in the system behaves.

posted_at anchors both the 4-hour unclicked reminder ping and the 24-hour
expiry; reminder_sent_at makes that ping exactly-once.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_meeting_outcome_prompt_jobs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS meeting_outcome_prompt_jobs (
		prompt_job_id          BIGSERIAL    PRIMARY KEY,
		client_id              VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		booking_id             BIGINT       NOT NULL REFERENCES bookings(booking_id),
		scheduled_for          TIMESTAMPTZ  NOT NULL,
		status                 VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		last_error             TEXT,
		work_order_action_id   UUID,
		slack_channel_id       VARCHAR(20),
		slack_message_ts       VARCHAR(30),
		posted_at              TIMESTAMPTZ,
		reminder_sent_at       TIMESTAMPTZ,
		claimed_at             TIMESTAMPTZ,
		created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_meeting_outcome_prompt_jobs_booking UNIQUE (booking_id),
		CONSTRAINT ck_meeting_outcome_prompt_jobs_status CHECK (
			status IN ('PENDING', 'SENDING', 'SENT', 'SKIPPED', 'BLOCKED',
			           'CANCELLED', 'FAILED', 'EXPIRED')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_booking ON meeting_outcome_prompt_jobs (booking_id)",
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_status ON meeting_outcome_prompt_jobs (status)",
	# Supports the 4h-ping / 24h-expiry sweep over already-posted cards.
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_posted ON meeting_outcome_prompt_jobs (status, posted_at)",
	# Same least-privilege posture as no_show_prompt_jobs — no DELETE for
	# either role, a job row is never removed, only status-transitioned.
	"REVOKE DELETE ON meeting_outcome_prompt_jobs FROM blackink_app",
	"REVOKE DELETE ON meeting_outcome_prompt_jobs FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON meeting_outcome_prompt_jobs TO blackink_app",
	"GRANT USAGE ON SEQUENCE meeting_outcome_prompt_jobs_prompt_job_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON meeting_outcome_prompt_jobs TO blackink_system",
	"GRANT USAGE ON SEQUENCE meeting_outcome_prompt_jobs_prompt_job_id_seq TO blackink_system",

	# ── Intelligence-mirror write path for the outcome modal ─────────────────
	# src/services/meeting_outcomes.py mirrors prospect intelligence onto
	# contacts.prospect_objections and companies.current_pm_software /
	# door_count_est. Those two tables are RLS-scoped through
	# companies.owning_client_id, which is NULL for every unallocated
	# prospect — so the plain UPDATEs silently affected ZERO rows whenever
	# the caller was a session scoped to client_id='BLACKINK_INTERNAL_SALES'
	# (which is every Slack interactive path, including 3.2.3's existing
	# "Mark No-Show" flow: a pre-existing silent no-op this migration also
	# fixes, not only a new requirement of the "Log Outcome" modal, whose
	# DoD explicitly asserts contacts.prospect_objections is updated).
	#
	# Same write-side SECURITY DEFINER escape hatch as
	# pause_contact_after_no_show()/resume_contact_if_rebooked()
	# (apply_bookings.py), hardened identically: caller scope read from the
	# session's own RLS context and never accepted as a parameter,
	# search_path = pg_catalog, EXECUTE revoked from PUBLIC and granted only
	# to blackink_app, owned by CURRENT_USER so SECURITY DEFINER actually
	# bypasses RLS. The freshness guard itself stays in Python — it reads
	# meeting_outcomes, which IS directly visible to that scoped session —
	# so these functions do exactly one UPDATE each and make no decisions.
	"""
	CREATE OR REPLACE FUNCTION mirror_contact_objections(
		p_contact_id BIGINT,
		p_objections TEXT
	)
	RETURNS VOID
	SECURITY DEFINER
	SET search_path = pg_catalog
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_requesting_client_id VARCHAR(40);
	BEGIN
		v_requesting_client_id := current_setting('app.current_client_id', true);
		IF v_requesting_client_id IS DISTINCT FROM 'BLACKINK_INTERNAL_SALES' THEN
			RETURN;
		END IF;
		UPDATE public.contacts
		SET prospect_objections = p_objections, updated_at = NOW()
		WHERE contact_id = p_contact_id;
	END;
	$$
	""",
	"REVOKE EXECUTE ON FUNCTION mirror_contact_objections(BIGINT, TEXT) FROM PUBLIC",
	"GRANT EXECUTE ON FUNCTION mirror_contact_objections(BIGINT, TEXT) TO blackink_app",
	"ALTER FUNCTION mirror_contact_objections(BIGINT, TEXT) OWNER TO CURRENT_USER",

	"""
	CREATE OR REPLACE FUNCTION mirror_company_intelligence(
		p_company_id  VARCHAR,
		p_pm_software VARCHAR,
		p_door_count  INTEGER
	)
	RETURNS VOID
	SECURITY DEFINER
	SET search_path = pg_catalog
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_requesting_client_id VARCHAR(40);
	BEGIN
		v_requesting_client_id := current_setting('app.current_client_id', true);
		IF v_requesting_client_id IS DISTINCT FROM 'BLACKINK_INTERNAL_SALES' THEN
			RETURN;
		END IF;
		-- COALESCE for the same reason the Python UPDATE uses it: a meeting
		-- where the rep didn't ask about PM software must not erase a value
		-- captured in an earlier one.
		UPDATE public.companies
		SET current_pm_software = COALESCE(p_pm_software, current_pm_software),
		    door_count_est      = COALESCE(p_door_count, door_count_est),
		    updated_at          = NOW()
		WHERE company_id = p_company_id;
	END;
	$$
	""",
	"REVOKE EXECUTE ON FUNCTION mirror_company_intelligence(VARCHAR, VARCHAR, INTEGER) FROM PUBLIC",
	"GRANT EXECUTE ON FUNCTION mirror_company_intelligence(VARCHAR, VARCHAR, INTEGER) TO blackink_app",
	"ALTER FUNCTION mirror_company_intelligence(VARCHAR, VARCHAR, INTEGER) OWNER TO CURRENT_USER",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_meeting_outcome_prompt_jobs: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
