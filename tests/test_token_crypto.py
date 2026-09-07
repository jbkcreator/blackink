"""Pure unit tests for src/core/token_crypto.py — no DB required."""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.core import token_crypto


class _FakeSecretStr:
	def __init__(self, value):
		self._value = value

	def get_secret_value(self):
		return self._value


class _FakeSettings:
	def __init__(self, key=None):
		self.token_encryption_key = _FakeSecretStr(key) if key else None


def test_encrypt_decrypt_round_trip(monkeypatch):
	key = Fernet.generate_key().decode("utf-8")
	monkeypatch.setattr(token_crypto, "get_settings", lambda: _FakeSettings(key))

	ciphertext = token_crypto.encrypt_token("super-secret-refresh-token")
	assert ciphertext != "super-secret-refresh-token"
	assert token_crypto.decrypt_token(ciphertext) == "super-secret-refresh-token"


def test_decrypt_with_wrong_key_raises(monkeypatch):
	key_a = Fernet.generate_key().decode("utf-8")
	key_b = Fernet.generate_key().decode("utf-8")

	monkeypatch.setattr(token_crypto, "get_settings", lambda: _FakeSettings(key_a))
	ciphertext = token_crypto.encrypt_token("some-token")

	monkeypatch.setattr(token_crypto, "get_settings", lambda: _FakeSettings(key_b))
	with pytest.raises(InvalidToken):
		token_crypto.decrypt_token(ciphertext)


def test_unset_key_raises_clear_config_error(monkeypatch):
	monkeypatch.setattr(token_crypto, "get_settings", lambda: _FakeSettings(None))
	with pytest.raises(token_crypto.TokenEncryptionNotConfiguredError):
		token_crypto.encrypt_token("anything")
	with pytest.raises(token_crypto.TokenEncryptionNotConfiguredError):
		token_crypto.decrypt_token("anything")
