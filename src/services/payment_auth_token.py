"""Signed, expiring onboarding token — stands in for the authenticated
onboarding-portal session this repo doesn't have yet, same reasoning as
src/services/calendar_oauth.py's connect-link token: no client-portal
login/session system exists anywhere in this repo (no users/session
table), so a request handler cannot trust a bare client_id/company_id
supplied directly in a request body or query string — anyone who guesses
or observes another company's identifiers could otherwise drive that
company's payment-auth flow.

This token is deliberately temporary integration-testing scaffolding, not
a permanent auth system: the production path is the future authenticated
onboarding portal minting one of these (or, more likely, replacing this
mechanism outright with its own session) at the moment a client reaches
the payment-auth step of onboarding. mint_payment_auth_onboarding_token()
is only ever called from tests and the dev CLI
(scripts/dev_mint_payment_auth_token.py) in THIS codebase — never from a
production request path, since no such path exists yet to call it from.

Short expiry (default 30 minutes) — long enough to complete one onboarding
step, short enough that a leaked/logged token stops being useful quickly.
offer_code is bound into the signed payload, not accepted separately from
the caller, so a request can never claim a different (possibly enabled)
offer_code than the one the token was actually minted for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from config.settings import get_settings

DEFAULT_TOKEN_TTL_MINUTES = 30


class PaymentAuthTokenError(RuntimeError):
	"""Invalid, expired, or malformed onboarding token."""


@dataclass(frozen=True)
class PaymentAuthTokenClaims:
	client_id: str
	company_id: str
	offer_code: str


def _secret() -> str:
	settings = get_settings()
	secret = settings.payment_auth_onboarding_token_secret
	if not secret:
		raise PaymentAuthTokenError("PAYMENT_AUTH_ONBOARDING_TOKEN_SECRET is not configured")
	return secret.get_secret_value()


def mint_payment_auth_onboarding_token(
	*, client_id: str, company_id: str, offer_code: str, expires_minutes: int = DEFAULT_TOKEN_TTL_MINUTES
) -> str:
	payload = {
		"client_id": client_id,
		"company_id": company_id,
		"offer_code": offer_code,
		"exp": datetime.now(timezone.utc) + timedelta(minutes=expires_minutes),
		"iat": datetime.now(timezone.utc),
	}
	return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_payment_auth_onboarding_token(token: str) -> PaymentAuthTokenClaims:
	try:
		payload = jwt.decode(token, _secret(), algorithms=["HS256"])
	except jwt.PyJWTError as exc:
		raise PaymentAuthTokenError("Invalid or expired onboarding token") from exc
	try:
		return PaymentAuthTokenClaims(
			client_id=payload["client_id"], company_id=payload["company_id"], offer_code=payload["offer_code"],
		)
	except KeyError as exc:
		raise PaymentAuthTokenError("Onboarding token is missing a required claim") from exc
