"""Unit tests for src.services.slack.auth — no live DB, no live Slack."""

from unittest.mock import MagicMock

import pytest

from src.services.slack import auth
from tests.helpers.slack_signing import DEFAULT_TEST_SECRET, sign_interactive_payload


@pytest.fixture
def signed_settings(monkeypatch):
	"""Patches src.services.slack.auth's get_settings() return value —
	patching the module's own binding (not config.settings.settings
	directly) is immune to another test's config.settings reload
	desyncing this one, per FA/tests/test_slack_interact_endpoint.py:50-61."""
	mock_settings = MagicMock()
	mock_settings.slack_signing_secret = MagicMock(get_secret_value=lambda: DEFAULT_TEST_SECRET)
	mock_settings.blackink_global_approvers = ()
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	return mock_settings


# ── verify_slack_signature ───────────────────────────────────────────────


def test_valid_signature_passes(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"})
	assert auth.verify_slack_signature(headers, body) is True


def test_bad_signature_rejected(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"}, bad_signature=True)
	assert auth.verify_slack_signature(headers, body) is False


def test_missing_signature_header_rejected(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"})
	headers = dict(headers)
	del headers["x-slack-signature"]
	assert auth.verify_slack_signature(headers, body) is False


def test_replay_beyond_window_rejected(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"}, ts_offset=-400)
	assert auth.verify_slack_signature(headers, body) is False


def test_future_timestamp_beyond_window_rejected(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"}, ts_offset=400)
	assert auth.verify_slack_signature(headers, body) is False


def test_malformed_timestamp_rejected(signed_settings):
	body, headers = sign_interactive_payload({"type": "block_actions"})
	headers = dict(headers)
	headers["x-slack-request-timestamp"] = "not-a-number"
	assert auth.verify_slack_signature(headers, body) is False


def test_no_signing_secret_configured_rejects_everything(monkeypatch):
	mock_settings = MagicMock()
	mock_settings.slack_signing_secret = None
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	body, headers = sign_interactive_payload({"type": "block_actions"}, secret="whatever")
	assert auth.verify_slack_signature(headers, body) is False


# ── approver_authorized — FAIL CLOSED is the whole point of this module ──


def test_empty_allowlist_authorizes_nobody(monkeypatch):
	"""The FA regression test: admin_router.py:1786-1790 documents that
	`if approvers and user_id not in approvers` silently authorized EVERY
	workspace member whenever the allowlist was empty, because an empty
	list is falsy and the `if` short-circuits. This must never happen
	here — empty/unset means nobody, unconditionally."""
	mock_settings = MagicMock()
	mock_settings.blackink_global_approvers = ()
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	assert auth.approver_authorized("U_ANYONE") is False
	assert auth.approver_authorized("U_ANYONE", client_id="acme_pm") is False


def test_unset_allowlist_none_authorizes_nobody(monkeypatch):
	mock_settings = MagicMock()
	mock_settings.blackink_global_approvers = None
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	assert auth.approver_authorized("U_ANYONE") is False


def test_listed_approver_authorized(monkeypatch):
	mock_settings = MagicMock()
	mock_settings.blackink_global_approvers = ("U_APPROVER",)
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	assert auth.approver_authorized("U_APPROVER") is True


def test_non_listed_user_not_authorized(monkeypatch):
	mock_settings = MagicMock()
	mock_settings.blackink_global_approvers = ("U_APPROVER",)
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	assert auth.approver_authorized("U_SOMEONE_ELSE") is False


def test_empty_user_id_never_authorized(monkeypatch):
	mock_settings = MagicMock()
	mock_settings.blackink_global_approvers = ("",)  # pathological config, must not match
	monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
	assert auth.approver_authorized("") is False
