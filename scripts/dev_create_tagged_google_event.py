"""Dev-only: creates a real, tagged test event on an already-connected
Google Calendar, via the API — Google Calendar's own UI (web/mobile) has
no way to set a custom extendedProperties value, so a manually-created
event can never carry the blackink_booking tag process_booking_webhook
requires. This script uses the connection's own stored (decrypted, then
refreshed-if-needed) access token to insert one directly, for exercising
the real webhook path end-to-end.

Usage:
    PYTHONPATH=. python scripts/dev_create_tagged_google_event.py <client_id>

Prints the wall-clock time the insert call was made — compare against
the `events.created_at` timestamp of the resulting meeting_booked row
(SELECT created_at FROM events WHERE event_type='meeting_booked' ORDER
BY created_at DESC LIMIT 1) to verify the 5-second promise. The event is
scheduled 1 hour from now with a 30-minute duration and a real
throwaway attendee address — delete it from Google Calendar afterward if
you don't want it lingering.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from src.core.database import get_db_context
from src.services.booking_ingest import BOOKING_TAG_KEY, BOOKING_TAG_VALUE
from src.services.calendar_oauth import get_valid_access_token
from sqlalchemy import text


def main() -> int:
	if len(sys.argv) != 2:
		print("Usage: python scripts/dev_create_tagged_google_event.py <client_id>")
		return 1
	client_id = sys.argv[1]

	with get_db_context(client_id=client_id) as session:
		connection = session.execute(
			text("SELECT * FROM calendar_connections WHERE client_id = :cid AND provider = 'GOOGLE'"),
			{"cid": client_id},
		).one()
		access_token = get_valid_access_token(session, connection)

	start = datetime.now(timezone.utc) + timedelta(hours=1)
	end = start + timedelta(minutes=30)
	body = {
		"summary": "Blackink test booking (dev script)",
		"start": {"dateTime": start.isoformat()},
		"end": {"dateTime": end.isoformat()},
		"attendees": [{"email": "owner-test@example.com", "displayName": "Test Owner"}],
		"extendedProperties": {"private": {BOOKING_TAG_KEY: BOOKING_TAG_VALUE}},
	}

	insert_time = datetime.now(timezone.utc)
	resp = requests.post(
		f"https://www.googleapis.com/calendar/v3/calendars/{connection.external_calendar_id}/events",
		headers={"Authorization": f"Bearer {access_token}"},
		json=body,
		timeout=15,
	)
	resp.raise_for_status()
	event = resp.json()

	print(f"Created tagged Google Calendar event {event['id']} at {insert_time.isoformat()} (UTC).")
	print("Wait a few seconds for the push notification, then check:")
	print(
		"  SELECT created_at, payload FROM events WHERE event_type = 'meeting_booked' "
		"ORDER BY created_at DESC LIMIT 1;"
	)
	print(f"created_at should be within 5 seconds of {insert_time.isoformat()}.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
