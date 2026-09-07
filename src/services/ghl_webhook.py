"""GoHighLevel (GHL) fallback booking webhook — client comment W1-8:
"GoHighLevel is the fallback for a client with no connectable
[Google/Microsoft] calendar." Built as a real, working path per explicit
instruction, reusing the identical booking/owner-matching/confirmation
pipeline as Google and Microsoft (src/services/booking_ingest.py's
process_ghl_event_locked() shares _process_event() with
sync_connection_locked() — not a parallel implementation). No SMS, same
as every other provider in this subsystem.

GHL's webhook model is fundamentally different from Google Calendar /
Microsoft Graph, which drove different design choices here:

- **No OAuth app, no fetch step.** GHL delivers the full booking payload
  directly in the webhook POST body, configured as a "Custom Webhook"
  workflow action inside the client's GHL account (their ops team sets
  this up when a client has no Google/Microsoft calendar — a manual
  onboarding step, not code). There is nothing to OAuth-connect and
  nothing to paginate/sync — one webhook call is one booking event.
- **No native HMAC signature scheme.** Unlike Stripe-style webhooks, a
  GHL Custom Webhook action only supports adding static custom headers,
  not a computed per-request signature. Authentication here is a shared
  secret header (`X-Blackink-Webhook-Secret`) compared against
  `calendar_connections.verification_secret` — the practical equivalent
  available with GHL's actual capabilities, not a gap.
- **The JSON body shape is our own contract**, not GHL's native payload
  field names (which the workflow admin can freely remap in GHL's UI
  anyway) — configure the GHL workflow's webhook body template to send
  exactly this:

    {
      "event_id": "<GHL appointment id, stable across reschedules>",
      "status": "booked" | "cancelled" | "rescheduled",
      "scheduled_at": "<ISO 8601>",
      "created_at": "<ISO 8601>",
      "rep_name": "<assigned user's name>",
      "rep_email": "<assigned user's email>",
      "owner_name": "<contact's name>",
      "owner_email": "<contact's email>",
      "owner_phone": "<contact's phone, optional>",
      "door_count": <int, optional, from a custom field>
    }
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.booking_ingest import NormalizedEvent

PLACEHOLDER_CALENDAR_ID = "GHL_WORKFLOW"


def mint_ghl_webhook_credentials(session: Session, client_id: str) -> dict:
	"""Provisions (or rotates) this client's GHL webhook connection.
	Returns {"connection_token": ..., "secret": ...} — handed to whoever
	configures the client's GHL workflow (an onboarding/ops step, not
	built here): the webhook URL is
	/api/v1/webhooks/booking/ghl/{connection_token}, and {secret} goes in
	the workflow's X-Blackink-Webhook-Secret custom header."""
	connection_token = secrets.token_urlsafe(24)
	secret = secrets.token_urlsafe(32)
	session.execute(
		text(
			"""
			INSERT INTO calendar_connections
				(client_id, provider, external_calendar_id, subscription_id, verification_secret, status)
			VALUES (:client_id, 'GOHIGHLEVEL', :placeholder, :token, :secret, 'ACTIVE')
			ON CONFLICT (client_id, provider) WHERE connection_scope = 'CLIENT_OWNER_BOOKING' DO UPDATE SET
				subscription_id = EXCLUDED.subscription_id,
				verification_secret = EXCLUDED.verification_secret,
				status = 'ACTIVE',
				updated_at = NOW()
			RETURNING connection_id
			"""
		),
		{"client_id": client_id, "placeholder": PLACEHOLDER_CALENDAR_ID, "token": connection_token, "secret": secret},
	)
	return {"connection_token": connection_token, "secret": secret}


_STATUS_MAP = {"booked": "CONFIRMED", "cancelled": "CANCELLED", "rescheduled": "CONFIRMED"}


def parse_ghl_event(payload: dict) -> NormalizedEvent:
	def _dt(value: Optional[str]) -> Optional[datetime]:
		if not value:
			return None
		return datetime.fromisoformat(value.replace("Z", "+00:00"))

	status = _STATUS_MAP.get(payload.get("status"), "CONFIRMED")
	return NormalizedEvent(
		external_event_id=payload["event_id"],
		# GHL's webhook is purpose-built per client for this exact flow —
		# unlike a general-purpose Google/Microsoft calendar mixing
		# personal and business events, every delivered notification IS a
		# Blackink booking by construction, so there's no separate tag to
		# check for.
		tagged=True,
		event_status=status,
		scheduled_at=_dt(payload.get("scheduled_at")),
		created_at=_dt(payload.get("created_at")),
		client_rep_name=payload.get("rep_name"),
		client_rep_email=payload.get("rep_email"),
		owner_name=payload.get("owner_name"),
		owner_email=payload.get("owner_email"),
		owner_phone=payload.get("owner_phone"),
		raw_payload=payload,
	)
