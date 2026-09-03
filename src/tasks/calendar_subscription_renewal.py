"""Scheduled OAuth-token and push-subscription renewal (Subtask 3.2.1).
Both providers' push channels are day-scale, not month-scale (Google
channels and Microsoft Graph event subscriptions both expire within a
few days) — an unrenewed subscription silently stops delivering
bookings, which is worse than an explicit failure, so this task runs
periodically and marks a connection status='NEEDS_RECONNECT' on renewal
failure rather than leaving a stale row that looks healthy.

Access-token refresh itself is handled lazily by
src/services/calendar_oauth.get_valid_access_token() on every provider
call — this task's own job is specifically the push-subscription
renewal, which nothing else triggers on its own.

Runs under the BYPASSRLS system session — same posture as the other
scheduled tasks in this subtask.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.calendar_providers import GoogleCalendarClient, MicrosoftGraphClient

logger = logging.getLogger(__name__)

_RENEW_WITHIN = timedelta(hours=12)


def _webhook_url(provider: str) -> str:
	settings = get_settings()
	return f"{settings.calendar_webhook_base_url}/api/v1/webhooks/booking/{provider.lower()}"


def run_renewal_sweep() -> int:
	renewed = 0
	with get_system_db_context() as session:
		due = session.execute(
			text(
				"SELECT connection_id, provider FROM calendar_connections "
				"WHERE status = 'ACTIVE' AND (expires_at IS NULL OR expires_at <= :cutoff)"
			),
			{"cutoff": datetime.now(timezone.utc) + _RENEW_WITHIN},
		).fetchall()

		for row in due:
			connection = session.execute(
				text("SELECT * FROM calendar_connections WHERE connection_id = :id"), {"id": row.connection_id}
			).one()
			try:
				if row.provider == "GOOGLE":
					client = GoogleCalendarClient(session)
					new_channel_id = secrets.token_urlsafe(24)
					verification_secret = secrets.token_urlsafe(32)
					result = client.register_watch(
						connection, new_channel_id, _webhook_url("GOOGLE"), verification_secret
					)
					session.execute(
						text(
							"UPDATE calendar_connections SET subscription_id = :sub, "
							"verification_secret = :secret, updated_at = NOW() WHERE connection_id = :id"
						),
						{"sub": new_channel_id, "secret": verification_secret, "id": connection.connection_id},
					)
				else:
					client = MicrosoftGraphClient(session)
					verification_secret = secrets.token_urlsafe(32)
					result = client.register_subscription(
						connection, _webhook_url("MICROSOFT"), verification_secret
					)
					session.execute(
						text(
							"UPDATE calendar_connections SET subscription_id = :sub, "
							"verification_secret = :secret, expires_at = :expires_at, updated_at = NOW() "
							"WHERE connection_id = :id"
						),
						{
							"sub": result["subscription_id"],
							"secret": verification_secret,
							"expires_at": result.get("expires_at"),
							"id": connection.connection_id,
						},
					)
				renewed += 1
			except Exception:
				logger.exception("calendar_subscription_renewal: renewal failed for connection %s", row.connection_id)
				session.execute(
					text("UPDATE calendar_connections SET status = 'NEEDS_RECONNECT', updated_at = NOW() WHERE connection_id = :id"),
					{"id": row.connection_id},
				)

	logger.info("calendar_subscription_renewal: renewed %d connection(s)", renewed)
	return renewed


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_renewal_sweep()
