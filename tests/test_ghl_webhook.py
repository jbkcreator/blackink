"""Tests for the GoHighLevel fallback webhook (client comment W1-8) —
both the service layer (live DB, reused booking pipeline) and the HTTP
route's literal 401/200 contract (Subtask 3.2.1 DoD)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.main import app
from src.core.database import get_db_context, get_owner_db_context
from src.services.booking_ingest import process_ghl_event_locked
from src.services.ghl_webhook import mint_ghl_webhook_credentials, parse_ghl_event
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


@pytest.fixture
def ghl_connection(canary_tenants):
	with get_db_context(client_id=CANARY_A) as session:
		creds = mint_ghl_webhook_credentials(session, CANARY_A)
	yield creds
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = :cid"), {"cid": CANARY_A})
		session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": CANARY_A})
	with get_db_context(client_id=CANARY_A) as session:
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = :cid AND provider = 'GOHIGHLEVEL'"), {"cid": CANARY_A})


def _payload(event_id="ghl-evt-1", status="booked"):
	now = datetime.now(timezone.utc)
	return {
		"event_id": event_id,
		"status": status,
		"scheduled_at": (now + timedelta(days=1)).isoformat(),
		"created_at": (now - timedelta(days=1)).isoformat(),
		"rep_name": "Rep Name",
		"rep_email": "rep@clientfirm.com",
		"owner_name": "Jane Owner",
		"owner_email": "ghl-owner@example.com",
		"owner_phone": "+15555550100",
		"door_count": 12,
	}


def test_parse_ghl_event_maps_status_correctly():
	booked = parse_ghl_event(_payload(status="booked"))
	assert booked.event_status == "CONFIRMED"
	assert booked.tagged is True

	cancelled = parse_ghl_event(_payload(status="cancelled"))
	assert cancelled.event_status == "CANCELLED"


def test_process_ghl_event_creates_booking_and_meeting_booked_event(ghl_connection):
	with get_db_context(client_id=CANARY_A) as session:
		connection_id = session.execute(
			text("SELECT connection_id FROM calendar_connections WHERE client_id = :cid AND provider = 'GOHIGHLEVEL'"),
			{"cid": CANARY_A},
		).one().connection_id

		event = parse_ghl_event(_payload())
		outcome = process_ghl_event_locked(session, connection_id, event)
		assert outcome.new_bookings == 1

		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'ghl-evt-1'"),
			{"id": connection_id},
		).one()
		assert booking.status == "PENDING_RECONCILIATION"
		assert booking.confirmation_status == "PENDING"

		booked_event = session.execute(
			text("SELECT * FROM events WHERE event_type = 'meeting_booked' AND entity_id = :bid"),
			{"bid": str(booking.booking_id)},
		).one()
		assert booked_event.payload["baseline"] is False


def test_process_ghl_cancellation_updates_existing_booking(ghl_connection):
	with get_db_context(client_id=CANARY_A) as session:
		connection_id = session.execute(
			text("SELECT connection_id FROM calendar_connections WHERE client_id = :cid AND provider = 'GOHIGHLEVEL'"),
			{"cid": CANARY_A},
		).one().connection_id

		process_ghl_event_locked(session, connection_id, parse_ghl_event(_payload(event_id="ghl-evt-2")))
		outcome = process_ghl_event_locked(
			session, connection_id, parse_ghl_event(_payload(event_id="ghl-evt-2", status="cancelled"))
		)
		assert outcome.cancelled == 1

		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'ghl-evt-2'"),
			{"id": connection_id},
		).one()
		assert booking.event_status == "CANCELLED"
		assert booking.confirmation_status == "CANCELLED"


def test_ghl_webhook_route_rejects_wrong_secret(ghl_connection):
	client = TestClient(app)
	resp = client.post(
		f"/api/v1/webhooks/booking/ghl/{ghl_connection['connection_token']}",
		json=_payload(event_id="ghl-evt-http-1"),
		headers={"X-Blackink-Webhook-Secret": "wrong-secret"},
	)
	assert resp.status_code == 401


def test_ghl_webhook_route_accepts_correct_secret(ghl_connection):
	client = TestClient(app)
	resp = client.post(
		f"/api/v1/webhooks/booking/ghl/{ghl_connection['connection_token']}",
		json=_payload(event_id="ghl-evt-http-2"),
		headers={"X-Blackink-Webhook-Secret": ghl_connection["secret"]},
	)
	assert resp.status_code == 200

	with get_db_context(client_id=CANARY_A) as session:
		booking = session.execute(
			text("SELECT * FROM bookings WHERE external_event_id = 'ghl-evt-http-2'")
		).one()
		assert booking.status == "PENDING_RECONCILIATION"
