"""Shared FastAPI dependencies — mirrors Forced Action's deps.py convention
(get_db imported by every router)."""

import secrets
from typing import Generator, Optional

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.auth import decode_scoped_token


def get_db() -> Generator[Session, None, None]:
    with get_db_context() as session:
        yield session


def get_current_akrash(authorization: Optional[str] = Header(default=None)) -> str:
    """Validates a scope='akrash' bearer token. Raises 403 on anything else
    — mirrors FA's get_current_admin rejecting scope != 'demo'."""
    settings = get_settings()
    secret = settings.akrash_ingest_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Akrash ingestion auth is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    payload = decode_scoped_token(token, secret.get_secret_value(), required_scope="akrash")
    if payload is None:
        raise HTTPException(status_code=403, detail="Invalid or wrong-scope token")
    return payload["sub"]


def require_admin_jwt(authorization: Optional[str] = Header(default=None)) -> dict:
    """Validates an admin JWT for internal dashboard routes (sandbox, metrics, meetings)."""
    settings = get_settings()
    secret = settings.admin_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Admin JWT secret not configured on server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, secret.get_secret_value(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload
