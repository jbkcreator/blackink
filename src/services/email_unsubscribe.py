"""Stateless one-click email unsubscribe — mandatory on every cold/win-back
outbound send (CLAUDE.md's "Outbound email — mandatory one-click
unsubscribe" invariant, confirmed 2026-09-07). Ported from
ForcedAction-System/Forced-action-'s src/services/email_unsubscribe.py +
src/api/email_unsubscribe_router.py pattern (docs/adr/0028-cross-channel-
suppression-block-all.md in that repo), adapted to this repo's existing
PyJWT convention (src/services/auth.py, src/services/calendar_oauth.py)
rather than that repo's python-jose.

No DB row per token — a signed HS256 JWT is minted once at send time and
must keep verifying for as long as the recipient still has that email
(hence the long default expiry). client_id is embedded in the token so a
click can only ever suppress within the client it was minted for, matching
this repo's tenant-isolation posture — there is no cross-client suppression
side-channel here.

Shared by both the cold 5-touch sequence (src/services/sequence_orchestrator.py)
and the win-back sequence (src/services/winback_sequencer.py) — fixed once,
at the shared sending layer, not per-sequence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from config.settings import get_settings

_ALGORITHM = "HS256"
_TOKEN_TYPE = "email_unsubscribe"
_DEFAULT_EXPIRY = timedelta(days=365)


def _secret() -> str:
	settings = get_settings()
	secret = settings.email_unsubscribe_secret
	if not secret:
		raise RuntimeError(
			"Unsubscribe tokens need EMAIL_UNSUBSCRIBE_SECRET configured "
			"(config/settings.py's email_unsubscribe_secret)"
		)
	return secret.get_secret_value()


def mint_unsubscribe_token(client_id: str, email: str, expires_in: timedelta = _DEFAULT_EXPIRY) -> str:
	payload = {
		"client_id": client_id,
		"email": email.strip().lower(),
		"type": _TOKEN_TYPE,
		"exp": datetime.now(timezone.utc) + expires_in,
	}
	return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_unsubscribe_token(token: str) -> Optional[tuple[str, str]]:
	"""Returns (client_id, email) or None if invalid, expired, or wrong type."""
	try:
		payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
	except jwt.PyJWTError:
		return None
	if payload.get("type") != _TOKEN_TYPE:
		return None
	client_id = payload.get("client_id")
	email = payload.get("email")
	if not client_id or not email:
		return None
	return client_id, email


def unsubscribe_url(client_id: str, email: str) -> str:
	"""One-click unsubscribe link, landing on the public
	/api/v1/public/unsubscribe endpoint (src/api/unsubscribe_router.py)."""
	base = get_settings().app_base_url.rstrip("/")
	token = mint_unsubscribe_token(client_id, email)
	return f"{base}/api/v1/public/unsubscribe?token={token}"


def append_unsubscribe_footer(body: str, url: str) -> str:
	"""Appends a plain-text unsubscribe line to an outbound email body.

	Applied uniformly at dispatch time (sequence_orchestrator.dispatch_touch,
	winback_sequencer.dispatch_winback_touch) rather than baked into
	individual templates, so the footer can never drift between sequences
	or get forgotten by a new one."""
	return f"{body}\n\n---\nDon't want these emails? Unsubscribe: {url}"
