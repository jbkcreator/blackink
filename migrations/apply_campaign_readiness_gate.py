"""
Provision evaluate_campaign_readiness() (Dev 1 plan, Week 1 Subtask 1.2.1).

Standalone SQL implementation of the compliance gate's first two checks —
Check 1 (global/explicit opt-out) and Check 2 (cross-client non-poach) —
built as a literal, psql-callable Postgres function per 1.2.1's DoD, rather
than folded into the existing Python evaluate_compliance_gate()
(src/services/compliance_gate.py), which deliberately runs every check
without short-circuiting for a complete audit trail. This function is the
opposite by design: Check 2 only runs if Check 1 passes.

Reuses the same SECURITY DEFINER / no-PUBLIC-EXECUTE pattern as
is_claimed_by_other_client() (apply_compliance_gate_audit.py), but unlike
that function this one DOES write the matching client_id into the
non_poach_suppressed event payload — the DoD explicitly asks for it, so the
audit trail carries it even though the function's return value still never
discloses it to the caller.

contact_id is BIGINT, not UUID as the DoD literally names it — matches
contacts.contact_id's actual type (same class of blueprint-vs-reality
deviation as company_id being sha256(domain), not gen_random_uuid()).

Idempotent: CREATE OR REPLACE FUNCTION.

    PYTHONPATH=. python migrations/apply_campaign_readiness_gate.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE OR REPLACE FUNCTION evaluate_campaign_readiness(p_contact_id BIGINT)
	RETURNS TEXT
	SECURITY DEFINER
	SET search_path = public
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_is_opted_out         BOOLEAN;
		v_suppression_state    BOOLEAN;
		v_company_id           VARCHAR(64);
		v_email                VARCHAR(255);
		v_domain               VARCHAR(255);
		v_requesting_client_id VARCHAR(40);
		v_matched_client_id    VARCHAR(40);
	BEGIN
		SELECT is_opted_out, suppression_state, company_id, email
		  INTO v_is_opted_out, v_suppression_state, v_company_id, v_email
		  FROM contacts WHERE contact_id = p_contact_id;

		IF NOT FOUND THEN
			RAISE EXCEPTION 'evaluate_campaign_readiness: contact % not found', p_contact_id;
		END IF;

		-- Check 1 — Global/Explicit Opt-Out. Strictly first; short-circuits.
		IF v_is_opted_out OR v_suppression_state THEN
			RETURN 'PERMANENTLY_BLOCKED';
		END IF;

		-- Check 2 — Cross-Client Non-Poach. Only reached if Check 1 passed.
		v_requesting_client_id := current_setting('app.current_client_id', true);
		IF v_requesting_client_id IS NULL OR v_requesting_client_id = '' THEN
			RAISE EXCEPTION 'evaluate_campaign_readiness: no tenant context set (app.current_client_id)';
		END IF;

		SELECT domain INTO v_domain FROM companies WHERE company_id = v_company_id;

		SELECT b.client_id INTO v_matched_client_id
		  FROM client_pm_books b
		  WHERE b.client_id <> v_requesting_client_id
		    AND ((v_domain IS NOT NULL AND b.owner_domain = v_domain)
		         OR (v_email IS NOT NULL AND b.owner_email = v_email))
		  LIMIT 1;

		IF v_matched_client_id IS NOT NULL THEN
			INSERT INTO events (client_id, event_type, entity_type, entity_id, payload)
			VALUES (
				v_requesting_client_id,
				'non_poach_suppressed',
				'contact',
				p_contact_id::text,
				jsonb_build_object('matched_client_id', v_matched_client_id, 'company_id', v_company_id)
			);
			RETURN 'SUPPRESSED';
		END IF;

		-- Both checks passed — Check 3 (Subtask 1.2.2, not yet implemented) picks up from here.
		RETURN 'PASS';
	END;
	$$
	""",
	# Postgres grants EXECUTE on every new function to PUBLIC by default —
	# revoke it explicitly, same posture as is_claimed_by_other_client: only
	# blackink_app calls this. akrash_ingest and blackink_system get no path
	# to it at all.
	"REVOKE EXECUTE ON FUNCTION evaluate_campaign_readiness(BIGINT) FROM PUBLIC",
	"GRANT EXECUTE ON FUNCTION evaluate_campaign_readiness(BIGINT) TO blackink_app",
	# CREATE OR REPLACE FUNCTION does not transfer ownership on an existing
	# function — explicit here so this migration is self-correcting
	# regardless of who created it first. SECURITY DEFINER only bypasses RLS
	# if the owner does (superuser/BYPASSRLS), same rationale as
	# apply_compliance_gate_audit.py's identical line.
	"ALTER FUNCTION evaluate_campaign_readiness(BIGINT) OWNER TO CURRENT_USER",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_campaign_readiness_gate: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
