"""Session-based auth: bcrypt passwords, random session tokens."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta

import db

SESSION_TTL_DAYS = 30


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_session(user_id: str) -> str:
    token      = secrets.token_hex(32)
    expires_at = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    db.create_session(token, user_id, expires_at)
    return token


def get_user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    return db.get_user_by_session(token)
