"""Minimal scoped-JWT helpers, mirroring Forced Action's admin-router
pattern (create_access_token({"sub": ..., "scope": ...}), a scope-checking
dependency) but generalized to any scope rather than hard-coded to admin.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from config.settings import get_settings


def create_access_token(subject: str, scope: str, secret: str, expires_hours: int = 24 * 30) -> str:
	payload = {
		"sub": subject,
		"scope": scope,
		"exp": datetime.now(timezone.utc) + timedelta(hours=expires_hours),
		"iat": datetime.now(timezone.utc),
	}
	return jwt.encode(payload, secret, algorithm="HS256")


def decode_scoped_token(token: str, secret: str, required_scope: str) -> Optional[dict]:
	try:
		payload = jwt.decode(token, secret, algorithms=["HS256"])
	except jwt.PyJWTError:
		return None
	if payload.get("scope") != required_scope:
		return None
	return payload
