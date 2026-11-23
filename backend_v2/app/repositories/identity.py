from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.identity import AuditLog, Company, OtpCode, User


class CompanyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, company_id: uuid.UUID) -> Company | None:
        return self.session.get(Company, company_id)

    def create(
        self,
        *,
        name: str,
        region: str | None = None,
        sectors: list[str] | None = None,
        services: list[str] | None = None,
        keywords: list[str] | None = None,
        profile: dict | None = None,
    ) -> Company:
        company = Company(
            name=name,
            region=region,
            sectors=sectors or [],
            services=services or [],
            keywords=keywords or [],
            profile=profile or {},
        )
        self.session.add(company)
        self.session.flush()
        return company

    def update(self, company: Company, values: dict) -> Company:
        for key, value in values.items():
            setattr(company, key, value)
        company.updated_at = utcnow()
        self.session.flush()
        return company


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(User.email == email.lower().strip()))

    def create(
        self,
        *,
        company_id: uuid.UUID,
        email: str,
        password_hash: str | None,
        full_name: str | None,
        email_verified: bool = False,
        email_otp_enabled: bool = False,
    ) -> User:
        user = User(
            company_id=company_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            full_name=full_name,
            email_verified=email_verified,
            email_otp_enabled=email_otp_enabled,
        )
        self.session.add(user)
        self.session.flush()
        return user

    def mark_verified(self, user: User) -> User:
        user.email_verified = True
        user.updated_at = utcnow()
        self.session.flush()
        return user

    def set_email_otp_enabled(self, user: User, enabled: bool) -> User:
        user.email_otp_enabled = enabled
        user.updated_at = utcnow()
        self.session.flush()
        return user

    def set_password_hash(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        user.updated_at = utcnow()
        self.session.flush()
        return user

    def touch_login(self, user: User) -> User:
        user.last_login_at = utcnow()
        user.updated_at = utcnow()
        self.session.flush()
        return user


class OtpRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest(self, email: str, purpose: str) -> OtpCode | None:
        return self.session.scalar(
            select(OtpCode)
            .where(OtpCode.email == email.lower().strip(), OtpCode.purpose == purpose)
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )

    def find_active(self, email: str, purpose: str, now: datetime) -> OtpCode | None:
        return self.session.scalar(
            select(OtpCode)
            .where(
                OtpCode.email == email.lower().strip(),
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
                OtpCode.expires_at > now,
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )

    def count_since(self, email: str, purpose: str, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count(OtpCode.id)).where(
                    OtpCode.email == email.lower().strip(),
                    OtpCode.purpose == purpose,
                    OtpCode.created_at >= since,
                )
            )
            or 0
        )

    def create(
        self,
        *,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: datetime,
        payload: dict | None = None,
        user_id: uuid.UUID | None = None,
    ) -> OtpCode:
        otp = OtpCode(
            email=email.lower().strip(),
            purpose=purpose,
            code_hash=code_hash,
            expires_at=expires_at,
            payload=payload or {},
            user_id=user_id,
        )
        self.session.add(otp)
        self.session.flush()
        return otp

    def consume(self, otp: OtpCode) -> None:
        otp.consumed_at = utcnow()
        self.session.flush()

    def increment_attempts(self, otp: OtpCode) -> int:
        otp.attempts += 1
        self.session.flush()
        return otp.attempts

    def invalidate_active(self, email: str, purpose: str, now: datetime) -> None:
        active = self.session.scalars(
            select(OtpCode).where(
                OtpCode.email == email.lower().strip(),
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
                OtpCode.expires_at > now,
            )
        ).all()
        for otp in active:
            otp.consumed_at = now
        self.session.flush()

    def delete(self, otp: OtpCode) -> None:
        self.session.delete(otp)
        self.session.flush()


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self,
        *,
        action: str,
        entity_type: str,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        entity_id: uuid.UUID | None = None,
        before: dict | None = None,
        after: dict | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            company_id=company_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
        self.session.add(entry)
        self.session.flush()
        return entry
