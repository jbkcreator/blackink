"""Unit test for the mailgun_inbound_router idempotency-key fallback hash.

PR review finding: the fallback hash (used when Mailgun sends no Message-Id,
a supported case for HTML-only portal notifications — see
EmailParts.text_body) only covered sender/subject/body_plain. Two distinct
HTML-only messages sharing sender+subject hashed identically and the second
was silently treated as a duplicate and dropped. Covered here as a pure
unit test since _content_hash has no DB/FastAPI dependency.
"""

from src.api.mailgun_inbound_router import _content_hash


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
