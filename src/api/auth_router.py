"""Admin authentication — login and token refresh for the internal dashboard."""
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_db_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest):
    settings = get_settings()
    secret = settings.admin_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Admin JWT secret not configured on server")

    # Fetch stored hash — uses blackink_app (RLS) but admin_users has no RLS policy,
    # so no client_id is needed; pass None to avoid the SET LOCAL call.
    with get_db_context() as db:
        row = db.execute(
            text("SELECT password_hash FROM admin_users WHERE username = :u"),
            {"u": req.username},
        ).fetchone()

    if not row or not bcrypt.checkpw(req.password.encode(), row[0].encode()):
        logger.warning("[auth] failed login attempt for username=%r", req.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    expiry = datetime.now(tz=timezone.utc) + timedelta(hours=settings.admin_jwt_expiry_hours)
    token = jwt.encode(
        {"sub": req.username, "exp": expiry},
        secret.get_secret_value(),
        algorithm="HS256",
    )

    logger.info("[auth] login success username=%r", req.username)
    return {"access_token": token, "token_type": "bearer", "expires_at": expiry.isoformat()}
