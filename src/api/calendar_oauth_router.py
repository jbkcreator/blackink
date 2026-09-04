"""Calendar connect flow (Subtask 3.2.1). Google Calendar + Microsoft
Graph only — no Calendly (client comment W1-8). state/connect-link
verification stands in for the client-portal login system this repo
doesn't have (see src/services/calendar_oauth.py's module docstring).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_db_context
from src.core.token_crypto import encrypt_token
from src.services import calendar_oauth
from src.services.booking_ingest import sync_connection_locked
from src.services.calendar_oauth import OAuthStateError
from src.services.calendar_providers import (
	GoogleCalendarClient,
	MicrosoftGraphClient,
	expires_at_ms_to_datetime,
)

router = APIRouter(prefix="/api/v1/calendar", tags=["calendar-oauth"])
logger = logging.getLogger(__name__)


@dataclass
class _PendingConnection:
	"""Stands in for a calendar_connections row before one exists — the
	subscribe/watch call needs an access token via
	calendar_oauth.get_valid_access_token() before the row can be inserted
	(the row needs the subscription_id the watch call returns). Carries
	only what that call reads: token_expires_at is always fresh here, so
	the refresh branch (which would need a real connection_id) never runs."""

	connection_id: Optional[int]
	provider: str
	external_calendar_id: str
	access_token_encrypted: str
	token_expires_at: datetime


def _redirect_uri(provider: str) -> str:
	settings = get_settings()
	return f"{settings.calendar_webhook_base_url}/api/v1/calendar/oauth/callback/{provider.lower()}"


def _webhook_url(provider: str) -> str:
	settings = get_settings()
	return f"{settings.calendar_webhook_base_url}/api/v1/webhooks/booking/{provider.lower()}"


@router.get("/connect/{provider}")
def connect(provider: str, token: str = Query(...)):
	provider = provider.upper()
	try:
		# Pure decode first — no DB session yet, since we don't know which
		# client_id to scope one to until the JWT itself is decoded (it's
		# in the payload). See decode_connect_link()'s docstring.
		claims = calendar_oauth.decode_connect_link(token)
	except OAuthStateError as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc
	if claims.provider != provider:
		raise HTTPException(status_code=400, detail="Connect-link provider does not match route")

	with get_db_context(client_id=claims.client_id) as session:
		try:
			calendar_oauth.consume_connect_link_nonce(session, claims)
		except OAuthStateError as exc:
			raise HTTPException(status_code=400, detail=str(exc)) from exc
		auth_url = calendar_oauth.build_authorization_url(claims, _redirect_uri(provider))
	return RedirectResponse(auth_url)


@router.get("/oauth/callback/{provider}")
def oauth_callback(provider: str, code: str = Query(...), state: str = Query(...)):
	provider = provider.upper()
	if provider not in ("GOOGLE", "MICROSOFT"):
		raise HTTPException(status_code=404, detail="Unknown provider")

	try:
		claims = calendar_oauth.verify_state(state)
	except OAuthStateError as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc
	if claims.provider != provider:
		raise HTTPException(status_code=400, detail="State provider does not match route")

	tokens = calendar_oauth.exchange_code_for_tokens(provider, code, _redirect_uri(provider))
	if not tokens.refresh_token:
		raise HTTPException(status_code=502, detail="Provider did not return a refresh token (check offline-access/prompt=consent config)")

	# Captured BEFORE the subscription is registered — the initialization-
	# boundary fix in booking_ingest.sync_connection_locked() partitions
	# the baseline response against this instant, since a booking could be
	# created between this line and the subscribe call landing.
	connect_initiated_at = datetime.now(timezone.utc)

	channel_id = secrets.token_urlsafe(24)
	verification_secret = secrets.token_urlsafe(32)

	with get_db_context(client_id=claims.client_id) as session:
		if provider == "GOOGLE":
			external_calendar_id = "primary"
			existing_row = _PendingConnection(
				connection_id=None, provider="GOOGLE", external_calendar_id=external_calendar_id,
				access_token_encrypted=encrypt_token(tokens.access_token), token_expires_at=tokens.expires_at,
			)
			client = GoogleCalendarClient(session)
			subscription_id_value = channel_id
			subscription_expires_at = None
			if get_settings().skip_calendar_watch_registration:
				logger.warning(
					"SKIP_CALENDAR_WATCH_REGISTRATION is set — connecting client %s's Google calendar "
					"WITHOUT a live push subscription. Real OAuth tokens and baseline sync still run; "
					"new bookings will only be picked up by calendar_sync_worker's periodic safety sweep, "
					"not in near-real-time. Never leave this set outside local dev.",
					claims.client_id,
				)
			else:
				# register_watch needs a connection-shaped object with a valid
				# access token available via get_valid_access_token(); the row
				# doesn't exist yet, so we pass a lightweight stand-in exposing
				# just what that call needs. Google echoes channel_id back
				# verbatim as X-Goog-Channel-ID — it IS the subscription_id,
				# unlike Microsoft's server-issued one below.
				watch_result = client.register_watch(existing_row, channel_id, _webhook_url(provider), verification_secret)
				subscription_expires_at = expires_at_ms_to_datetime(watch_result.get("expires_at_ms"))
		else:
			me_calendar = requests.get(
				"https://graph.microsoft.com/v1.0/me/calendar",
				headers={"Authorization": f"Bearer {tokens.access_token}"},
				timeout=15,
			)
			me_calendar.raise_for_status()
			external_calendar_id = me_calendar.json()["id"]
			existing_row = _PendingConnection(
				connection_id=None, provider="MICROSOFT", external_calendar_id=external_calendar_id,
				access_token_encrypted=encrypt_token(tokens.access_token), token_expires_at=tokens.expires_at,
			)
			client = MicrosoftGraphClient(session)
			subscription_id_value = channel_id
			subscription_expires_at = None
			if get_settings().skip_calendar_watch_registration:
				logger.warning(
					"SKIP_CALENDAR_WATCH_REGISTRATION is set — connecting client %s's Microsoft calendar "
					"WITHOUT a live push subscription. Never leave this set outside local dev.",
					claims.client_id,
				)
			else:
				# Graph issues its own subscription id (body["id"]) — distinct
				# from the locally generated channel_id, and it's what Graph's
				# webhook payload carries back, so it must be what's persisted
				# as subscription_id or resolve_calendar_connection() can never
				# find this row (previously stored channel_id here — a bug).
				sub_result = client.register_subscription(existing_row, _webhook_url(provider), verification_secret)
				subscription_id_value = sub_result["subscription_id"]
				subscription_expires_at = sub_result.get("expires_at")

		row = session.execute(
			text(
				"""
				INSERT INTO calendar_connections
					(client_id, provider, external_calendar_id, subscription_id, verification_secret,
					 access_token_encrypted, refresh_token_encrypted, token_expires_at, expires_at, status)
				VALUES (:client_id, :provider, :external_calendar_id, :subscription_id, :verification_secret,
						:access_token_encrypted, :refresh_token_encrypted, :token_expires_at, :expires_at, 'ACTIVE')
				ON CONFLICT (client_id, provider) DO UPDATE SET
					external_calendar_id = EXCLUDED.external_calendar_id,
					subscription_id = EXCLUDED.subscription_id,
					verification_secret = EXCLUDED.verification_secret,
					access_token_encrypted = EXCLUDED.access_token_encrypted,
					refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
					token_expires_at = EXCLUDED.token_expires_at,
					expires_at = EXCLUDED.expires_at,
					status = 'ACTIVE',
					sync_token = NULL,
					initial_sync_done = FALSE,
					updated_at = NOW()
				RETURNING connection_id
				"""
			),
			{
				"client_id": claims.client_id,
				"provider": provider,
				"external_calendar_id": external_calendar_id,
				"subscription_id": subscription_id_value,
				"verification_secret": verification_secret,
				"access_token_encrypted": encrypt_token(tokens.access_token),
				"refresh_token_encrypted": encrypt_token(tokens.refresh_token),
				"token_expires_at": tokens.expires_at,
				"expires_at": subscription_expires_at,
			},
		).one()
		connection_id = row.connection_id

		provider_client = GoogleCalendarClient(session) if provider == "GOOGLE" else MicrosoftGraphClient(session)
		sync_connection_locked(session, connection_id, provider_client, connect_boundary=connect_initiated_at)

	return {"status": "connected", "provider": provider, "connection_id": connection_id}
