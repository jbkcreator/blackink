"""Google Calendar and Microsoft Graph implementations of
CalendarProviderClient (src/services/booking_ingest.py). Deliberately two
separate parsing functions, not one shared code path pretending the two
providers' payloads are equivalent — their recurrence, deletion, and
sync-token semantics genuinely differ (see booking_ingest.py's module
docstring and the plan's "Provider-specific recurrence and deletion"
section).

Google-specific correctness constraints (hard API constraints, not style
choices):
- `singleEvents=true` is required to receive individually-IDed recurring
  instances instead of one master event per series, and must be the same
  value on every call across a sync-token's lifetime.
- `orderBy=startTime` is only valid on the initial (non-incremental) call
  — it cannot be combined with `syncToken`.
- `syncToken` mode does not support a reliable server-side
  `extendedProperties` filter — the booking tag is checked entirely in
  application code (_is_tagged_google / _is_tagged_microsoft below),
  never as a request parameter, on both the baseline and incremental
  calls.

Deletion/cancellation payloads from either provider can be minimal (just
an id and a cancelled/removed marker, no extendedProperties) — this
module still surfaces those as NormalizedEvent(event_status='CANCELLED',
tagged=False); booking_ingest.py's cancellation path intentionally does
NOT gate on `tagged` (see its module docstring) — it looks the event up
in the existing `bookings` ledger by tenant-scoped identity instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests

from src.services.booking_ingest import (
	BOOKING_TAG_KEY,
	BOOKING_TAG_VALUE,
	CalendarProviderClient,
	FetchResult,
	NormalizedEvent,
	SyncTokenInvalidError,
)
from src.services.calendar_oauth import get_valid_access_token
from sqlalchemy.orm import Session

_GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
_GOOGLE_WATCH_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/watch"

_MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
	if not value:
		return None
	return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_tagged_google(item: dict) -> bool:
	private = (item.get("extendedProperties") or {}).get("private") or {}
	return private.get(BOOKING_TAG_KEY) == BOOKING_TAG_VALUE


def _normalize_google_event(item: dict) -> NormalizedEvent:
	organizer = item.get("organizer") or {}
	attendees = item.get("attendees") or []
	non_organizer = next((a for a in attendees if not a.get("organizer")), {})
	status = "CANCELLED" if item.get("status") == "cancelled" else "CONFIRMED"
	start = (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date")
	return NormalizedEvent(
		external_event_id=item["id"],
		tagged=_is_tagged_google(item),
		event_status=status,
		scheduled_at=_parse_dt(start),
		created_at=_parse_dt(item.get("created")),
		client_rep_name=organizer.get("displayName"),
		client_rep_email=organizer.get("email"),
		owner_name=non_organizer.get("displayName"),
		owner_email=non_organizer.get("email"),
		owner_phone=None,  # Google Calendar attendees carry no phone field
		raw_payload=item,
	)


class GoogleCalendarClient:
	"""Implements CalendarProviderClient for one connection at a time —
	constructed per sync_connection() call with the DB session and the
	connection row so it can refresh/persist the access token as needed."""

	def __init__(self, session: Session):
		self._session = session

	def _headers(self, connection) -> dict:
		token = get_valid_access_token(self._session, connection)
		return {"Authorization": f"Bearer {token}"}

	def fetch_baseline(self, connection) -> FetchResult:
		events: list = []
		params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": "250"}
		url = _GOOGLE_EVENTS_URL.format(calendar_id=connection.external_calendar_id)
		next_sync_token = None
		while True:
			resp = requests.get(url, headers=self._headers(connection), params=params, timeout=15)
			resp.raise_for_status()
			body = resp.json()
			events.extend(_normalize_google_event(item) for item in body.get("items", []))
			next_sync_token = body.get("nextSyncToken", next_sync_token)
			page_token = body.get("nextPageToken")
			if not page_token:
				break
			params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": "250", "pageToken": page_token}
		return FetchResult(events=events, sync_token=next_sync_token or "")

	def fetch_incremental(self, connection) -> FetchResult:
		events: list = []
		# singleEvents must match the baseline call's value; orderBy is
		# omitted here — Google rejects orderBy combined with syncToken.
		params = {"singleEvents": "true", "syncToken": connection.sync_token, "maxResults": "250"}
		url = _GOOGLE_EVENTS_URL.format(calendar_id=connection.external_calendar_id)
		next_sync_token = connection.sync_token
		while True:
			resp = requests.get(url, headers=self._headers(connection), params=params, timeout=15)
			if resp.status_code == 410:
				raise SyncTokenInvalidError("Google sync token expired/invalid - full resync required")
			resp.raise_for_status()
			body = resp.json()
			events.extend(_normalize_google_event(item) for item in body.get("items", []))
			next_sync_token = body.get("nextSyncToken", next_sync_token)
			page_token = body.get("nextPageToken")
			if not page_token:
				break
			params = {"singleEvents": "true", "syncToken": connection.sync_token, "maxResults": "250", "pageToken": page_token}
		return FetchResult(events=events, sync_token=next_sync_token)

	def register_watch(self, connection, channel_id: str, webhook_url: str, verification_secret: str) -> dict:
		"""channel_id is generated by the caller (calendar_oauth_router's
		callback) and stored as calendar_connections.subscription_id —
		notifications carry it back verbatim as X-Goog-Channel-ID, which is
		what the webhook route looks the connection up by. Google's own
		resourceId (needed only for unsubscribing, not implemented here) is
		returned separately, not stored."""
		url = _GOOGLE_WATCH_URL.format(calendar_id=connection.external_calendar_id)
		resp = requests.post(
			url,
			headers=self._headers(connection),
			json={"id": channel_id, "type": "web_hook", "address": webhook_url, "token": verification_secret},
			timeout=15,
		)
		resp.raise_for_status()
		body = resp.json()
		return {"resource_id": body["resourceId"], "expires_at_ms": body.get("expiration")}


def _is_tagged_microsoft(item: dict) -> bool:
	props = item.get("singleValueExtendedProperties") or []
	for p in props:
		if p.get("value") == BOOKING_TAG_VALUE and BOOKING_TAG_KEY in (p.get("id") or ""):
			return True
	return False


def _normalize_microsoft_event(item: dict) -> NormalizedEvent:
	if item.get("@removed"):
		return NormalizedEvent(
			external_event_id=item["id"],
			tagged=False,
			event_status="CANCELLED",
			scheduled_at=None,
			created_at=None,
			client_rep_name=None,
			client_rep_email=None,
			owner_name=None,
			owner_email=None,
			owner_phone=None,
			raw_payload=item,
		)
	organizer = ((item.get("organizer") or {}).get("emailAddress")) or {}
	attendees = item.get("attendees") or []
	non_organizer_addr = {}
	for a in attendees:
		addr = a.get("emailAddress") or {}
		if addr.get("address") != organizer.get("address"):
			non_organizer_addr = addr
			break
	status = "CANCELLED" if item.get("isCancelled") else "CONFIRMED"
	start = (item.get("start") or {}).get("dateTime")
	return NormalizedEvent(
		external_event_id=item["id"],
		tagged=_is_tagged_microsoft(item),
		event_status=status,
		scheduled_at=_parse_dt(start),
		created_at=_parse_dt(item.get("createdDateTime")),
		client_rep_name=organizer.get("name"),
		client_rep_email=organizer.get("address"),
		owner_name=non_organizer_addr.get("name"),
		owner_email=non_organizer_addr.get("address"),
		owner_phone=None,
		raw_payload=item,
	)


class MicrosoftGraphClient:
	"""Implements CalendarProviderClient for Microsoft Graph. Delta-query
	shape (series masters + exception instances, @removed markers for
	deletions) is genuinely different from Google's — see module
	docstring; this is not a shared code path with GoogleCalendarClient."""

	_EXTENDED_PROP_SELECT = (
		"$expand=singleValueExtendedProperties($filter=id eq 'String {" + BOOKING_TAG_KEY + "}')"
	)

	def __init__(self, session: Session):
		self._session = session

	def _headers(self, connection) -> dict:
		token = get_valid_access_token(self._session, connection)
		return {"Authorization": f"Bearer {token}"}

	def fetch_baseline(self, connection) -> FetchResult:
		events: list = []
		url = (
			f"{_MS_GRAPH_BASE}/me/calendars/{connection.external_calendar_id}/events/delta"
			f"?{self._EXTENDED_PROP_SELECT}"
		)
		delta_link = None
		while url:
			resp = requests.get(url, headers=self._headers(connection), timeout=15)
			resp.raise_for_status()
			body = resp.json()
			events.extend(_normalize_microsoft_event(item) for item in body.get("value", []))
			url = body.get("@odata.nextLink")
			delta_link = body.get("@odata.deltaLink", delta_link)
		return FetchResult(events=events, sync_token=delta_link or "")

	def fetch_incremental(self, connection) -> FetchResult:
		events: list = []
		url = connection.sync_token
		delta_link = connection.sync_token
		while url:
			resp = requests.get(url, headers=self._headers(connection), timeout=15)
			if resp.status_code == 410 or resp.status_code == 400:
				raise SyncTokenInvalidError("Graph delta link expired/invalid - full resync required")
			resp.raise_for_status()
			body = resp.json()
			events.extend(_normalize_microsoft_event(item) for item in body.get("value", []))
			url = body.get("@odata.nextLink")
			delta_link = body.get("@odata.deltaLink", delta_link)
		return FetchResult(events=events, sync_token=delta_link)

	def register_subscription(self, connection, webhook_url: str, verification_secret: str) -> dict:
		# Graph caps event-resource subscriptions at 4230 minutes (~3 days) —
		# calendar_subscription_renewal.py is what keeps this from silently
		# lapsing, not a longer expiration here.
		from datetime import timedelta

		expires_at = datetime.now(timezone.utc) + timedelta(minutes=4230)
		resp = requests.post(
			f"{_MS_GRAPH_BASE}/subscriptions",
			headers=self._headers(connection),
			json={
				"changeType": "created,updated,deleted",
				"notificationUrl": webhook_url,
				"resource": f"me/calendars/{connection.external_calendar_id}/events",
				"expirationDateTime": expires_at.isoformat(),
				"clientState": verification_secret,
			},
			timeout=15,
		)
		resp.raise_for_status()
		body = resp.json()
		return {"subscription_id": body["id"], "expires_at": body.get("expirationDateTime")}
