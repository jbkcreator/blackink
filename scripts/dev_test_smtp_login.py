"""Standalone SMTP auth check — reads SMTP_TEST_* from .env.local
directly (not the real mailboxes table, not the real settings.py
pipeline) so you can quickly iterate on credentials before writing them
into the encrypted DB row via dev_set_smtp_credentials.py. Only attempts
STARTTLS + login, no actual send.

Add these to .env.local first:
    SMTP_TEST_HOST=smtp.gmail.com
    SMTP_TEST_PORT=587
    SMTP_TEST_USERNAME=heu.solutions@gmail.com
    SMTP_TEST_PASSWORD=your app password here (spaces are fine)

Usage:
    PYTHONPATH=. python scripts/dev_test_smtp_login.py
"""
import os
import smtplib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE", ".env"))

host = os.environ["SMTP_TEST_HOST"]
port = int(os.environ["SMTP_TEST_PORT"])
username = os.environ["SMTP_TEST_USERNAME"]
password = os.environ["SMTP_TEST_PASSWORD"]

print(f"Connecting to {host}:{port} as {username}...")
try:
	with smtplib.SMTP(host, port, timeout=15) as smtp:
		smtp.starttls()
		smtp.login(username, password)
	print("Login succeeded.")
except smtplib.SMTPAuthenticationError as exc:
	print(f"Login FAILED: {exc}")
	raise SystemExit(1)
