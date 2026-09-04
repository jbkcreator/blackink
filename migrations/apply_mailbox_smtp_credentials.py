"""
Add encrypted SMTP credential columns to mailboxes (Dev 3, Subtask
3.2.1 — Inbound Booking Engine's real email confirmation path).

mailboxes previously stored no credentials at all — instantly_account_email
is an identifier string, not a secret (Instantly's own API key lives in
settings.instantly_api_key, and Instantly's integration is read-only
analytics only, see src/services/instantly_service.py's docstring; it has
no send/campaign-create capability). Booking confirmation email is real,
gated by settings.email_sending_enabled (default False so missing
configuration is a visible launch blocker, not a silent no-op), and sent
via src/services/email_dispatch.py's SmtpEmailProvider through the
client's own delegated subdomain — the existing Respond design already
documented in CLAUDE.md (delegated subdomain, Reply-To the client, BCC
the client). smtp_password_encrypted is encrypted at rest via
src/core/token_crypto.py (Fernet) — no per-row DB encryption existed
anywhere in this repo before this table's sibling, calendar_connections.

Idempotent: ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_mailbox_smtp_credentials.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smtp_host VARCHAR(255)",
	"ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smtp_port INTEGER",
	"ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smtp_username VARCHAR(255)",
	"ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smtp_password_encrypted TEXT",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_mailbox_smtp_credentials: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
