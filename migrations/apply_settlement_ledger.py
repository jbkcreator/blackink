"""
Provision the settlement ledger (Subtask 1.2.2 — 50/50 Settlement Split
Engine & 60-Day Clawback Monitor).

Blueprint source: Tasks/Project_Blackink_-_Complete_Implementation_Blueprint__
Full__v2.md §3.2.2 (L568-629). Charges a client 50% when a signed
management agreement is confirmed (door_signed, see apply_pms_agreements.py),
50% at day 60 — voided if the agreement was terminated inside that window.
Subtask 1.1.1's appointments schema explicitly deferred "opportunity-level
billing idempotency is enforced at settlement" to this table.

── Deliberate adaptation of the printed DDL (blueprint L609-629) ──
  * client_id UUID            -> VARCHAR(40) REFERENCES clients      (RLS boundary, as in appointments)
  * owner_id UUID NOT NULL    -> company_id VARCHAR(64) (nullable) — owner_entities is unpopulated
    dedup scaffolding, not a usable FK target; the real party in this schema is companies/owner_contacts.
  * property_id UUID NOT NULL -> DROPPED. No properties table exists anywhere in this schema.
    pms_agreements.pms_property_ref (an opaque PMS string) is the closest analogue and lives on
    the agreement, not duplicated here.
  * evidence_packet_url TEXT NOT NULL -> NULLable at the column level (the row is created before
    the packet is compiled) but functionally NOT NULL for any charged row — see the fail-closed
    CHECK and trigger below. No general-purpose object storage has ever been built in this repo
    (the same gap that leaves contacts.ovs_pdf_url unpopulated); Stripe Files + FileLink is the
    store here specifically because Stripe Invoices have no attachment field of their own.
  * installment_1_status / installment_2_status use 'CHARGED' as the terminal value (matching the
    subtask's own Definition of Done, which asserts installment_1_status = 'CHARGED' literally),
    not 'PAID'. A distinct 'SETTLING' value carries ACH's asynchronous pending-settlement window
    so 'CHARGED' always means Stripe has confirmed money moved.
  * is_clawed_back BOOLEAN DEFAULT FALSE -> STORED generated column, ALWAYS AS
    (installment_2_status = 'VOIDED_CLAWBACK'), so the flag can never independently disagree with
    the status that is supposed to explain it.

── Pricing is NOT resolved by fiat — the blueprint contradicts itself ──
The printed settlement DDL bills total_bounty_cents per door, but the SAME blueprint's own pricing
registry (Tasks/…v2.md:1085) prices `appt_standard` as "$99 flat — Any door count. Replaces all
door-band pricing." settlement_offer_config below supports BOTH bases (pricing_basis = 'PER_DOOR'
| 'FLAT_PER_AGREEMENT'), each requiring its own amount column and forbidding the other's, so a
half-configured offer cannot charge and a basis flip cannot silently reuse the wrong column. Under
FLAT_PER_AGREEMENT, door_count is recorded on the ledger as evidence only — never a multiplier
(asserted by tests/test_settlement_pricing_basis.py). settlement_enabled ships FALSE and both
amount columns ship NULL: nothing charges until an operator picks a basis AND an amount, and this
migration deliberately does not pick a price for the client.

── Zero-dollars-upfront and every other fail-closed rule are enforced here, not by convention ──
Six independent layers (see also src/services/settlement/charge.py and
tests/test_no_upfront_charge_paths.py):
  1. door_signed_at and pms_agreement_id are NOT NULL with a composite same-tenant FK — no ledger
     row is representable without a signed agreement.
  2. The trg_settlement_guard_transition trigger (below) rejects INSERT unless the referenced
     agreement is ACTIVE with a door_signed_at matching this row's own snapshot; freezes
     client_id/opportunity_id/pms_agreement_id/door_signed_at and all three cents columns; rejects
     a transition into CHARGING/SETTLING/CHARGED unless the corresponding installment's
     preconditions hold (agreement still ACTIVE for installment 1, not clawed-back-eligible for
     installment 2); rejects that same transition while evidence_packet_url IS NULL — a
     reviewer-required fail-closed rule, no "compiled but unpublished, proceed anyway" path exists;
     and rejects it for a non-PMS_SYNC agreement unless allow_synthetic_charge = TRUE, which
     src/services/settlement/charge.py only ever sets when the configured Stripe secret key is a
     sk_test_ key — so a SYNTHETIC door_signed event (the DoD's own test path) can exercise the
     full pipeline in Stripe test mode and is structurally unable to bill a real client in
     production. Also rejects CHARGED -> anything (a refund is a new Stripe object, not a rewrite).
  3. settlement_offer_config.settlement_enabled defaults FALSE with the pricing CHECK above.
  4. src/services/pms_sync.py's StubPmsProvider returns None (couldn't determine) for every call
     with no PMS integration contracted — so day-60 re-verification neither charges nor claws back
     on a guess.
  5. src/services/settlement/charge.py's charge_installment() is the only function in the repo that
     calls a money-moving Stripe API, and it re-reads the row/agreement and re-derives the amount
     and precondition from the DB rather than trusting its caller.
  6. tests/test_no_upfront_charge_paths.py walks src/ asserting invoice-finalize/pay/item-create and
     uncaptured PaymentIntent.create calls exist ONLY in that one module.

── Exactly-once billing — the promise appointments.py's docstring deferred here ──
UNIQUE (client_id, opportunity_id): many appointment rows can share one opportunity_id
(idx_opportunity_dedupe on appointments is deliberately non-unique for that reason) but at most one
bounty may ever be billed for it. UNIQUE (client_id, pms_agreement_id): catches a duplicated
door_signed ingest even with a fresh opportunity_id. A partial unique index excluding VOIDED rows
was considered and rejected — it would let a voided-then-reinserted row re-bill the same
opportunity. Accepted trade-off: a single opportunity that legitimately produces a second billable
agreement fails the INSERT and bills nothing; the failure direction is always under-billing, never
double-billing, and unwedging it is a deliberate product decision plus a migration, not a silent
retry. Two further partial unique indices guard inst1/inst2_stripe_invoice_id — a Stripe invoice
backs at most one installment, so an idempotency-key collision surfaces as an error instead of a
silently duplicated financial record.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE FUNCTION. Run AFTER
apply_pms_agreements.py, BEFORE apply_rls_policies.py (which must be RE-RUN after this migration).

    PYTHONPATH=. python migrations/apply_settlement_ledger.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	# ── Global reference config — commercial terms in a config row, never a
	#    hardcoded offer branch. Same precedent as payment_auth_offer_config
	#    (Subtask 1.2.1): ships disabled, an operator flips it per confirmed
	#    offer. NOT registered in config/tenant_policies.py — not
	#    tenant-bearing, same class as payment_auth_offer_config /
	#    entitlement_offers.
	"""
	CREATE TABLE IF NOT EXISTS settlement_offer_config (
		offer_code             VARCHAR(50)  PRIMARY KEY,
		settlement_enabled     BOOLEAN      NOT NULL DEFAULT FALSE,
		pricing_basis          VARCHAR(24)  NOT NULL DEFAULT 'PER_DOOR',
		per_door_bounty_cents  BIGINT,
		flat_bounty_cents      BIGINT,
		installment_1_bps      INTEGER      NOT NULL DEFAULT 5000,
		clawback_window_days   INTEGER      NOT NULL DEFAULT 60,
		created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_settlement_offer_basis CHECK (
			pricing_basis IN ('PER_DOOR', 'FLAT_PER_AGREEMENT')
		),
		CONSTRAINT ck_settlement_offer_bps CHECK (installment_1_bps BETWEEN 0 AND 10000),
		CONSTRAINT ck_settlement_offer_needs_price CHECK (
			NOT settlement_enabled OR (
				(pricing_basis = 'PER_DOOR'
					AND per_door_bounty_cents IS NOT NULL AND flat_bounty_cents IS NULL)
				OR (pricing_basis = 'FLAT_PER_AGREEMENT'
					AND flat_bounty_cents IS NOT NULL AND per_door_bounty_cents IS NULL)
			)
		)
	)
	""",
	"GRANT SELECT ON settlement_offer_config TO blackink_app",
	"GRANT SELECT ON settlement_offer_config TO blackink_system",
	# ── The ledger itself ──
	"""
	CREATE TABLE IF NOT EXISTS settlement_transactions (
		transaction_id             BIGSERIAL    PRIMARY KEY,
		client_id                  VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		pms_agreement_id           BIGINT       NOT NULL,
		opportunity_id             UUID         NOT NULL,
		company_id                 VARCHAR(64)  REFERENCES companies(company_id),
		offer_code                 VARCHAR(50)  NOT NULL REFERENCES settlement_offer_config(offer_code),
		door_count                 INTEGER      NOT NULL DEFAULT 1 CHECK (door_count > 0),
		total_bounty_cents         BIGINT       NOT NULL CHECK (total_bounty_cents > 0),
		installment_1_cents        BIGINT       NOT NULL CHECK (installment_1_cents > 0),
		installment_2_cents        BIGINT       NOT NULL CHECK (installment_2_cents >= 0),

		installment_1_status       VARCHAR(30)  NOT NULL DEFAULT 'PENDING',
		inst1_attempts             INTEGER      NOT NULL DEFAULT 0,
		inst1_last_error           TEXT,
		inst1_next_retry_at        TIMESTAMPTZ,
		inst1_claimed_at           TIMESTAMPTZ,
		installment_1_charged_at   TIMESTAMPTZ,
		inst1_stripe_invoice_id    VARCHAR(100),
		inst1_rail                 VARCHAR(10),

		installment_2_status       VARCHAR(30)  NOT NULL DEFAULT 'SCHEDULED',
		inst2_attempts             INTEGER      NOT NULL DEFAULT 0,
		inst2_last_error           TEXT,
		inst2_next_retry_at        TIMESTAMPTZ,
		inst2_claimed_at           TIMESTAMPTZ,
		installment_2_scheduled_for TIMESTAMPTZ NOT NULL,
		installment_2_charged_at   TIMESTAMPTZ,
		inst2_stripe_invoice_id    VARCHAR(100),
		inst2_rail                 VARCHAR(10),

		door_signed_at             TIMESTAMPTZ  NOT NULL,
		allow_synthetic_charge     BOOLEAN      NOT NULL DEFAULT FALSE,

		evidence_packet_status         VARCHAR(30) NOT NULL DEFAULT 'PENDING',
		evidence_packet_sha256         CHAR(64),
		evidence_packet_bytes          INTEGER,
		evidence_packet_url            TEXT,
		evidence_packet_stripe_file_id VARCHAR(100),

		is_clawed_back  BOOLEAN GENERATED ALWAYS AS (installment_2_status = 'VOIDED_CLAWBACK') STORED,

		created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

		CONSTRAINT fk_settlement_agreement_same_tenant
			FOREIGN KEY (client_id, pms_agreement_id)
			REFERENCES pms_agreements (client_id, pms_agreement_id),
		CONSTRAINT ck_settlement_split_sums CHECK (
			installment_1_cents + installment_2_cents = total_bounty_cents
		),
		CONSTRAINT ck_settlement_inst1_status CHECK (installment_1_status IN (
			'PENDING', 'CHARGING', 'SETTLING', 'CHARGED', 'FAILED', 'FAILED_PERMANENT',
			'UNCERTAIN', 'BLOCKED', 'VOIDED'
		)),
		CONSTRAINT ck_settlement_inst2_status CHECK (installment_2_status IN (
			'SCHEDULED', 'CHARGING', 'SETTLING', 'CHARGED', 'FAILED', 'FAILED_PERMANENT',
			'UNCERTAIN', 'BLOCKED', 'VOIDED_CLAWBACK', 'VOIDED'
		)),
		CONSTRAINT ck_settlement_inst1_charged_at CHECK (
			(installment_1_status = 'CHARGED') = (installment_1_charged_at IS NOT NULL)
		),
		CONSTRAINT ck_settlement_inst2_charged_at CHECK (
			(installment_2_status = 'CHARGED') = (installment_2_charged_at IS NOT NULL)
		),
		-- Fail-closed evidence-packet gate: a charge (SETTLING or CHARGED —
		-- the states meaning Stripe was actually asked to move money) cannot
		-- be recorded without a published, linked packet. Deliberately does
		-- NOT cover CHARGING (the claim/attempt bookkeeping state entered
		-- before charge.py has had a chance to publish the packet) — see the
		-- matching comment on the trigger below. Reinforced by the trigger,
		-- which additionally rejects the transition (not just the resulting
		-- state).
		CONSTRAINT ck_settlement_evidence_packet_required CHECK (
			installment_1_status NOT IN ('SETTLING', 'CHARGED')
			AND installment_2_status NOT IN ('SETTLING', 'CHARGED')
			OR evidence_packet_url IS NOT NULL
		),
		-- Exactly-once billing (see module docstring).
		CONSTRAINT uq_settlement_opportunity UNIQUE (client_id, opportunity_id),
		CONSTRAINT uq_settlement_agreement UNIQUE (client_id, pms_agreement_id)
	)
	""",
	# PR #37 review finding #2: an exactly-once-alert guard for the terminal
	# FAILED_PERMANENT state, mirroring the existing inst{N}_blocked_reason
	# columns' role for the transient-BLOCKED alert. A compare-and-swap
	# UPDATE ... WHERE {prefix}_alerted_permanent_at IS NULL in
	# ledger.mark_installment_failed() is what makes "exactly one incident"
	# provable even under a concurrent/duplicate call, and
	# ledger.reopen_failed_permanent_installment() clears it back to NULL so
	# a LATER FAILED_PERMANENT (after a manual reopen and a fresh 3-attempt
	# budget) can alert again.
	# Rollback: additive, nullable columns — safe to leave on the live
	# server; undo only with a manual
	# `ALTER TABLE settlement_transactions DROP COLUMN inst1_alerted_permanent_at, DROP COLUMN inst2_alerted_permanent_at`
	# if ever needed (no down-migration exists in this repo).
	"ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst1_alerted_permanent_at TIMESTAMPTZ",
	"ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst2_alerted_permanent_at TIMESTAMPTZ",
	"CREATE INDEX IF NOT EXISTS ix_settlement_client ON settlement_transactions(client_id)",
	"CREATE INDEX IF NOT EXISTS ix_settlement_inst1_claim ON settlement_transactions(installment_1_status, inst1_next_retry_at)",
	"CREATE INDEX IF NOT EXISTS ix_settlement_inst2_claim ON settlement_transactions(installment_2_status, installment_2_scheduled_for)",
	"CREATE INDEX IF NOT EXISTS ix_settlement_agreement ON settlement_transactions(pms_agreement_id)",
	# ── BLOCKED reason codes (PR #30 review findings 1 & 2) ──────────────────
	# BLOCKED used to be excluded from both claim queries unconditionally, so
	# any installment blocked by a transient cause (an Evidence Packet upload
	# failure, a day-60 PMS outage) was lost forever — the wired StubPmsProvider
	# always returns None, so this was the fate of EVERY installment 2 row.
	# These columns let ledger.claim_installment_1/2 distinguish a *retryable*
	# BLOCKED (EVIDENCE_PACKET_UNPUBLISHED, PMS_VERIFICATION_UNAVAILABLE) from a
	# structurally parked one (SYNTHETIC_AGREEMENT_IN_LIVE_MODE must never be
	# reclaimed — trg_settlement_guard_transition rejects that charge outright,
	# so reclaiming it would only make the claim UPDATE itself raise and take
	# the sweep's per-row savepoint with it). No CHECK on the value set
	# deliberately — a new reason code should not require a migration edit.
	"ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst1_blocked_reason VARCHAR(40)",
	"ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst2_blocked_reason VARCHAR(40)",
	"""
	CREATE INDEX IF NOT EXISTS ix_settlement_inst1_blocked_retry
		ON settlement_transactions(installment_1_status, inst1_blocked_reason, inst1_next_retry_at)
	""",
	"""
	CREATE INDEX IF NOT EXISTS ix_settlement_inst2_blocked_retry
		ON settlement_transactions(installment_2_status, inst2_blocked_reason, inst2_next_retry_at)
	""",
	"""
	CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_inst1_invoice
		ON settlement_transactions(inst1_stripe_invoice_id) WHERE inst1_stripe_invoice_id IS NOT NULL
	""",
	"""
	CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_inst2_invoice
		ON settlement_transactions(inst2_stripe_invoice_id) WHERE inst2_stripe_invoice_id IS NOT NULL
	""",
	# Self-correcting upgrade for an already-applied instance of this
	# migration whose ck_settlement_evidence_packet_required CHECK still
	# covers CHARGING (found by live-DB testing to make the claim step
	# itself unreachable — see the CHECK's own comment above). CREATE TABLE
	# IF NOT EXISTS doesn't touch an existing table's constraints, so this
	# re-run always drops and re-adds it with the corrected predicate —
	# harmless once already correct, same pattern as apply_appointment_ops.py.
	"ALTER TABLE settlement_transactions DROP CONSTRAINT IF EXISTS ck_settlement_evidence_packet_required",
	"""
	ALTER TABLE settlement_transactions ADD CONSTRAINT ck_settlement_evidence_packet_required CHECK (
		installment_1_status NOT IN ('SETTLING', 'CHARGED')
		AND installment_2_status NOT IN ('SETTLING', 'CHARGED')
		OR evidence_packet_url IS NOT NULL
	)
	""",
	# ── Transition guard — mirrors appointments_guard_transition() ──
	"""
	CREATE OR REPLACE FUNCTION settlement_guard_transition() RETURNS TRIGGER AS $$
	DECLARE
		v_agreement RECORD;
		v_clawback_window_days INTEGER;
	BEGIN
		IF TG_OP = 'INSERT' THEN
			SELECT status, door_signed_at, terminated_at INTO v_agreement
			FROM pms_agreements WHERE pms_agreement_id = NEW.pms_agreement_id;

			IF NOT FOUND THEN
				RAISE EXCEPTION 'pms_agreement % does not exist', NEW.pms_agreement_id;
			END IF;
			IF v_agreement.status != 'ACTIVE' THEN
				RAISE EXCEPTION 'pms_agreement % is not ACTIVE (status=%) — cannot open a settlement against it',
					NEW.pms_agreement_id, v_agreement.status;
			END IF;
			IF v_agreement.door_signed_at IS DISTINCT FROM NEW.door_signed_at THEN
				RAISE EXCEPTION 'door_signed_at (%) does not match pms_agreement % own door_signed_at (%) — no fabricated day-0',
					NEW.door_signed_at, NEW.pms_agreement_id, v_agreement.door_signed_at;
			END IF;
		END IF;

		IF TG_OP = 'UPDATE' THEN
			IF NEW.client_id IS DISTINCT FROM OLD.client_id
				OR NEW.opportunity_id IS DISTINCT FROM OLD.opportunity_id
				OR NEW.pms_agreement_id IS DISTINCT FROM OLD.pms_agreement_id
				OR NEW.door_signed_at IS DISTINCT FROM OLD.door_signed_at THEN
				RAISE EXCEPTION 'client_id/opportunity_id/pms_agreement_id/door_signed_at are immutable once set (transaction %)', OLD.transaction_id;
			END IF;
			IF NEW.total_bounty_cents IS DISTINCT FROM OLD.total_bounty_cents
				OR NEW.installment_1_cents IS DISTINCT FROM OLD.installment_1_cents
				OR NEW.installment_2_cents IS DISTINCT FROM OLD.installment_2_cents THEN
				RAISE EXCEPTION 'the cents split is immutable once set (transaction %)', OLD.transaction_id;
			END IF;
			IF OLD.installment_1_status = 'CHARGED' AND NEW.installment_1_status != 'CHARGED' THEN
				RAISE EXCEPTION 'installment_1_status cannot leave CHARGED (transaction %) — a refund is a new Stripe object, not a status rewrite', OLD.transaction_id;
			END IF;
			IF OLD.installment_2_status = 'CHARGED' AND NEW.installment_2_status != 'CHARGED' THEN
				RAISE EXCEPTION 'installment_2_status cannot leave CHARGED (transaction %) — a refund is a new Stripe object, not a status rewrite', OLD.transaction_id;
			END IF;

			SELECT status, door_signed_at, terminated_at INTO v_agreement
			FROM pms_agreements WHERE pms_agreement_id = NEW.pms_agreement_id;
			SELECT clawback_window_days INTO v_clawback_window_days
			FROM settlement_offer_config WHERE offer_code = NEW.offer_code;

			-- Fail-closed: no charge without a published, linked evidence packet.
			-- Deliberately checked only on entry into SETTLING/CHARGED — the
			-- ACTUAL money-moving states — not on entry into CHARGING. CHARGING
			-- is the claim/attempt bookkeeping state that ledger.claim_installment_1/2
			-- sets BEFORE charge.py has had a chance to compile and publish the
			-- packet; gating CHARGING here would make the claim step itself
			-- permanently unreachable (a real bug caught by
			-- tests/test_settlement_live.py::test_concurrent_claim_installment_2_claims_disjoint_sets
			-- during live-DB verification). charge.py's own preflight refusal
			-- still blocks BEFORE any Stripe call if the packet never publishes;
			-- this trigger is the backstop for the ACTUAL charge, not the attempt.
			IF (NEW.installment_1_status IN ('SETTLING', 'CHARGED')
				AND OLD.installment_1_status NOT IN ('SETTLING', 'CHARGED'))
				OR (NEW.installment_2_status IN ('SETTLING', 'CHARGED')
				AND OLD.installment_2_status NOT IN ('SETTLING', 'CHARGED')) THEN
				IF NEW.evidence_packet_url IS NULL THEN
					RAISE EXCEPTION 'cannot charge transaction % without a published evidence_packet_url', NEW.transaction_id;
				END IF;
				-- Fail-closed: no production charge against a synthetic agreement.
				IF NOT NEW.allow_synthetic_charge THEN
					PERFORM 1 FROM pms_agreements
						WHERE pms_agreement_id = NEW.pms_agreement_id AND agreement_source = 'PMS_SYNC';
					IF NOT FOUND THEN
						RAISE EXCEPTION 'transaction % backs a non-PMS_SYNC agreement and allow_synthetic_charge is not set — refusing to charge outside Stripe test mode', NEW.transaction_id;
					END IF;
				END IF;
			END IF;

			-- installment 1 requires the agreement still be ACTIVE.
			IF NEW.installment_1_status IN ('CHARGING', 'SETTLING', 'CHARGED')
				AND OLD.installment_1_status NOT IN ('CHARGING', 'SETTLING', 'CHARGED')
				AND v_agreement.status != 'ACTIVE' THEN
				RAISE EXCEPTION 'cannot charge installment 1 for transaction % — pms_agreement % is not ACTIVE (status=%)',
					NEW.transaction_id, NEW.pms_agreement_id, v_agreement.status;
			END IF;

			-- installment 2 requires the agreement not to have terminated inside
			-- the clawback window (the clawback rule enforced by Postgres, not
			-- only by src/services/settlement/clawback.py).
			IF NEW.installment_2_status IN ('CHARGING', 'SETTLING', 'CHARGED')
				AND OLD.installment_2_status NOT IN ('CHARGING', 'SETTLING', 'CHARGED')
				AND v_agreement.terminated_at IS NOT NULL
				AND v_clawback_window_days IS NOT NULL
				AND v_agreement.terminated_at < v_agreement.door_signed_at + (v_clawback_window_days || ' days')::INTERVAL THEN
				RAISE EXCEPTION 'cannot charge installment 2 for transaction % — pms_agreement % terminated inside the clawback window',
					NEW.transaction_id, NEW.pms_agreement_id;
			END IF;
		END IF;

		RETURN NEW;
	END;
	$$ LANGUAGE plpgsql;
	""",
	"DROP TRIGGER IF EXISTS trg_settlement_guard_transition ON settlement_transactions",
	"""
	CREATE TRIGGER trg_settlement_guard_transition
		BEFORE INSERT OR UPDATE ON settlement_transactions
		FOR EACH ROW EXECUTE FUNCTION settlement_guard_transition();
	""",
	# Financial ledger — no DELETE for either runtime role.
	"REVOKE DELETE ON settlement_transactions FROM blackink_app",
	"REVOKE DELETE ON settlement_transactions FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON settlement_transactions TO blackink_app",
	"GRANT USAGE ON SEQUENCE settlement_transactions_transaction_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON settlement_transactions TO blackink_system",
	"GRANT USAGE ON SEQUENCE settlement_transactions_transaction_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_settlement_ledger: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
