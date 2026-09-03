"""Scheduled sync worker (Subtask 3.2.1) — the durability backstop for
the webhook route's fast-ack-then-BackgroundTask common path (see
src/api/booking_webhook_router.py). Two responsibilities on a short
polling interval:

1. Drain any calendar_sync_queue row not yet cleared — covers a
   BackgroundTask that never completed (process restart, crash mid-sync).
2. On a longer interval, sweep every ACTIVE connection regardless of
   whether a queue row exists — defense in depth against any
   missed/dropped push notification, not just the connect-time
   initialization boundary. Incremental sync via sync_token/delta link is
   idempotent and inexpensive to re-run, so this is safe to do on a
   schedule rather than only reactively.

Runs under the BYPASSRLS system session — this sweep spans every
client's connections by nature, same posture as
county_allocation_reassessment.py.
"""

import logging

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.booking_ingest import sync_connection_locked
from src.services.calendar_confirmation import attempt_immediate_confirmations
from src.services.calendar_providers import GoogleCalendarClient, MicrosoftGraphClient

logger = logging.getLogger(__name__)


def _client_for(session, provider: str):
	return GoogleCalendarClient(session) if provider == "GOOGLE" else MicrosoftGraphClient(session)


def drain_queue() -> int:
	processed = 0
	with get_system_db_context() as session:
		queued = session.execute(text("SELECT connection_id, requested_at FROM calendar_sync_queue")).fetchall()
		for row in queued:
			connection_id, captured_requested_at = row.connection_id, row.requested_at
			provider_row = session.execute(
				text("SELECT provider FROM calendar_connections WHERE connection_id = :id"), {"id": connection_id}
			).first()
			if provider_row is None:
				session.execute(
					text("DELETE FROM calendar_sync_queue WHERE connection_id = :id"), {"id": connection_id}
				)
				continue
			client = _client_for(session, provider_row.provider)
			try:
				outcome = sync_connection_locked(session, connection_id, client)
			except Exception:
				logger.exception("calendar_sync_worker: sync failed for connection %s", connection_id)
				continue
			session.execute(
				text("DELETE FROM calendar_sync_queue WHERE connection_id = :id AND requested_at <= :captured"),
				{"id": connection_id, "captured": captured_requested_at},
			)
			if outcome.queued_confirmations:
				attempt_immediate_confirmations(session, outcome.queued_confirmations)
			processed += 1
	logger.info("calendar_sync_worker: drained %d queued connection(s)", processed)
	return processed


def sweep_all_active_connections() -> int:
	"""Periodic safety net — closes the initialization-boundary and
	dropped-notification gaps that pure push-notification reliance can't
	guarantee against (see the plan doc)."""
	swept = 0
	with get_system_db_context() as session:
		connections = session.execute(
			text("SELECT connection_id, provider FROM calendar_connections WHERE status = 'ACTIVE'")
		).fetchall()
		for row in connections:
			client = _client_for(session, row.provider)
			try:
				outcome = sync_connection_locked(session, row.connection_id, client)
			except Exception:
				logger.exception("calendar_sync_worker: safety sweep failed for connection %s", row.connection_id)
				continue
			if outcome.queued_confirmations:
				attempt_immediate_confirmations(session, outcome.queued_confirmations)
			swept += 1
	logger.info("calendar_sync_worker: safety sweep touched %d active connection(s)", swept)
	return swept


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	drain_queue()
	sweep_all_active_connections()
