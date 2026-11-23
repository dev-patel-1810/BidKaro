from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.errors import AppError, BadRequest, Conflict, RateLimited, ServiceUnavailable
from app.core.security import create_access_token, hash_password, verify_password
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.identity import Company, OtpCode, User
from app.repositories.identity import (
    AuditRepository,
    CompanyRepository,
    OtpRepository,
    UserRepository,
)
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    OtpChallenge,
    RegisterRequest,
    ResendOtpRequest,
    SecuritySettingsRequest,
    SessionResponse,
    UserOut,
)
from app.services.email import EmailService


def _code_hash(email: str, purpose: str, code: str) -> str:
    secret = get_settings().jwt_secret.encode("utf-8")
    message = f"{email.lower().strip()}:{purpose}:{code}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class OtpService:
    def __init__(self, session: Session, email: EmailService) -> None:
        self.settings = get_settings()
        self.repository = OtpRepository(session)
        self.email = email

    def _challenge(
        self,
        email: str,
        purpose: str,
        message: str,
        resend_after: int | None = None,
    ) -> OtpChallenge:
        return OtpChallenge(
            email=email,
            purpose=purpose,
            message=message,
            expires_in_seconds=self.settings.otp_expire_minutes * 60,
            resend_after_seconds=(
                self.settings.otp_resend_cooldown_seconds
                if resend_after is None
                else max(resend_after, 0)
            ),
        )

    def generic(self, email: str, purpose: str) -> OtpChallenge:
        """The same answer for a known and an unknown address."""
        return self._challenge(
            email,
            purpose,
            "If this address can receive a code, it will arrive shortly.",
        )

    def issue(
        self,
        *,
        email: str,
        purpose: str,
        payload: dict | None = None,
        user_id=None,
    ) -> OtpChallenge:
        if not self.email.configured:
            raise ServiceUnavailable(
                "Email verification is not configured on the server. "
                "Set SMTP_USERNAME and SMTP_APP_PASSWORD."
            )

        now = utcnow()
        latest = self.repository.latest(email, purpose)
        if latest is not None:
            elapsed = (now - latest.created_at).total_seconds()
            wait = math.ceil(self.settings.otp_resend_cooldown_seconds - elapsed)
            if wait > 0:
                raise RateLimited(
                    f"Please wait {wait} seconds before requesting another code."
                )

        sent_last_hour = self.repository.count_since(
            email, purpose, now - timedelta(hours=1)
        )
        if sent_last_hour >= self.settings.otp_max_sends_per_hour:
            raise RateLimited(
                "Too many codes were requested for this email. Try again in an hour."
            )

        code = f"{secrets.randbelow(900000) + 100000:06d}"
        self.repository.invalidate_active(email, purpose, now)
        otp = self.repository.create(
            email=email,
            purpose=purpose,
            code_hash=_code_hash(email, purpose, code),
            expires_at=now + timedelta(minutes=self.settings.otp_expire_minutes),
            payload=payload,
            user_id=user_id,
        )
        try:
            self.email.send_code(
                to=email,
                code=code,
                purpose=purpose,
                expires_minutes=self.settings.otp_expire_minutes,
            )
        except Exception:
            # Never keep a code the user never received.
            self.repository.delete(otp)
            raise

        return self._challenge(
            email,
            purpose,
            "We emailed a 6-digit code. It expires in "
            f"{self.settings.otp_expire_minutes} minutes.",
        )

    def verify(self, *, email: str, purpose: str, code: str) -> OtpCode:
        now = utcnow()
        active = self.repository.find_active(email, purpose, now)
        if active is None:
            raise AppError(
                400, "This code has expired or was already used. Request a new one."
            )

        attempts = self.repository.increment_attempts(active)
        if not hmac.compare_digest(active.code_hash, _code_hash(email, purpose, code)):
            remaining = max(self.settings.otp_max_attempts - attempts, 0)
            if remaining == 0:
                self.repository.invalidate_active(email, purpose, now)
                raise RateLimited("Too many incorrect attempts. Request a new code.")
            raise AppError(400, f"Incorrect code. {remaining} attempts left.")

        self.repository.consume(active)
        return active


