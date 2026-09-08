"""Dev-only CLI to mint a payment-auth onboarding token — stands in for
the authenticated onboarding portal's payment-auth step (a separate, not
built here subsystem), which is what mints this token in production.
NEVER the production trigger — see src/services/payment_auth_token.py's
module docstring.

Usage:
    PYTHONPATH=. python scripts/dev_mint_payment_auth_token.py <client_id> <company_id> <offer_code>

Prints the full modal URL (test-harness page — see
src/api/payment_auth_router.py's payment_auth_test_harness_page) and the
raw token, for exercising the setup-intents/confirm/status endpoints
directly (e.g. with curl) during Stripe test-mode verification.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.settings import get_settings
from src.services.payment_auth_token import mint_payment_auth_onboarding_token


def main() -> int:
	if len(sys.argv) != 4:
		print("Usage: python scripts/dev_mint_payment_auth_token.py <client_id> <company_id> <offer_code>")
		return 1
	client_id, company_id, offer_code = sys.argv[1], sys.argv[2], sys.argv[3]

	token = mint_payment_auth_onboarding_token(client_id=client_id, company_id=company_id, offer_code=offer_code)
	settings = get_settings()
	url = f"{settings.app_base_url}/api/v1/onboarding/payment-auth/test-harness?token={token}"
	print(f"Onboarding token (expires in 30 minutes):\n\n{token}\n")
	print(f"Test-harness page (non-production, only served when ENVIRONMENT is not production):\n\n{url}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
