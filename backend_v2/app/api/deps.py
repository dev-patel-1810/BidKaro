from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import TokenError, decode_access_token
from app.integrations.ai import AIProvider, build_ai_provider
from app.models.identity import Company, User
from app.services.email import EmailService

bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_email_service() -> EmailService:
    return EmailService()


@lru_cache(maxsize=1)
def get_ai_provider() -> AIProvider:
    return build_ai_provider()


@dataclass(slots=True)
class AuthContext:
    user: User
    company: Company


def _unauthorised(detail: str = "Sign in to continue.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise _unauthorised()
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = payload["sub"]
    except (TokenError, KeyError):
        raise _unauthorised("Your session has expired. Sign in again.")

    from uuid import UUID

    try:
        user_uuid = UUID(str(user_id))
    except ValueError:
        raise _unauthorised("Your session is not valid. Sign in again.")

    user = session.get(User, user_uuid)
    if user is None or not user.is_active:
        raise _unauthorised("Your account is not available.")
    return user


def get_current_company(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> Company:
    # Tenancy is reloaded from the user row. The token's company_id claim is
    # never trusted for authorisation.
    company = session.get(Company, user.company_id)
    if company is None or not company.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Company account is unavailable.",
        )
    return company


def get_auth_context(
    user: User = Depends(get_current_user),
    company: Company = Depends(get_current_company),
) -> AuthContext:
    return AuthContext(user=user, company=company)
