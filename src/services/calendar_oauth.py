"""OAuth connect flow for Subtask 3.2.1 — Google Calendar and Microsoft
Graph only (no Calendly; client comment W1-8, Blackink_Source_of_Truth.md
line 577). Genuinely greenfield: no OAuth, token-refresh, or per-row
credential-encryption code existed anywhere in this repo before this
module (confirmed by research prior to implementation) — this follows
the repo's existing minimalism (requests, not a heavy SDK) rather than
importing google-api-python-client/msal.

No client-portal login system exists anywhere in this repo either (no
users/session table, not assigned to any Week 1 subtask across any of
the four developers). Instead of building one from scratch, OAuth
`state` binds to a single-use signed connect-link token
(mint_calendar_connect_link) — the production path is this function
being called automatically by the (separate, not built here) onboarding
flow's calendar-connect step; a CLI/script entry point for local dev is
dev-only, never the production trigger.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.token_crypto import decrypt_token, encrypt_token

CONNECT_LINK_TTL_MINUTES = 30
STATE_TTL_MINUTES = 10

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

_MS_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_MS_SCOPE = "offline_access Calendars.Read"


class OAuthStateError(ValueError):
	"""Tampered, expired, or replayed connect-link / state token."""


def _jwt_secret() -> str:
	settings = get_settings()
	secret = settings.calendar_oauth_state_secret
	if not secret:
		raise RuntimeError("CALENDAR_OAUTH_STATE_SECRET is not configured")
	return secret.get_secret_value()


def mint_calendar_connect_link(session: Session, client_id: str, provider: str) -> str:
	"""Called automatically by the onboarding flow's calendar-connect step
	(production path) or a dev-only CLI script (local testing only — never
	the production trigger). Encodes client_id + a single-use nonce,
	recorded in oauth_connect_nonces so the link cannot be replayed once
	consumed."""
	nonce = secrets.token_urlsafe(32)
	expires_at = datetime.now(timezone.utc) + timedelta(minutes=CONNECT_LINK_TTL_MINUTES)
	session.execute(
		text(
			"INSERT INTO oauth_connect_nonces (nonce, client_id, provider, expires_at) "
			"VALUES (:nonce, :client_id, :provider, :expires_at)"
		),
		{"nonce": nonce, "client_id": client_id, "provider": provider, "expires_at": expires_at},
	)
	payload = {
		"client_id": client_id,
		"provider": provider,
		"nonce": nonce,
		"exp": expires_at,
		"iat": datetime.now(timezone.utc),
	}
	return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


@dataclass
class ConnectLinkClaims:
	client_id: str
	provider: str
	nonce: str


def verify_and_consume_connect_link(session: Session, token: str) -> ConnectLinkClaims:
	try:
		payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
	except jwt.PyJWTError as exc:
		raise OAuthStateError("Invalid or expired connect-link token") from exc

	row = session.execute(
		text(
			"UPDATE oauth_connect_nonces SET consumed_at = NOW() "
			"WHERE nonce = :nonce AND consumed_at IS NULL AND expires_at > NOW() "
			"RETURNING client_id, provider"
		),
		{"nonce": payload["nonce"]},
	).first()
	if row is None:
		raise OAuthStateError("Connect-link nonce already used, expired, or unknown")
	return ConnectLinkClaims(client_id=row.client_id, provider=row.provider, nonce=payload["nonce"])


def build_authorization_url(claims: ConnectLinkClaims, redirect_uri: str) -> str:
	"""`state` is a second, short-lived signed token binding this specific
	authorization attempt (client_id + nonce + provider) so the callback
	can verify it wasn't forged — distinct from the connect-link token,
	which is already consumed by this point."""
	state_payload = {
		"client_id": claims.client_id,
		"provider": claims.provider,
		"nonce": claims.nonce,
		"exp": datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
	}
	state = jwt.encode(state_payload, _jwt_secret(), algorithm="HS256")
	settings = get_settings()

	if claims.provider == "GOOGLE":
		if not settings.google_oauth_client_id:
			raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID is not configured")
		params = {
			"client_id": settings.google_oauth_client_id,
			"redirect_uri": redirect_uri,
			"response_type": "code",
			"scope": _GOOGLE_SCOPE,
			"access_type": "offline",
			"prompt": "consent",
			"state": state,
		}
		return _GOOGLE_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
	elif claims.provider == "MICROSOFT":
		if not settings.microsoft_oauth_client_id:
			raise RuntimeError("MICROSOFT_OAUTH_CLIENT_ID is not configured")
		params = {
			"client_id": settings.microsoft_oauth_client_id,
			"redirect_uri": redirect_uri,
			"response_type": "code",
			"scope": _MS_SCOPE,
			"state": state,
		}
		return _MS_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
	raise ValueError(f"Unknown provider: {claims.provider}")


def verify_state(token: str) -> ConnectLinkClaims:
	try:
		payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
	except jwt.PyJWTError as exc:
		raise OAuthStateError("Invalid or expired OAuth state") from exc
	return ConnectLinkClaims(client_id=payload["client_id"], provider=payload["provider"], nonce=payload["nonce"])


@dataclass
class TokenExchangeResult:
	access_token: str
	refresh_token: Optional[str]
	expires_at: datetime


def exchange_code_for_tokens(provider: str, code: str, redirect_uri: str) -> TokenExchangeResult:
	settings = get_settings()
	if provider == "GOOGLE":
		resp = requests.post(
			_GOOGLE_TOKEN_URL,
			data={
				"client_id": settings.google_oauth_client_id,
				"client_secret": settings.google_oauth_client_secret.get_secret_value(),
				"code": code,
				"redirect_uri": redirect_uri,
				"grant_type": "authorization_code",
			},
			timeout=15,
		)
	elif provider == "MICROSOFT":
		resp = requests.post(
			_MS_TOKEN_URL,
			data={
				"client_id": settings.microsoft_oauth_client_id,
				"client_secret": settings.microsoft_oauth_client_secret.get_secret_value(),
				"code": code,
				"redirect_uri": redirect_uri,
				"grant_type": "authorization_code",
			},
			timeout=15,
		)
	else:
		raise ValueError(f"Unknown provider: {provider}")

	resp.raise_for_status()
	body = resp.json()
	expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.get("expires_in", 3600))
	return TokenExchangeResult(
		access_token=body["access_token"],
		refresh_token=body.get("refresh_token"),
		expires_at=expires_at,
	)


def refresh_access_token(provider: str, refresh_token: str) -> TokenExchangeResult:
	settings = get_settings()
	if provider == "GOOGLE":
		resp = requests.post(
			_GOOGLE_TOKEN_URL,
			data={
				"client_id": settings.google_oauth_client_id,
				"client_secret": settings.google_oauth_client_secret.get_secret_value(),
				"refresh_token": refresh_token,
				"grant_type": "refresh_token",
			},
			timeout=15,
		)
	elif provider == "MICROSOFT":
		resp = requests.post(
			_MS_TOKEN_URL,
			data={
				"client_id": settings.microsoft_oauth_client_id,
				"client_secret": settings.microsoft_oauth_client_secret.get_secret_value(),
				"refresh_token": refresh_token,
				"grant_type": "refresh_token",
				"scope": _MS_SCOPE,
			},
			timeout=15,
		)
	else:
		raise ValueError(f"Unknown provider: {provider}")

	resp.raise_for_status()
	body = resp.json()
	expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.get("expires_in", 3600))
	return TokenExchangeResult(
		access_token=body["access_token"],
		refresh_token=body.get("refresh_token", refresh_token),  # providers don't always rotate it
		expires_at=expires_at,
	)


def get_valid_access_token(session: Session, connection_row) -> str:
	"""Decrypts the stored access token, transparently refreshing (and
	persisting the refreshed, re-encrypted tokens) if it's expired or
	close to expiring."""
	if connection_row.token_expires_at and connection_row.token_expires_at > datetime.now(timezone.utc) + timedelta(
		minutes=2
	):
		return decrypt_token(connection_row.access_token_encrypted)

	refresh_token = decrypt_token(connection_row.refresh_token_encrypted)
	result = refresh_access_token(connection_row.provider, refresh_token)
	session.execute(
		text(
			"UPDATE calendar_connections SET access_token_encrypted = :access, "
			"refresh_token_encrypted = :refresh, token_expires_at = :expires_at, updated_at = NOW() "
			"WHERE connection_id = :id"
		),
		{
			"access": encrypt_token(result.access_token),
			"refresh": encrypt_token(result.refresh_token),
			"expires_at": result.expires_at,
			"id": connection_row.connection_id,
		},
	)
	return result.access_token
