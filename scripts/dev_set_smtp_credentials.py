"""Interactively prompts for real SMTP credentials (never passed as CLI
args or pasted into chat) and stores them for a test client — creates a
sending_domains row (SPF/DKIM/DMARC flags set True manually, since this
is reusing an already-deliverable external account, not validating our
own DNS) and a mailboxes row with the password encrypted via
token_crypto before it ever touches the database.

Usage:
    PYTHONPATH=. python scripts/dev_set_smtp_credentials.py <client_id>
"""
import getpass
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context
from src.core.token_crypto import encrypt_token


def main() -> int:
	if len(sys.argv) != 2:
		print("Usage: python scripts/dev_set_smtp_credentials.py <client_id>")
		return 1
	client_id = sys.argv[1]

	smtp_host = input("SMTP host (e.g. smtp.gmail.com): ").strip()
	smtp_port = int(input("SMTP port (e.g. 587): ").strip())
	smtp_username = input("SMTP username: ").strip()
	smtp_password = getpass.getpass("SMTP password (hidden input): ").strip()
	from_address = input("From address (mailbox_address): ").strip()

	with get_owner_db_context() as db:
		domain = from_address.split("@", 1)[1]
		domain_id = db.execute(
			text(
				"INSERT INTO sending_domains (domain, client_id, spf_validated, dkim_validated, dmarc_validated) "
				"VALUES (:domain, :cid, TRUE, TRUE, TRUE) "
				"ON CONFLICT (domain) DO UPDATE SET spf_validated = TRUE, dkim_validated = TRUE, dmarc_validated = TRUE "
				"RETURNING id"
			),
			{"domain": domain, "cid": client_id},
		).scalar()

		db.execute(
			text(
				"INSERT INTO mailboxes (domain_id, mailbox_address, client_id, smtp_host, smtp_port, "
				"smtp_username, smtp_password_encrypted, quarantine_state) "
				"VALUES (:domain_id, :addr, :cid, :host, :port, :user, :pw, 'active') "
				"ON CONFLICT (mailbox_address) DO UPDATE SET "
				"smtp_host = :host, smtp_port = :port, smtp_username = :user, "
				"smtp_password_encrypted = :pw, quarantine_state = 'active'"
			),
			{
				"domain_id": domain_id,
				"addr": from_address,
				"cid": client_id,
				"host": smtp_host,
				"port": smtp_port,
				"user": smtp_username,
				"pw": encrypt_token(smtp_password),
			},
		)
		db.commit()

	print(f"SMTP credentials stored (encrypted) for client {client_id}, mailbox {from_address}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
