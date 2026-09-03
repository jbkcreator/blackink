"""Cryptographic resume-token generation and verification for Relay halts.

A halt can only be cleared by presenting the correct HMAC-SHA256 token for its
halt_id. The token is generated at issue time by an authorized admin (or Slack
command handler), shared out-of-band (e.g. printed to #blackink-command), and
presented back through the /relay-resume Slack command.

This intentionally does NOT store the token — the HMAC is deterministic given
the secret and halt_id, so generation and verification are both stateless.
RELAY_RESUME_SECRET must be a high-entropy string (32+ bytes recommended).
"""

import hashlib
import hmac
from typing import Optional

from config.settings import get_settings


def _secret() -> bytes:
    s = get_settings()
    if not s.relay_resume_secret:
        raise RuntimeError(
            "RELAY_RESUME_SECRET is not configured — "
            "set this env var before issuing or verifying halt resume tokens."
        )
    return s.relay_resume_secret.get_secret_value().encode()


def generate_resume_token(halt_id: int) -> str:
    """Return a 64-hex-char HMAC-SHA256 token bound to halt_id."""
    return hmac.new(_secret(), str(halt_id).encode(), hashlib.sha256).hexdigest()


def verify_resume_token(halt_id: int, token: str) -> bool:
    """Constant-time comparison — safe against timing attacks."""
    try:
        expected = generate_resume_token(halt_id)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, token)