class AuthService:
    def __init__(
        self,
        session: Session,
        email: EmailService,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.users = UserRepository(session)
        self.companies = CompanyRepository(session)
        self.audit = AuditRepository(session)
        self.email = email
        self.otp = OtpService(session, email)

    def register(self, payload: RegisterRequest) -> OtpChallenge:
        # Check the integration before the account lookup so a broken server is
        # reported honestly and the same way for every address.
        if not self.email.configured:
            raise ServiceUnavailable(
                "Email verification is not configured on the server. "
                "Set SMTP_USERNAME and SMTP_APP_PASSWORD."
            )

        existing = self.users.get_by_email(payload.email)
        if existing is not None:
            # Generic answer: never confirm whether an address is registered.
            return self.otp.generic(payload.email, "signup")

        # A pending signup already has a live code.  Answer generically rather
        # than sending again or exposing the resend cooldown.
        pending = self.otp.repository.find_active(payload.email, "signup", utcnow())
        if pending is not None:
            return self.otp.generic(payload.email, "signup")

        password_hash = None
        if payload.password is not None:
            try:
                password_hash = hash_password(payload.password)
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc

        try:
            self.otp.issue(
                email=payload.email,
                purpose="signup",
                payload={
                    "company_name": payload.company_name,
                    "full_name": payload.full_name,
                    "password_hash": password_hash,
                },
            )
            self.audit.log(
                action="auth.signup_requested",
                entity_type="otp",
                after={"email": payload.email},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        # The same sentence for a new, pending and already-registered address.
        return self.otp.generic(payload.email, "signup")

    def verify_signup(self, *, email: str, code: str) -> AuthResponse:
        try:
            otp = self.otp.verify(email=email, purpose="signup", code=code)
            existing = self.users.get_by_email(email)
            if existing is not None:
                if not existing.email_verified:
                    self.users.mark_verified(existing)
                user = existing
                company = self.companies.get(user.company_id)
                if company is None or not company.is_active:
                    raise AppError(403, "Company account is unavailable.")
            else:
                payload = otp.payload or {}
                company_name = str(payload.get("company_name") or "").strip()
                if not company_name:
                    raise AppError(
                        400,
                        "The signup details are missing. Start the signup again.",
                    )
                company = self.companies.create(name=company_name)
                user = self.users.create(
                    company_id=company.id,
                    email=email,
                    password_hash=payload.get("password_hash"),
                    full_name=payload.get("full_name"),
                    email_verified=True,
                    email_otp_enabled=True,
                )
                self.audit.log(
                    company_id=company.id,
                    actor_user_id=user.id,
                    action="auth.registered",
                    entity_type="user",
                    entity_id=user.id,
                    after={"company_name": company.name},
                )

            self.users.touch_login(user)
            self.session.commit()
            self.session.refresh(user)
            self.session.refresh(company)
            return self._auth_response(user, company)
        except Exception:
            self.session.rollback()
            raise

    def verify_login(self, *, email: str, code: str) -> AuthResponse:
        user = self.users.get_by_email(email)
        if user is None or not user.is_active:
            raise AppError(400, "Invalid or expired code.")
        if not user.email_verified:
            raise AppError(403, "Verify your email address before signing in.")
        if not user.email_otp_enabled:
            raise AppError(403, "Email sign-in is not enabled for this account.")

        try:
            self.otp.verify(email=email, purpose="login", code=code)
            company = self.companies.get(user.company_id)
            if company is None or not company.is_active:
                raise AppError(403, "Company account is unavailable.")
            self.users.touch_login(user)
            self.audit.log(
                company_id=company.id,
                actor_user_id=user.id,
                action="auth.login_otp",
                entity_type="user",
                entity_id=user.id,
            )
            self.session.commit()
            self.session.refresh(user)
            self.session.refresh(company)
            return self._auth_response(user, company)
        except Exception:
            self.session.rollback()
            raise

    def login(self, payload: LoginRequest) -> AuthResponse:
        user = self.users.get_by_email(payload.email)
        if user is None or not verify_password(payload.password, user.password_hash):
            raise AppError(401, "Incorrect email or password.")
        if not user.email_verified:
            raise AppError(403, "Verify your email address before signing in.")
        if not user.is_active:
            raise AppError(403, "This account is inactive.")

        company = self.companies.get(user.company_id)
        if company is None or not company.is_active:
            raise AppError(403, "Company account is unavailable.")

        self.users.touch_login(user)
        self.audit.log(
            company_id=company.id,
            actor_user_id=user.id,
            action="auth.login_password",
            entity_type="user",
            entity_id=user.id,
        )
        self.session.commit()
        self.session.refresh(user)
        self.session.refresh(company)
        return self._auth_response(user, company)

    def request_login_code(self, email: str) -> OtpChallenge:
        user = self.users.get_by_email(email)
        eligible = (
            user is not None
            and user.is_active
            and user.email_verified
            and user.email_otp_enabled
        )
        if not eligible:
            return self.otp.generic(email, "login")
        challenge = self.otp.issue(email=email, purpose="login", user_id=user.id)
        self.session.commit()
        return challenge

    def resend(self, payload: ResendOtpRequest) -> OtpChallenge:
        if payload.purpose == "signup":
            if self.users.get_by_email(payload.email) is not None:
                return self.otp.generic(payload.email, "signup")
            pending = self.otp.repository.find_active(
                payload.email, "signup", utcnow()
            )
            if pending is None:
                return self.otp.generic(payload.email, "signup")
            challenge = self.otp.issue(
                email=payload.email,
                purpose="signup",
                payload=pending.payload,
            )
            self.session.commit()
            return challenge

        user = self.users.get_by_email(payload.email)
        eligible = (
            user is not None
            and user.is_active
            and user.email_verified
            and user.email_otp_enabled
        )
        if not eligible:
            return self.otp.generic(payload.email, "login")
        challenge = self.otp.issue(email=payload.email, purpose="login", user_id=user.id)
        self.session.commit()
        return challenge

    def verify_otp(self, *, email: str, code: str, purpose: str) -> AuthResponse:
        if purpose == "signup":
            return self.verify_signup(email=email, code=code)
        return self.verify_login(email=email, code=code)

    def update_security(
        self, *, user: User, payload: SecuritySettingsRequest
    ) -> UserOut:
        if not (payload.current_password or payload.code):
            raise BadRequest(
                "Confirm the change with the current password or an email code."
            )

        if payload.current_password is not None:
            if not verify_password(payload.current_password, user.password_hash):
                raise AppError(401, "Current password is incorrect.")
        else:
            try:
                self.otp.verify(
                    email=user.email, purpose="login", code=payload.code or ""
                )
            except AppError:
                raise
            except Exception:
                self.session.rollback()
                raise

        if payload.email_otp_enabled is False and user.password_hash is None:
            if payload.new_password is None:
                raise Conflict(
                    "Set a password before disabling email code sign-in."
                )
        if payload.email_otp_enabled is True and not user.email_verified:
            raise BadRequest("Verify your email address before enabling email sign-in.")

        before = {
            "email_otp_enabled": user.email_otp_enabled,
            "password_set": user.password_hash is not None,
        }

        try:
            if payload.new_password is not None:
                try:
                    self.users.set_password_hash(
                        user, hash_password(payload.new_password)
                    )
                except ValueError as exc:
                    raise BadRequest(str(exc)) from exc
            if payload.email_otp_enabled is not None:
                self.users.set_email_otp_enabled(user, payload.email_otp_enabled)
            self.audit.log(
                company_id=user.company_id,
                actor_user_id=user.id,
                action="auth.security_changed",
                entity_type="user",
                entity_id=user.id,
                before=before,
                after={
                    "email_otp_enabled": user.email_otp_enabled,
                    "password_set": user.password_hash is not None,
                },
            )
            self.session.commit()
            self.session.refresh(user)
        except Exception:
            self.session.rollback()
            raise
        return UserOut.model_validate(user)

    def session(self, user: User, company: Company) -> SessionResponse:
        return SessionResponse(
            user=UserOut.model_validate(user),
            company=company_to_out(company),
        )

    def _auth_response(self, user: User, company: Company) -> AuthResponse:
        return AuthResponse(
            access_token=create_access_token(user.id, company.id),
            user=UserOut.model_validate(user),
            company=company_to_out(company),
        )


def company_to_out(company: Company):
    from app.schemas.auth import CompanyOut

    return CompanyOut.model_validate(company)
