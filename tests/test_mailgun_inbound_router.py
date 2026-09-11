"""Unit test for the mailgun_inbound_router idempotency-key fallback hash.

PR review finding: the fallback hash (used when Mailgun sends no Message-Id,
a supported case for HTML-only portal notifications — see
EmailParts.text_body) only covered sender/subject/body_plain. Two distinct
HTML-only messages sharing sender+subject hashed identically and the second
was silently treated as a duplicate and dropped. Covered here as a pure
unit test since _content_hash has no DB/FastAPI dependency.
"""

import hashlib
import hmac
from unittest.mock import MagicMock, patch

from pydantic import SecretStr

from src.api.mailgun_inbound_router import _content_hash, _verify_mailgun_signature


def test_distinct_html_only_bodies_hash_differently():
    # Same sender/subject, empty body_plain (HTML-only), different HTML content.
    h1 = _content_hash("notify@portal.example", "New Lead", "", "<p>Lead A</p>")
    h2 = _content_hash("notify@portal.example", "New Lead", "", "<p>Lead B</p>")
    assert h1 != h2


def test_same_inputs_hash_identically():
    h1 = _content_hash("notify@portal.example", "New Lead", "", "<p>Lead A</p>")
    h2 = _content_hash("notify@portal.example", "New Lead", "", "<p>Lead A</p>")
    assert h1 == h2


def test_missing_html_does_not_crash():
    assert _content_hash("s", "subj", "plain body", None)


# ---------------------------------------------------------------------------
# Group D / D-2 — this router now reads the SAME setting
# (settings.mailgun_signing_key) as src/api/inbound_email_router.py, not its
# own separate mailgun_webhook_signing_key. Two settings for one Mailgun
# account key meant setting only one silently disabled the other endpoint.
# ---------------------------------------------------------------------------

def _settings_with_key(key):
    settings = MagicMock()
    settings.mailgun_signing_key = SecretStr(key) if key else None
    return settings


def test_valid_signature_accepted_via_shared_setting():
    key = "shared-mailgun-secret"
    ts, token = "1234567890", "abc123"
    sig = hmac.new(key.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    with patch("src.api.mailgun_inbound_router.get_settings", return_value=_settings_with_key(key)):
        assert _verify_mailgun_signature(ts, token, sig) is True


def test_invalid_signature_rejected():
    with patch("src.api.mailgun_inbound_router.get_settings", return_value=_settings_with_key("shared-mailgun-secret")):
        assert _verify_mailgun_signature("1234567890", "abc123", "wrong-signature") is False


def test_unset_signing_key_rejects_everything():
    """Fail-closed: no key configured must reject, never accept."""
    with patch("src.api.mailgun_inbound_router.get_settings", return_value=_settings_with_key(None)):
        assert _verify_mailgun_signature("1234567890", "abc123", "anything") is False
