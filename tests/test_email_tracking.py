"""Unit tests for S-8's email_tracking.py (pixel/click token mint+verify)
and its wiring into the public router (src/api/email_tracking_router.py).
No DB/network: token tests are pure PyJWT round-trips; router tests patch
get_db_context/log_event."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import jwt
import pytest

from config.settings import get_settings
from src.services import email_tracking as et


@pytest.fixture(autouse=True)
def _tracking_secret(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("EMAIL_TRACKING_SECRET", "test-secret-do-not-use-in-prod")
    yield
    get_settings.cache_clear()


# ── pixel tokens ──────────────────────────────────────────────────────────

def test_pixel_token_round_trips():
    token = et.mint_pixel_token("acme_pm", 42, "dispatch-1")
    claims = et.verify_pixel_token(token)
    assert claims == et.PixelClaims(client_id="acme_pm", contact_id=42, dispatch_id="dispatch-1")


def test_pixel_token_rejects_garbage():
    assert et.verify_pixel_token("not-a-jwt") is None


def test_pixel_token_rejects_expired(monkeypatch):
    from datetime import timedelta

    token = et.mint_pixel_token("acme_pm", 42, "dispatch-1", expires_in=timedelta(seconds=-1))
    assert et.verify_pixel_token(token) is None


def test_pixel_token_cannot_be_verified_as_a_click_token():
    """Token-type discriminator must actually be checked, not just present."""
    pixel_token = et.mint_pixel_token("acme_pm", 42, "dispatch-1")
    assert et.verify_click_token(pixel_token) is None


# ── click tokens ──────────────────────────────────────────────────────────

def test_click_token_round_trips():
    token = et.mint_click_token("acme_pm", 42, "dispatch-1", "https://example.com/report")
    claims = et.verify_click_token(token)
    assert claims == et.ClickClaims(
        client_id="acme_pm", contact_id=42, dispatch_id="dispatch-1", target_url="https://example.com/report"
    )


def test_click_token_cannot_be_verified_as_a_pixel_token():
    click_token = et.mint_click_token("acme_pm", 42, "dispatch-1", "https://example.com")
    assert et.verify_pixel_token(click_token) is None


def test_click_target_url_is_whatever_was_signed_not_request_supplied():
    """The click endpoint never takes a redirect target from the request —
    only from inside the token. Confirms the claim really does carry it."""
    token = et.mint_click_token("acme_pm", 1, "d1", "https://malicious.example.com")
    claims = et.verify_click_token(token)
    assert claims.target_url == "https://malicious.example.com"  # signed by US at send time, not attacker input


# ── html_body_with_pixel ────────────────────────────────────────────────────

def test_html_body_with_pixel_embeds_img_tag():
    html = et.html_body_with_pixel("Hi there,\nBest,\nThe team", "https://x.example/pixel?token=abc")
    assert '<img src="https://x.example/pixel?token=abc"' in html
    assert "Hi there,<br>" in html


def test_html_body_with_pixel_escapes_html_in_body():
    html = et.html_body_with_pixel("<script>alert(1)</script>", "https://x.example/pixel")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── wrap_link ────────────────────────────────────────────────────────────

def test_wrap_link_produces_a_verifiable_click_url():
    url = et.wrap_link("acme_pm", 1, "d1", "https://real-target.example.com")
    token = url.split("token=")[1]
    claims = et.verify_click_token(token)
    assert claims.target_url == "https://real-target.example.com"
