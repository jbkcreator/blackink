"""Directly exercises send_booking_confirmation_email() (real SMTP + real
ICS) against a real, checkable inbox — decoupled from the Google Calendar
round trip, since that's not what this specific DoD item needs to prove.

Usage:
    PYTHONPATH=. python scripts/dev_send_test_confirmation_email.py <client_id> <real_email_to_check>
"""
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.database import get_db_context
from src.services.email_dispatch import send_booking_confirmation_email


def main() -> int:
	if len(sys.argv) != 3:
		print("Usage: python scripts/dev_send_test_confirmation_email.py <client_id> <real_email_to_check>")
		return 1
	client_id, to_email = sys.argv[1], sys.argv[2]

	start = time.monotonic()
	with get_db_context(client_id=client_id) as session:
		message_id = send_booking_confirmation_email(
			session,
			client_id=client_id,
			booking_id=999999,  # not a real booking row — fine, this only affects the ICS uid/logging
			owner_email=to_email,
			owner_name="Test Owner",
			client_reply_to=to_email,
			scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
		)
	elapsed = time.monotonic() - start

	if message_id is None:
		print(f"BLOCKED — check the booking_confirmation_blocked event for why (elapsed {elapsed:.2f}s)")
	else:
		print(f"Sent. Provider message id: {message_id} (elapsed {elapsed:.2f}s)")
		print(f"Check {to_email}'s inbox for a message with a working ICS attachment.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
