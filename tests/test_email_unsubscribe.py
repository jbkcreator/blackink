"""Tests for email_unsubscribe.py — the mandatory one-click unsubscribe
token/link mechanism (CLAUDE.md's "Outbound email — mandatory one-click
unsubscribe" invariant, Subtask 3.1.2)."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.services.email_unsubscribe import (
	append_unsubscribe_footer,
	mint_unsubscribe_token,
	unsubscribe_url,
	verify_unsubscribe_token,
)


def _fake_settings(secret="test-secret", app_base_url="https://app.example.com"):
	settings = MagicMock()
	settings.email_unsubscribe_secret.get_secret_value.return_value = secret
	settings.app_base_url = app_base_url
	return settings


def test_mint_and_verify_round_trip():
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings()):
		token = mint_unsubscribe_token("client_a", "Owner@Example.com")
		parsed = verify_unsubscribe_token(token)
	assert parsed == ("client_a", "owner@example.com")  # lowercased at mint time


def test_verify_rejects_garbage_token():
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings()):
		assert verify_unsubscribe_token("not-a-real-token") is None


def test_verify_rejects_expired_token():
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings()):
		token = mint_unsubscribe_token("client_a", "owner@example.com", expires_in=timedelta(seconds=-1))
		assert verify_unsubscribe_token(token) is None


def test_verify_rejects_token_signed_with_different_secret():
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings(secret="secret-one")):
		token = mint_unsubscribe_token("client_a", "owner@example.com")
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings(secret="secret-two")):
		assert verify_unsubscribe_token(token) is None


def test_missing_secret_raises_rather_than_shipping_unprotected():
	settings = MagicMock()
	settings.email_unsubscribe_secret = None
	with patch("src.services.email_unsubscribe.get_settings", return_value=settings):
		with pytest.raises(RuntimeError):
			mint_unsubscribe_token("client_a", "owner@example.com")


def test_unsubscribe_url_embeds_public_endpoint_and_token():
	with patch("src.services.email_unsubscribe.get_settings", return_value=_fake_settings()):
		url = unsubscribe_url("client_a", "owner@example.com")
	assert url.startswith("https://app.example.com/api/v1/public/unsubscribe?token=")


def test_append_unsubscribe_footer_preserves_body_and_adds_link():
	body = "Hi there,\n\nOriginal message."
	footer_body = append_unsubscribe_footer(body, "https://app.example.com/api/v1/public/unsubscribe?token=abc")
	assert body in footer_body
	assert "https://app.example.com/api/v1/public/unsubscribe?token=abc" in footer_body
