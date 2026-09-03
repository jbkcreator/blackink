"""Dev-only CLI to mint a calendar connect-link — stands in for the
onboarding flow's calendar-connect step (a separate, not-built-here
subtask), which is what triggers mint_calendar_connect_link() in
production. NEVER the production trigger — see
src/services/calendar_oauth.py's module docstring.

Usage:
    PYTHONPATH=. python scripts/dev_mint_connect_link.py <client_id> <GOOGLE|MICROSOFT>

Prints the full connect URL — open it in a browser to run the real
OAuth consent flow. Requires the client_id to already exist in the
`clients` table (calendar_connections.client_id is a hard FK) and
CALENDAR_WEBHOOK_BASE_URL to be set to your public tunnel URL, since
that's also used to build the OAuth redirect_uri.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.calendar_oauth import mint_calendar_connect_link


def main() -> int:
	if len(sys.argv) != 3 or sys.argv[2].upper() not in ("GOOGLE", "MICROSOFT"):
		print("Usage: python scripts/dev_mint_connect_link.py <client_id> <GOOGLE|MICROSOFT>")
		return 1
	client_id, provider = sys.argv[1], sys.argv[2].upper()

	settings = get_settings()
	with get_db_context() as session:
		token = mint_calendar_connect_link(session, client_id, provider)

	connect_url = f"{settings.calendar_webhook_base_url}/api/v1/calendar/connect/{provider.lower()}?token={token}"
	print(f"Open this URL in a browser to connect {client_id}'s {provider} calendar:\n\n{connect_url}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
