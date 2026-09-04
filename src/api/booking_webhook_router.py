"""Webhook notification routes (Subtask 3.2.1). Google/Microsoft require a
fast response — Microsoft's own guidance is to queue and return within
~3 seconds; Google's push channels back off and eventually deactivate on
slow/failing responses. Those two routes therefore do the minimum
synchronous work (handshake validation + enqueue) and never run a full
sync-and-dispatch pass inline — see src/services/booking_ingest.py and
src/tasks/calendar_sync_worker.py for where the real processing happens.
GoHighLevel's route processes inline instead (see its own docstring
below) since there's no external fetch step to defer.

A webhook notification carries only (provider, subscription_id), not a
client_id, so it can't open an RLS-scoped session to look itself up —
resolve_calendar_connection() (a narrow SECURITY DEFINER SQL function,
not the BYPASSRLS system role, which is never imported from src/api/)
resolves just enough identity to then open a properly client_id-scoped
session for the rest of the work.

Signature/handshake failure is HTTP 401 on every route, success HTTP 200
— the literal contract in Subtask 3.2.1's DoD.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from sqlalchemy import text

from src.core.database import get_db_context

router = APIRouter(prefix="/api/v1/webhooks/booking", tags=["booking-webhooks"])


def _resolve_connection(session, provider: str, subscription_id: str):
	return session.execute(
		text("SELECT * FROM resolve_calendar_connection(:provider, :subscription_id)"),
		{"provider": provider, "subscription_id": subscription_id},
	).first()


def _enqueue(client_id: str, connection_id: int) -> None:
	with get_db_context(client_id=client_id) as session:
		session.execute(
			text(
				"INSERT INTO calendar_sync_queue (connection_id, requested_at) VALUES (:cid, NOW()) "
				"ON CONFLICT (connection_id) DO UPDATE SET requested_at = NOW()"
			),
			{"cid": connection_id},
		)


def _run_sync_now(client_id: str, connection_id: int) -> None:
	"""The common-case fast path — runs after the HTTP response is already
	sent (FastAPI BackgroundTasks), giving near-immediate processing for
	the 5s/60s promises without holding the provider's webhook request
	open. calendar_sync_worker.py is the durability backstop for when this
	never runs (process restart, crash) or when no notification arrives at
	all (its periodic ACTIVE-connection sweep)."""
	from src.services.booking_ingest import sync_connection_locked
	from src.services.calendar_providers import GoogleCalendarClient, MicrosoftGraphClient
	from src.services.calendar_confirmation import attempt_immediate_confirmations

	with get_db_context(client_id=client_id) as session:
		row = session.execute(
			text("SELECT requested_at FROM calendar_sync_queue WHERE connection_id = :cid"),
			{"cid": connection_id},
		).first()
		if row is None:
			return
		captured_requested_at = row.requested_at

		provider_row = session.execute(
			text("SELECT provider FROM calendar_connections WHERE connection_id = :cid"), {"cid": connection_id}
		).one()
		client = (
			GoogleCalendarClient(session) if provider_row.provider == "GOOGLE" else MicrosoftGraphClient(session)
		)
		outcome = sync_connection_locked(session, connection_id, client)

		# Conditional delete — a notification arriving mid-sync bumps
		# requested_at, which makes this no longer match, leaving the row
		# for the next pass (see plan doc on the queue race fix).
		session.execute(
			text(
				"DELETE FROM calendar_sync_queue WHERE connection_id = :cid AND requested_at <= :captured"
			),
			{"cid": connection_id, "captured": captured_requested_at},
		)

		if outcome.queued_confirmations:
			attempt_immediate_confirmations(session, outcome.queued_confirmations)


@router.post("/google")
def google_webhook(
	request: Request,
	background_tasks: BackgroundTasks,
	x_goog_channel_id: str = Header(default=""),
	x_goog_channel_token: str = Header(default=""),
	x_goog_resource_state: str = Header(default=""),
):
	with get_db_context() as bootstrap_session:
		connection = _resolve_connection(bootstrap_session, "GOOGLE", x_goog_channel_id)
	if connection is None or connection.verification_secret != x_goog_channel_token:
		return Response(status_code=401)

	if x_goog_resource_state == "sync":
		# One-time no-op notification sent when the channel is first
		# created — acknowledged and discarded, never enqueued.
		return Response(status_code=200)

	_enqueue(connection.client_id, connection.connection_id)
	background_tasks.add_task(_run_sync_now, connection.client_id, connection.connection_id)
	return Response(status_code=200)


@router.post("/microsoft")
async def microsoft_webhook(request: Request, background_tasks: BackgroundTasks, validationToken: str = ""):
	# The validationToken handshake happens on THIS notification route, at
	# both subscription creation and every renewal — not the OAuth
	# callback route (corrected from an earlier draft of this plan).
	if validationToken:
		return Response(content=validationToken, media_type="text/plain")

	body = await request.json()
	items = body.get("value", [])
	seen_connections = set()
	validated_any = False
	with get_db_context() as bootstrap_session:
		for item in items:
			subscription_id = item.get("subscriptionId")
			client_state = item.get("clientState")
			connection = _resolve_connection(bootstrap_session, "MICROSOFT", subscription_id)
			if connection is None or connection.verification_secret != client_state:
				continue
			validated_any = True
			key = (connection.client_id, connection.connection_id)
			if key in seen_connections:
				continue
			seen_connections.add(key)
			_enqueue(connection.client_id, connection.connection_id)
			background_tasks.add_task(_run_sync_now, connection.client_id, connection.connection_id)

	# Empty item list is a benign Graph housekeeping ping, not an
	# authentication failure — only reject when items exist and every one
	# of them fails clientState validation.
	if items and not validated_any:
		return Response(status_code=401)
	return Response(status_code=200)


@router.post("/ghl/{connection_token}")
async def ghl_webhook(connection_token: str, request: Request, x_blackink_webhook_secret: str = Header(default="")):
	"""GoHighLevel has no OAuth app and no separate fetch step — the full
	booking payload arrives directly in this POST body (see
	src/services/ghl_webhook.py's module docstring for the payload
	contract and why authentication is a shared secret header rather than
	a computed signature, which GHL's Custom Webhook action doesn't
	support). Processed inline rather than via the enqueue-then-
	BackgroundTask pattern used for Google/Microsoft: there's no external
	API fetch to defer, so the whole request is already fast."""
	from src.services.booking_ingest import process_ghl_event_locked
	from src.services.calendar_confirmation import attempt_immediate_confirmations
	from src.services.ghl_webhook import parse_ghl_event

	with get_db_context() as bootstrap_session:
		connection = _resolve_connection(bootstrap_session, "GOHIGHLEVEL", connection_token)
	if connection is None or connection.verification_secret != x_blackink_webhook_secret:
		return Response(status_code=401)

	payload = await request.json()
	event = parse_ghl_event(payload)

	with get_db_context(client_id=connection.client_id) as session:
		outcome = process_ghl_event_locked(session, connection.connection_id, event)
		if outcome.queued_confirmations:
			attempt_immediate_confirmations(session, outcome.queued_confirmations)

	return Response(status_code=200)
