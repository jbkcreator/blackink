#!/bin/bash
# Runs every migrations/apply_*.py script in the exact dependency order
# documented in CLAUDE.md's "Common Commands" section. Safe to re-run —
# every script is idempotent (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF
# NOT EXISTS) — so this can be used both for a first deploy and to catch
# up a DB that only got partway through a previous run.
#
# Does NOT stop on the first failure — every script runs, every result is
# logged, and failures are summarized at the end. Stopping on first failure
# is what silently skipped apply_rls_policies.py last time (it ran after a
# script that failed, so it and everything after it never ran, and nobody
# noticed until the app crashed on a missing table).
#
# Usage (from the repo root, e.g. /root/blackink):
#   bash scripts/run_migrations.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$(pwd)"
VENV_PYTHON="./venv/bin/python3"

# A few migration scripts (e.g. apply_stl_cadence.py) read os.environ
# directly instead of going through config/settings.py, so they see
# nothing unless the shell itself has DATABASE_URL etc. exported —
# unlike the rest of the app, which loads .env on its own. Export .env
# here via python-dotenv (already a dependency) so every script works
# regardless of which pattern it follows. shlex-quoted, not `source .env`
# directly — a value containing quotes would otherwise get mangled.
if [ -f .env ]; then
  eval "$("$VENV_PYTHON" -c '
import shlex
from dotenv import dotenv_values
for k, v in dotenv_values(".env").items():
    if v is not None:
        print(f"export {k}={shlex.quote(v)}")
')"
fi

mkdir -p logs
LOG="logs/migrations_$(date +%Y%m%d_%H%M%S).log"

MIGRATIONS=(
  apply_db_roles
  apply_counties
  apply_raw_assessor_parcels
  apply_area_code_timezones
  apply_clients
  apply_clients_stl_fields
  apply_relay_halts
  apply_companies
  apply_contacts
  apply_pm_profiles
  apply_owner_entities
  apply_raw_prospect_pipeline
  apply_events
  apply_sandbox_dashboard_view
  apply_compliance_gate_audit
  apply_campaign_readiness_gate
  apply_sms_dispatch_log
  apply_sending_domains
  apply_mailbox_last_used
  apply_agent_work_orders
  apply_meeting_outcomes
  apply_contacts_prospect_objections
  apply_sequence_runs
  apply_sequence_touch_dispatches
  apply_companies_google_place_id
  apply_ghost_shopper_cleanup
  apply_owner_visibility_scores
  apply_ovs_audit_requests
  apply_admin_users
  apply_mailbox_smtp_credentials
  apply_owner_contacts
  apply_calendar_connections
  apply_bookings
  apply_booking_reminder_jobs
  apply_no_show_prompt_jobs
  apply_no_show_recovery_jobs
  apply_self_serve_audit_submissions
  apply_meeting_outcome_prompt_jobs
  apply_appointment_ops
  apply_payment_auth_capture
  apply_pms_agreements
  apply_settlement_ledger
  apply_settlement_reopen_count
  apply_inbound_messages
  apply_inbound_messages_sla
  apply_respond_routing_gaps
  apply_entitlements_billing
  apply_client_billing_account
  apply_ghost_shopper_reactivation
  apply_ghost_shopper_replies
  apply_ghost_form_submissions
  apply_ovs_data_coverage
  apply_inbound_messages_lead_fields
  apply_stl_cadence
  apply_winback_imports
  apply_winback_touch_sequence
  apply_winback_enrichment
  apply_rls_policies
  apply_akrash_grant
)

FAILED=()

for m in "${MIGRATIONS[@]}"; do
  script="migrations/${m}.py"
  if [ ! -f "$script" ]; then
    echo "== $m == SKIPPED (file not found — branch/version mismatch?)" | tee -a "$LOG"
    continue
  fi
  echo "== $m ==" | tee -a "$LOG"
  "$VENV_PYTHON" "$script" >>"$LOG" 2>&1
  code=$?
  if [ $code -ne 0 ]; then
    echo "   FAILED (exit=$code)" | tee -a "$LOG"
    FAILED+=("$m")
  else
    echo "   ok" | tee -a "$LOG"
  fi
done

echo ""
echo "================ MIGRATION RUN SUMMARY ================"
echo "Full log: $LOG"
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "All migrations completed successfully."
else
  echo "FAILED (${#FAILED[@]}):"
  printf '  %s\n' "${FAILED[@]}"
  echo ""
  echo "Fix the underlying issue, then re-run this script — already-applied"
  echo "migrations are idempotent and will no-op."
  exit 1
fi
echo "========================================================="
