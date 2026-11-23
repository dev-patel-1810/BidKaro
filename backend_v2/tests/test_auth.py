from __future__ import annotations

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.identity import User
from tests.helpers import auth_header, signup, verify


def test_register_is_generic_and_verify_creates_the_account(client, fake_email):
    first = client.post(
        "/api/auth/register",
        json={
            "email": "asha@example.com",
            "company_name": "Acme Infra",
            "full_name": "Asha Rao",
            "password": "secret123",
        },
    )
    assert first.status_code == 202
    assert first.json()["purpose"] == "signup"
    assert len(fake_email.codes) == 1

    # A second register for the same pending address must not say "already
    # exists", and must not send a second code inside the cooldown.
    second = client.post(
        "/api/auth/register",
        json={
            "email": "asha@example.com",
            "company_name": "Acme Infra",
            "full_name": "Asha Rao",
            "password": "secret123",
        },
    )
    assert second.status_code == 202
    assert second.json()["message"] == first.json()["message"]
    assert len(fake_email.codes) == 1

    code = fake_email.codes[0][1]
    verified = client.post(
        "/api/auth/verify-otp",
        json={"email": "asha@example.com", "code": code, "purpose": "signup"},
    )
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["access_token"]
    assert body["user"]["email"] == "asha@example.com"
    assert body["user"]["email_verified"] is True
    assert body["company"]["name"] == "Acme Infra"

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == "asha@example.com"))
        assert user is not None
        assert user.email_verified is True

    # Now the address is known, and the answer is still generic.
    third = client.post(
        "/api/auth/register",
        json={
            "email": "asha@example.com",
            "company_name": "Acme Infra",
            "password": "secret123",
        },
    )
    assert third.status_code == 202
    assert len(fake_email.codes) == 1


def test_password_login_and_me(client, fake_email):
    verified = verify(client, fake_email, email="login@example.com")
    token = verified["access_token"]

    login = client.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "secret123"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]

    me = client.get("/api/auth/me", headers=auth_header(token))
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "login@example.com"
    assert me.json()["company"]["name"] == "Acme Infra"


def test_wrong_and_reused_code_are_rejected(client, fake_email):
    code = signup(client, fake_email, email="code@example.com")
    wrong = client.post(
        "/api/auth/verify-otp",
        json={"email": "code@example.com", "code": "000000", "purpose": "signup"},
    )
    assert wrong.status_code == 400

    first = client.post(
        "/api/auth/verify-otp",
        json={"email": "code@example.com", "code": code, "purpose": "signup"},
    )
    assert first.status_code == 200
    reused = client.post(
        "/api/auth/verify-otp",
        json={"email": "code@example.com", "code": code, "purpose": "signup"},
    )
    assert reused.status_code == 400


def test_code_only_account_can_set_a_password_and_disable_codes(client, fake_email):
    verified = verify(
        client, fake_email, email="code-only@example.com", password=None
    )
    token = verified["access_token"]

    requested = client.post(
        "/api/auth/request-login-code", json={"email": "code-only@example.com"}
    )
    assert requested.status_code == 202
    code = fake_email.codes[-1][1]

    blocked = client.patch(
        "/api/auth/security",
        headers=auth_header(token),
        json={"email_otp_enabled": False, "code": code},
    )
    assert blocked.status_code == 409, blocked.text
    assert "password" in blocked.json()["detail"].lower()

    # The conflict rolls the code back, so the same code can confirm the
    # password change.
    changed = client.patch(
        "/api/auth/security",
        headers=auth_header(token),
        json={"new_password": "newsecret123", "code": code},
    )
    assert changed.status_code == 200, changed.text

    disabled = client.patch(
        "/api/auth/security",
        headers=auth_header(token),
        json={"email_otp_enabled": False, "current_password": "newsecret123"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["email_otp_enabled"] is False

    login = client.post(
        "/api/auth/login",
        json={"email": "code-only@example.com", "password": "newsecret123"},
    )
    assert login.status_code == 200


def test_code_login_requires_an_opted_in_verified_account(client, fake_email):
    unknown = client.post(
        "/api/auth/request-login-code", json={"email": "nobody@example.com"}
    )
    assert unknown.status_code == 202
    assert len(fake_email.codes) == 0

    verified = verify(client, fake_email, email="opted@example.com")
    token = verified["access_token"]
    # The account was created with code sign-in enabled, so a code is sent.
    requested = client.post(
        "/api/auth/request-login-code", json={"email": "opted@example.com"}
    )
    assert requested.status_code == 202
    code = fake_email.codes[-1][1]
    login = client.post(
        "/api/auth/verify-otp",
        json={"email": "opted@example.com", "code": code, "purpose": "login"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]

    # Turn code sign-in off, then a code request is silently ignored.
    disabled = client.patch(
        "/api/auth/security",
        headers=auth_header(token),
        json={"email_otp_enabled": False, "current_password": "secret123"},
    )
    assert disabled.status_code == 200
    count = len(fake_email.codes)
    ignored = client.post(
        "/api/auth/request-login-code", json={"email": "opted@example.com"}
    )
    assert ignored.status_code == 202
    assert len(fake_email.codes) == count
