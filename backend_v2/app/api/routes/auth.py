from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context, get_current_user, get_email_service
from app.core.db import get_db
from app.models.identity import User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    OtpChallenge,
    RegisterRequest,
    RequestLoginCodeRequest,
    ResendOtpRequest,
    SecuritySettingsRequest,
    SessionResponse,
    UserOut,
    VerifyOtpRequest,
)
from app.services.auth_service import AuthService
from app.services.email import EmailService

router = APIRouter(prefix="/auth", tags=["auth"])


def _service(session: Session, email: EmailService) -> AuthService:
    return AuthService(session, email)


@router.post(
    "/register",
    response_model=OtpChallenge,
    status_code=status.HTTP_202_ACCEPTED,
)
def register(
    payload: RegisterRequest,
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> OtpChallenge:
    """Accept a signup request without confirming whether the email is known."""
    return _service(session, email).register(payload)


@router.post("/verify-otp", response_model=AuthResponse)
def verify_otp(
    payload: VerifyOtpRequest,
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> AuthResponse:
    return _service(session, email).verify_otp(
        email=payload.email, code=payload.code, purpose=payload.purpose
    )


@router.post(
    "/resend-otp",
    response_model=OtpChallenge,
    status_code=status.HTTP_202_ACCEPTED,
)
def resend_otp(
    payload: ResendOtpRequest,
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> OtpChallenge:
    return _service(session, email).resend(payload)


@router.post(
    "/request-login-code",
    response_model=OtpChallenge,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_login_code(
    payload: RequestLoginCodeRequest,
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> OtpChallenge:
    return _service(session, email).request_login_code(payload.email)


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> AuthResponse:
    return _service(session, email).login(payload)


@router.get("/me", response_model=SessionResponse)
def me(context: AuthContext = Depends(get_auth_context)) -> SessionResponse:
    from app.schemas.auth import CompanyOut

    return SessionResponse(
        user=UserOut.model_validate(context.user),
        company=CompanyOut.model_validate(context.company),
    )


@router.patch("/security", response_model=UserOut)
def update_security(
    payload: SecuritySettingsRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    email: EmailService = Depends(get_email_service),
) -> UserOut:
    return _service(session, email).update_security(user=user, payload=payload)
