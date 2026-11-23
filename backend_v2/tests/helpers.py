from __future__ import annotations


def signup(
    client,
    fake_email,
    *,
    email: str,
    company_name: str = "Acme Infra",
    password: str | None = "secret123",
    full_name: str | None = "Asha Rao",
) -> str:
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "company_name": company_name,
            "full_name": full_name,
            "password": password,
        },
    )
    assert response.status_code == 202, response.text
    return fake_email.codes[-1][1]


def verify(
    client,
    fake_email,
    *,
    email: str,
    company_name: str = "Acme Infra",
    password: str | None = "secret123",
) -> dict:
    code = signup(
        client, fake_email, email=email, company_name=company_name, password=password
    )
    response = client.post(
        "/api/auth/verify-otp",
        json={"email": email, "code": code, "purpose": "signup"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
