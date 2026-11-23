from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.settings import get_settings


class TokenError(Exception):
    pass


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password must be at most 72 bytes.")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: UUID, company_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        # The claim is carried for clients that want to display tenancy, but
        # the server reloads the user/company and never authorises from it.
        "company_id": str(company_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except InvalidTokenError as exc:
        raise TokenError("Invalid or expired token.") from exc
    subject = payload.get("sub")
    if not subject or not isinstance(subject, str):
        raise TokenError("Token is missing a subject.")
    return payload
