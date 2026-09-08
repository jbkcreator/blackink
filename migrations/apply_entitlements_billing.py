"""
Provision the entitlement registry and billing-rules ledger (Subtask 1.2.3 —
Six Billing Rules Implementation).

Blueprint source: Tasks/Project_Blackink_-_Complete_Implementation_Blueprint__
Full__v2.md lines 1052-1073 (`entitlement_offers` / `client_active_entitlements`),
docs/Week2_Tasks_Dev_Split_v1.md §Subtask 1.2.3, docs/Sept04_New_Items_Triage.md
(items 3, 4, 8, 9, 10, and the retired "No List Price"/"Full County Exclusivity"
items), docs/client_responses.md §6b.

The Description of the spec is explicit: "All rules are stored as
entitlement_offers rows and billing-job conditions — no pricing logic compiled
into application branches." None of that substrate existed before this
migration — verified by exhaustive grep of every prior migration,
src/core/models.py and config/tenant_policies.py: `entitlement_offers` is named
only in prose/comments as an aspirational precedent
(apply_payment_auth_capture.py, apply_settlement_ledger.py, models.py:1099),
and tests/test_payment_auth.py:208 actively asserts
`"entitlement" not in sql_text`. `monthly_cap`, `companies.founding`, any
invoice/credit table, and any per-client entitlement row were all zero
matches.

── Deliberate adaptation of the printed DDL to this repository's real schema ──
Same conflation Subtask 1.1.1 already resolved for appointments — the
blueprint's `client_active_entitlements.client_id UUID REFERENCES
companies(company_id)` is not representable here:
  * companies is the prospected PM-firm pool, owning_client_id reassigned
    every 30 days by county_allocation_reassessment.py — not the paying tenant.
  * clients(client_id VARCHAR(40)) is the paying tenant and the RLS boundary.
So client_entitlements.client_id REFERENCES clients(client_id), not companies.
The "founding" flag (Sept-04 triage item 9) is therefore added to clients, not
companies as the spec's Description literally says ("`companies` table
(client rows)") — the spec's own parenthetical ("client rows") is what
companies is NOT in this schema; clients is.

`offer_code`, not `offer_id`, to match the two config tables that already
shipped this pattern (payment_auth_offer_config, settlement_offer_config).

`stripe_price_id` is nullable — the blueprint types it NOT NULL, but no Stripe
Price object has been created for any SKU (the archived source-of-truth
references price_founding_hillsborough_base / price_founding_sit_meter, never
created), and a $0 first-sit row would need a fabricated one. This migration
seeds real dollar rows for the eventual buildout of real Stripe Subscriptions;
that buildout is a separate, later ticket — every charge this subtask verifies
is a one-off Stripe Invoice through the existing StripeGateway ABC
(src/services/settlement/gateway.py), same as the settlement engine.

`eligibility_predicate` / `trigger_condition` are carried for schema fidelity
but are DOCUMENTATION-ONLY: nothing in this codebase ever text()s the stored
predicate string. Storing and evaluating operator-authored SQL is a
code-execution surface this subtask does not open.

`per_sit_cents` is new (not in the printed DDL) — Owner Growth and Full County
are two-part tariffs ($749/mo + $99/sit, $1,197/mo + $99/sit per
client_responses.md line 18-19); a single price_cents column cannot express a
subscription-plus-metered offer.

── Pricing — Sept-04 is the newest, explicitly-final layer ──
client_responses.md line 23: "Every row prints 'Founding rate — normally
higher' on the offer sheet... Retire everything not listed." Seeded here:
respond ($397/mo flat, NOT the blueprint's $249), owner_growth ($749/mo +
$99/sit), full_county ($1,197/mo + $99/sit), owner_growth_flat_897 ($897/mo,
is_default=FALSE per "stays as a row, not default"). Retired rows ($297
Respond, Scorecard $449, county_additional) are NOT seeded. appt_first is
seeded at price_cents=0 (billing_model FREE) rather than the blueprint's $49 —
rule 2 ("first sit free... charges $0") and rule 5 (monthly_cap NULL on
appt_first) are only simultaneously satisfiable if appt_first IS the $0 row,
not a $49 row overridden by a side flag. appt_standard stays $99 flat
(blueprint 1085, unchanged by Sept-04). Seeds use
`INSERT ... ON CONFLICT (offer_code) DO NOTHING` — never overwrite an
operator-edited live price.

── Proof ledger ──
Named seven times across the Sept-04 docs, defined zero times, backed by no
table anywhere in this repo or its history (verified by full-history grep).
This subtask writes miss-credit and dispute-credit facts through
src/services/events.py's log_event() — the blueprint's own designated
"shared ledger of record" (blueprint line 96) and the single existing
audit-trail write path — rather than inventing a fifth, undefined ledger
alongside settlement_ledger / the consent ledger / the cost ledger / the
referral-credit ledger. Flagged as a client question, not asserted as fact.

── Six billable-credit tables/columns, one per rule ──
  1. $50 miss credit       -> inbound_messages.acked_at/channel/
                              ack_latency_seconds/miss_credit_issued_at
                              (this migration) + billing_credits row.
  2. First sit free        -> client_entitlements.first_sit_consumed.
  3. 60-day guarantee      -> client_entitlements.guarantee_applied/
                              guarantee_checked_at + subscription_overrides row.
  4. Dispute credit         -> appointment_disputes (already has flagged_at /
                              outcome DEFAULT CREDITED_AUTOMATIC, from
                              apply_appointment_ops.py) + billing_credits row.
  5. No monthly ceiling     -> entitlement_offers.monthly_cap left NULL on
                              appt_standard / appt_first; no code anywhere
                              reads it as a COUNT(*) >= cap gate.
  6. founding flag          -> clients.founding (this migration).

`billing_credits.UNIQUE (client_id, credit_type, source_table, source_id)` is
what makes "no second credit at resolved_at" (rule 4) and "one credit per
miss" (rule 1) structural, not conventional — mirrors settlement_transactions'
own UNIQUE(client_id, opportunity_id) reasoning: the accepted failure
direction is under-crediting, never double-crediting.
`subscription_overrides.UNIQUE (client_id, billing_period)` makes "one-time
per account" structural even against a re-run that finds guarantee_applied
somehow reset.

── acked_at is a NEW clock, deliberately distinct from claimed_at ──
inbound_messages.claimed_at already exists (apply_inbound_messages_sla.py) and
is the 30-minute-SLA / human-claims-the-Slack-card clock. Rule 1's "not
acknowledged inside 60 seconds" cannot mean the same event — the commit that
introduced these six rules is itself named "30-min SLA sweep", so both a
60-second AUTOMATED first-response ack and a 30-minute HUMAN answer must be
true at once. acked_at is that automated-ack timestamp, written by whatever
sends the first auto-response (not built in this migration — a code change,
not a schema one). channel defaults 'EMAIL' (the only channel that exists
today) with a CHECK allowing the sibling values already used elsewhere in this
schema (SMS/FORM/CALL) for forward compatibility.
ack_latency_seconds is a STORED generated column — the spec names this exact
field, so it is queryable rather than an expression buried in a WHERE clause.

Idempotent: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS. Run AFTER
apply_appointment_ops.py and apply_inbound_messages_sla.py, BEFORE
apply_rls_policies.py (which must be RE-RUN after this migration — the three
new tenant-bearing tables are registered in config/tenant_policies.py).

── Rollback path (no down-migrations exist in this repo — stated plainly
   per the blackink-review skill's own checklist) ──
Entirely additive: 3 new tables, 6 new nullable/defaulted columns on
existing tables, 2 new CHECK constraints on new/default-backed columns, no
column renamed or dropped, no existing row's meaning changed. Safe to leave
applied indefinitely even if this subtask is abandoned. To fully undo
against a live server (not needed for a rollback of THIS subtask alone —
only if removing the feature entirely):
    DROP TABLE IF EXISTS subscription_overrides, billing_credits, client_entitlements, entitlement_offers;
    ALTER TABLE inbound_messages DROP COLUMN IF EXISTS acked_at, DROP COLUMN IF EXISTS channel,
        DROP COLUMN IF EXISTS miss_credit_issued_at, DROP COLUMN IF EXISTS ack_latency_seconds;
    ALTER TABLE clients DROP COLUMN IF EXISTS founding;
    ALTER TABLE appointments DROP COLUMN IF EXISTS billed_offer_code, DROP COLUMN IF EXISTS billed_amount_cents;
    ALTER TABLE appointment_disputes DROP COLUMN IF EXISTS credit_status;
No script for this exists (or should exist) — an operator considering it
must first confirm no billing_credits/subscription_overrides row is relied
on for a real, already-issued client credit, since dropping those tables
destroys that history permanently.

    PYTHONPATH=. python migrations/apply_entitlements_billing.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # ── Global reference config — never a hardcoded offer branch. Same
    #    class as settlement_offer_config / payment_auth_offer_config.
    #    Deliberately NOT registered in config/tenant_policies.py.
    """
    CREATE TABLE IF NOT EXISTS entitlement_offers (
        offer_code             VARCHAR(50)  PRIMARY KEY,
        display_name            VARCHAR(200) NOT NULL,
        price_cents             BIGINT       NOT NULL CHECK (price_cents >= 0),
        per_sit_cents            BIGINT       CHECK (per_sit_cents IS NULL OR per_sit_cents >= 0),
        billing_model            VARCHAR(24)  NOT NULL,
        stripe_price_id          VARCHAR(100),
        trigger_condition        VARCHAR(100),
        eligibility_predicate    TEXT,
        cooldown_days            INTEGER      NOT NULL DEFAULT 30,
        monthly_cap              INTEGER,
        is_default               BOOLEAN      NOT NULL DEFAULT TRUE,
        is_enabled               BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_entitlement_offers_billing_model CHECK (billing_model IN (
            'FREE', 'SUBSCRIPTION_MONTHLY', 'METERED_EVENT', 'UPFRONT_PACK', 'REVENUE_SHARE'
        ))
    )
    """,
    "GRANT SELECT ON entitlement_offers TO blackink_app",
    # blackink_system needs UPDATE too — apply_rate_migration() (rule 6, the
    # founding-flag rate-migration entry point) runs under
    # get_system_db_context() and writes entitlement_offers.price_cents.
    "GRANT SELECT, UPDATE ON entitlement_offers TO blackink_system",
    # Sept-04 final SKUs. ON CONFLICT DO NOTHING — never clobber an
    # operator-edited live price on a re-run.
    """
    INSERT INTO entitlement_offers
        (offer_code, display_name, price_cents, per_sit_cents, billing_model, monthly_cap, is_default)
    VALUES
        ('respond', 'Respond (Founding)', 39700, NULL, 'SUBSCRIPTION_MONTHLY', NULL, TRUE),
        ('owner_growth', 'Owner Growth (Founding)', 74900, 9900, 'SUBSCRIPTION_MONTHLY', NULL, TRUE),
        ('full_county', 'Full County (Founding)', 119700, 9900, 'SUBSCRIPTION_MONTHLY', NULL, TRUE),
        ('owner_growth_flat_897', 'Owner Growth (Flat, non-default)', 89700, NULL, 'SUBSCRIPTION_MONTHLY', NULL, FALSE),
        ('appt_first', 'First Attended Meeting (Free)', 0, NULL, 'FREE', NULL, TRUE),
        ('appt_standard', 'Attended Appointment (flat)', 9900, NULL, 'METERED_EVENT', NULL, TRUE)
    ON CONFLICT (offer_code) DO NOTHING
    """,
    # ── client_active_entitlements, adapted: client_id -> clients, not companies ──
    """
    CREATE TABLE IF NOT EXISTS client_entitlements (
        entitlement_id       BIGSERIAL    PRIMARY KEY,
        client_id             VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        offer_code            VARCHAR(50)  NOT NULL REFERENCES entitlement_offers(offer_code),
        status                VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED', 'EXPIRED')),
        activated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        expires_at            TIMESTAMPTZ,
        first_sit_consumed    BOOLEAN      NOT NULL DEFAULT FALSE,
        guarantee_applied     BOOLEAN      NOT NULL DEFAULT FALSE,
        guarantee_checked_at  TIMESTAMPTZ,
        -- Rule 6 ("founding never moves") requires a PER-ACCOUNT effective
        -- price, not just entitlement_offers.price_cents (a single shared
        -- row every client reads) — a rate migration cannot leave a founding
        -- account's price unchanged if there is nowhere to record what that
        -- account's price actually is. Snapshotted at entitlement-creation
        -- time (src/services/billing/offers.py::create_client_entitlement)
        -- from entitlement_offers.price_cents; apply_rate_migration() only
        -- ever updates THIS column for non-founding active entitlements —
        -- a founding row's locked_price_cents is never touched again.
        locked_price_cents    BIGINT,
        created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_client_entitlements_active_offer
        ON client_entitlements(client_id, offer_code) WHERE status = 'ACTIVE'
    """,
    "CREATE INDEX IF NOT EXISTS ix_client_entitlements_client ON client_entitlements(client_id)",
    # Self-correcting upgrade: CREATE TABLE IF NOT EXISTS above is a no-op
    # against an already-applied instance of this migration, so a column
    # added to that CREATE TABLE clause after the fact never actually lands
    # without an explicit ALTER — same self-correcting-upgrade pattern as
    # apply_appointment_ops.py. Harmless once already applied.
    "ALTER TABLE client_entitlements ADD COLUMN IF NOT EXISTS locked_price_cents BIGINT",
    "GRANT SELECT, INSERT, UPDATE ON client_entitlements TO blackink_app",
    "GRANT USAGE ON SEQUENCE client_entitlements_entitlement_id_seq TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON client_entitlements TO blackink_system",
    "GRANT USAGE ON SEQUENCE client_entitlements_entitlement_id_seq TO blackink_system",
    # ── Credit ledger — rules 1 and 4 write here. ──
    """
    CREATE TABLE IF NOT EXISTS billing_credits (
        credit_id                BIGSERIAL    PRIMARY KEY,
        client_id                 VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        credit_type                VARCHAR(30)  NOT NULL CHECK (credit_type IN ('MISS_CREDIT', 'DISPUTE_CREDIT')),
        amount_cents               BIGINT       NOT NULL CHECK (amount_cents > 0),
        source_table               VARCHAR(40)  NOT NULL,
        source_id                  TEXT         NOT NULL,
        issued_at                  TIMESTAMPTZ  NOT NULL,
        billing_period             DATE         NOT NULL,
        status                     VARCHAR(20)  NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPLIED', 'VOIDED')),
        stripe_invoice_item_id     VARCHAR(100),
        applied_at                 TIMESTAMPTZ,
        created_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        -- Structural (not conventional) guarantee against a double credit for
        -- the same underlying miss/dispute — see module docstring.
        CONSTRAINT uq_billing_credits_source UNIQUE (client_id, credit_type, source_table, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_billing_credits_client_period ON billing_credits(client_id, billing_period)",
    "CREATE INDEX IF NOT EXISTS ix_billing_credits_pending ON billing_credits(status) WHERE status = 'PENDING'",
    # Financial ledger — no DELETE for either runtime role, same posture as settlement_transactions.
    "REVOKE DELETE ON billing_credits FROM blackink_app",
    "REVOKE DELETE ON billing_credits FROM blackink_system",
    "GRANT SELECT, INSERT, UPDATE ON billing_credits TO blackink_app",
    "GRANT USAGE ON SEQUENCE billing_credits_credit_id_seq TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON billing_credits TO blackink_system",
    "GRANT USAGE ON SEQUENCE billing_credits_credit_id_seq TO blackink_system",
    # ── Subscription override ledger — rule 3. ──
    """
    CREATE TABLE IF NOT EXISTS subscription_overrides (
        override_id             BIGSERIAL    PRIMARY KEY,
        client_id                 VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        billing_period            DATE         NOT NULL,
        override_price_cents      BIGINT       NOT NULL DEFAULT 0 CHECK (override_price_cents >= 0),
        reason                    VARCHAR(40)  NOT NULL DEFAULT 'SIXTY_DAY_GUARANTEE',
        created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        -- One override per month per account — "one-time per account" holds
        -- even if guarantee_applied were somehow reset on a re-run.
        CONSTRAINT uq_subscription_overrides_period UNIQUE (client_id, billing_period)
    )
    """,
    "GRANT SELECT, INSERT ON subscription_overrides TO blackink_app",
    "GRANT USAGE ON SEQUENCE subscription_overrides_override_id_seq TO blackink_app",
    "GRANT SELECT, INSERT ON subscription_overrides TO blackink_system",
    "GRANT USAGE ON SEQUENCE subscription_overrides_override_id_seq TO blackink_system",
    # ── Rule 1 columns on inbound_messages ──
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ",
    """
    ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS channel VARCHAR(10) NOT NULL DEFAULT 'EMAIL'
    """,
    "ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS ck_inbound_messages_channel",
    """
    ALTER TABLE inbound_messages ADD CONSTRAINT ck_inbound_messages_channel
        CHECK (channel IN ('EMAIL', 'SMS', 'FORM', 'CALL'))
    """,
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS miss_credit_issued_at TIMESTAMPTZ",
    # Postgres has no "ADD COLUMN IF NOT EXISTS ... GENERATED" guard that
    # tolerates re-running against an already-generated column of a different
    # expression, so this one is guarded explicitly via information_schema —
    # same self-correcting-upgrade pattern as apply_appointment_ops.py.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'inbound_messages' AND column_name = 'ack_latency_seconds'
        ) THEN
            ALTER TABLE inbound_messages ADD COLUMN ack_latency_seconds INTEGER
                GENERATED ALWAYS AS (
                    CASE WHEN acked_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (acked_at - received_at))::INTEGER
                        ELSE NULL
                    END
                ) STORED;
        END IF;
    END$$;
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_inbound_messages_miss_credit_claim
        ON inbound_messages(client_id, ack_latency_seconds)
        WHERE channel = 'EMAIL' AND intent IS NULL AND miss_credit_issued_at IS NULL
    """,
    # A message that is NEVER auto-acknowledged (acked_at stays NULL forever —
    # the worst-case SLA breach, not a merely-late one) has ack_latency_seconds
    # NULL too, so it can never satisfy "> 60" above and would otherwise be
    # invisible to the miss-credit sweep forever. This second partial index
    # supports the claim query's OR branch for that case
    # (src/services/billing/miss_credit.py — WHERE acked_at IS NULL AND
    # received_at is old enough).
    """
    CREATE INDEX IF NOT EXISTS ix_inbound_messages_miss_credit_unacked
        ON inbound_messages(client_id, received_at)
        WHERE channel = 'EMAIL' AND intent IS NULL AND miss_credit_issued_at IS NULL AND acked_at IS NULL
    """,
    # ── Rule 6 — founding flag on clients (the paying tenant / account row),
    #    NOT companies (the prospected PM-firm pool) — see module docstring. ──
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS founding BOOLEAN NOT NULL DEFAULT FALSE",
    # ── What an appointment was ACTUALLY billed as — read back by dispute
    #    credits (rule 4) so a disputed FREE first sit never manufactures a
    #    $99 credit. Recording the real charge rather than re-deriving it
    #    retroactively (first_sit_consumed may have flipped for an unrelated
    #    later appointment by the time a dispute is credited).
    """
    ALTER TABLE appointments ADD COLUMN IF NOT EXISTS billed_offer_code VARCHAR(50)
        REFERENCES entitlement_offers(offer_code)
    """,
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS billed_amount_cents BIGINT",
    # ── Dispute-credit sweep claim state — an EXPIRED dispute (flagged
    #    outside the 48h window) must stop being reselected every tick
    #    forever; a PENDING one is still eligible; CREDITED means the credit
    #    already landed (billing_credits' own UNIQUE is still the structural
    #    guarantee against a double credit — this column is purely a claim
    #    filter so the sweep doesn't re-attempt a dead row).
    """
    ALTER TABLE appointment_disputes ADD COLUMN IF NOT EXISTS credit_status VARCHAR(20)
        NOT NULL DEFAULT 'PENDING'
    """,
    "ALTER TABLE appointment_disputes DROP CONSTRAINT IF EXISTS ck_appointment_disputes_credit_status",
    """
    ALTER TABLE appointment_disputes ADD CONSTRAINT ck_appointment_disputes_credit_status
        CHECK (credit_status IN ('PENDING', 'CREDITED', 'EXPIRED'))
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_appointment_disputes_credit_pending
        ON appointment_disputes(flagged_at) WHERE credit_status = 'PENDING'
    """,
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_entitlements_billing: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
