"""Application-layer encryption for tokens/secrets stored at rest —
OAuth access/refresh tokens on calendar_connections, SMTP passwords on
mailboxes. No per-row DB encryption (pgcrypto or otherwise) existed
anywhere in this repo before Subtask 3.2.1; this is a new, isolated
primitive, not an extension of an existing one.

Uses cryptography.fernet.Fernet (authenticated symmetric encryption) keyed
by settings.token_encryption_key — a urlsafe-base64-encoded 32-byte key,
generated once with Fernet.generate_key() and stored as an env var, never
committed. Decrypting with the wrong/rotated key raises InvalidToken
rather than silently returning garbage, by Fernet's own design.
"""

from cryptography.fernet import Fernet, InvalidToken

from config.settings import get_settings


class TokenEncryptionNotConfiguredError(RuntimeError):
	"""Raised when TOKEN_ENCRYPTION_KEY is unset — a clear config error
	instead of an obscure Fernet stack trace deep inside a caller."""


def _fernet() -> Fernet:
	settings = get_settings()
	key = settings.token_encryption_key
	if not key:
		raise TokenEncryptionNotConfiguredError(
			"TOKEN_ENCRYPTION_KEY is not configured - cannot encrypt/decrypt "
			"tokens. Generate one with Fernet.generate_key() and set it as an "
			"env var; never commit it."
		)
	return Fernet(key.get_secret_value().encode("utf-8"))


def encrypt_token(plaintext: str) -> str:
	return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
	try:
		return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
	except InvalidToken as exc:
		raise InvalidToken("Token could not be decrypted - wrong or rotated TOKEN_ENCRYPTION_KEY") from exc
