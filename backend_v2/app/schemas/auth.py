from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    region: str | None = None
    sectors: list[str] = []
    services: list[str] = []
    keywords: list[str] = []
    profile: dict = {}
    is_active: bool
    reminder_opt_in: bool
    reminder_lead_days: list[int]
    created_at: datetime
    updated_at: datetime


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    email_verified: bool
    email_otp_enabled: bool
    is_active: bool
    created_at: datetime


class RegisterRequest(BaseModel):
    email: EmailStr
    company_name: str = Field(min_length=1, max_length=200)
    full_name: str | None = Field(default=None, max_length=200)
    # Optional so a code-only account is possible.  The account can set a
    # password later through PATCH /api/auth/security.
    password: str | None = Field(default=None, min_length=8, max_length=72)

    @model_validator(mode="after")
    def normalise(self) -> RegisterRequest:
        self.email = str(self.email).strip().lower()  # type: ignore[assignment]
        self.company_name = self.company_name.strip()
        if self.full_name is not None:
            self.full_name = self.full_name.strip() or None
        return self


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)

    @model_validator(mode="after")
    def normalise(self) -> LoginRequest:
        self.email = str(self.email).strip().lower()  # type: ignore[assignment]
        return self


class RequestLoginCodeRequest(BaseModel):
    email: EmailStr

    @model_validator(mode="after")
    def normalise(self) -> RequestLoginCodeRequest:
        self.email = str(self.email).strip().lower()  # type: ignore[assignment]
        return self


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    purpose: Literal["signup", "verify_email", "login"] = "signup"

    @model_validator(mode="after")
    def normalise(self) -> VerifyOtpRequest:
        self.email = str(self.email).strip().lower()  # type: ignore[assignment]
        if self.purpose == "verify_email":
            self.purpose = "signup"
        return self


class ResendOtpRequest(BaseModel):
    email: EmailStr
    purpose: Literal["signup", "verify_email", "login"] = "signup"

    @model_validator(mode="after")
    def normalise(self) -> ResendOtpRequest:
        self.email = str(self.email).strip().lower()  # type: ignore[assignment]
        if self.purpose == "verify_email":
            self.purpose = "signup"
        return self


class SecuritySettingsRequest(BaseModel):
    email_otp_enabled: bool | None = None
    current_password: str | None = Field(default=None, max_length=72)
    code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str | None = Field(default=None, min_length=8, max_length=72)

    @model_validator(mode="after")
    def require_a_change(self) -> SecuritySettingsRequest:
        if (
            self.email_otp_enabled is None
            and self.new_password is None
        ):
            raise ValueError("Provide a security setting to change.")
        if self.new_password is not None and not (
            self.current_password or self.code
        ):
            raise ValueError("Changing the password needs the current password or an email code.")
        return self


class OtpChallenge(BaseModel):
    otp_required: bool = True
    email: EmailStr
    purpose: Literal["signup", "login"]
    message: str
    expires_in_seconds: int
    resend_after_seconds: int


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    company: CompanyOut


class SessionResponse(BaseModel):
    user: UserOut
    company: CompanyOut
