"""Simulates a Google Calendar push notification hitting our own webhook
route locally — exercises the real route logic (channel/token validation,
enqueue into calendar_sync_queue, background processing) without needing
Google to actually deliver anything, so no public URL/tunnel is required
for this specific test. Requires the local `uvicorn` server to already be
running (python -m uvicorn src.api.main:app --reload --port 8000).

Usage:
    PYTHONPATH=. python scripts/dev_simulate_google_webhook.py <connection_id>
"""
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from sqlalchemy import text

from src.core.database import get_owner_db_context


def main() -> int:
	if len(sys.argv) != 2:
		print("Usage: python scripts/dev_simulate_google_webhook.py <connection_id>")
		return 1
	connection_id = int(sys.argv[1])

	with get_owner_db_context() as db:
		row = db.execute(
			text("SELECT subscription_id, verification_secret, client_id FROM calendar_connections WHERE connection_id = :id"),
			{"id": connection_id},
		).one()

	headers = {
		"X-Goog-Channel-ID": row.subscription_id,
		"X-Goog-Channel-Token": row.verification_secret,
		"X-Goog-Resource-State": "exists",
	}
	print(f"POSTing to http://localhost:8000/api/v1/webhooks/booking/google as if Google sent it...")
	resp = requests.post("http://localhost:8000/api/v1/webhooks/booking/google", headers=headers, timeout=10)
	print(f"Response: {resp.status_code}")

	print("Waiting 3 seconds for the background task to process it...")
	time.sleep(3)

	with get_owner_db_context() as db:
		latest = db.execute(
			text(
				"SELECT event_type, entity_id, created_at FROM events "
				"WHERE client_id = :cid ORDER BY created_at DESC LIMIT 5"
			),
			{"cid": row.client_id},
		).fetchall()
	print("Latest events for this client:")
	for e in latest:
		print(f"  {e.created_at}  {e.event_type}  entity_id={e.entity_id}")

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
