"""Unit tests for src/api/email_tracking_router.py's pixel/click handlers.
Calls the route functions directly (no live DB/FastAPI TestClient — matches
this repo's own convention for public routers, e.g. test_public_landing_router.py's
docstring) with a fake session standing in for get_db_context."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.api.email_tracking_router import click, pixel
from src.services.email_tracking import mint_click_token, mint_pixel_token
from config.settings import get_settings


@pytest.fixture(autouse=True)
def _tracking_secret(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("EMAIL_TRACKING_SECRET", "test-secret")
    yield
    get_settings.cache_clear()


def _fake_db(existing_event=False):
    session = MagicMock()
    result = MagicMock()
    result.first.return_value = MagicMock() if existing_event else None
    session.execute.return_value = result

    @contextmanager
    def _ctx(client_id=None):
        yield session

    return _ctx, session


# ── pixel ────────────────────────────────────────────────────────────────

def test_pixel_returns_gif_for_valid_token():
    token = mint_pixel_token("acme_pm", 1, "d1")
    ctx, session = _fake_db()
    with patch("src.api.email_tracking_router.get_db_context", ctx), \
         patch("src.api.email_tracking_router.log_event") as mock_log:
        response = pixel(token=token)
    assert response.media_type == "image/gif"
    mock_log.assert_called_once()
    assert mock_log.call_args.args[1] == "email_opened"
    assert mock_log.call_args.kwargs["payload"] == {"dispatch_id": "d1"}


def test_pixel_returns_gif_even_for_invalid_token():
    """Broken pixel must never be a visible error to the recipient's mail
    client — always a valid GIF, valid token or not."""
    with patch("src.api.email_tracking_router.log_event") as mock_log:
        response = pixel(token="garbage")
    assert response.media_type == "image/gif"
    mock_log.assert_not_called()


def test_pixel_does_not_log_a_second_open_for_the_same_dispatch():
    token = mint_pixel_token("acme_pm", 1, "d1")
    ctx, session = _fake_db(existing_event=True)
    with patch("src.api.email_tracking_router.get_db_context", ctx), \
         patch("src.api.email_tracking_router.log_event") as mock_log:
        pixel(token=token)
    mock_log.assert_not_called()


def test_pixel_never_raises_on_db_failure():
    token = mint_pixel_token("acme_pm", 1, "d1")
    with patch("src.api.email_tracking_router.get_db_context", side_effect=RuntimeError("db down")), \
         patch("src.api.email_tracking_router.log_event"):
        response = pixel(token=token)  # must not raise
    assert response.media_type == "image/gif"


# ── click ────────────────────────────────────────────────────────────────

def test_click_redirects_to_the_signed_target_url():
    token = mint_click_token("acme_pm", 1, "d1", "https://real-target.example.com")
    ctx, session = _fake_db()
    with patch("src.api.email_tracking_router.get_db_context", ctx), \
         patch("src.api.email_tracking_router.log_event") as mock_log:
        response = click(token=token)
    assert response.status_code == 302
    assert response.headers["location"] == "https://real-target.example.com"
    mock_log.assert_called_once()
    assert mock_log.call_args.args[1] == "email_clicked"


def test_click_rejects_invalid_token_with_generic_error():
    response = click(token="garbage")
    assert response.status_code == 400


def test_click_does_not_log_a_second_click_for_the_same_dispatch():
    token = mint_click_token("acme_pm", 1, "d1", "https://x.example.com")
    ctx, session = _fake_db(existing_event=True)
    with patch("src.api.email_tracking_router.get_db_context", ctx), \
         patch("src.api.email_tracking_router.log_event") as mock_log:
        click(token=token)
    mock_log.assert_not_called()


def test_click_still_redirects_even_if_the_event_write_fails():
    """A logging failure must never block the recipient from reaching the
    real link — same fail-soft posture as the pixel route."""
    token = mint_click_token("acme_pm", 1, "d1", "https://real-target.example.com")
    with patch("src.api.email_tracking_router.get_db_context", side_effect=RuntimeError("db down")):
        response = click(token=token)
    assert response.status_code == 302
    assert response.headers["location"] == "https://real-target.example.com"
