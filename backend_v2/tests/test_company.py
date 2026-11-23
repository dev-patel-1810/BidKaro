from __future__ import annotations

from tests.helpers import auth_header, verify

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _upload(client, headers, *, title="Road certificate", expires_on="2030-01-01"):
    return client.post(
        "/api/company/documents",
        headers=headers,
        files={"file": ("certificate.pdf", PDF_BYTES, "application/pdf")},
        data={
            "document_type": "completion_certificate",
            "title": title,
            "issuer": "NHAI",
            "signatory": "Chief Engineer",
            "issued_on": "2025-04-01",
            "expires_on": expires_on,
            "financial_year": "2024-25",
            "project_value_amount": "2500000.50",
            "project_value_currency": "inr",
        },
    )


def test_company_profile_update(client, fake_email):
    verified = verify(client, fake_email, email="profile@example.com")
    headers = auth_header(verified["access_token"])

    empty = client.get("/api/company", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["name"] == "Acme Infra"

    updated = client.put(
        "/api/company",
        headers=headers,
        json={
            "region": "Maharashtra",
            "sectors": ["roads", "highways", "roads"],
            "services": ["construction", "maintenance"],
            "keywords": ["NH-48", "bridge"],
            "reminder_opt_in": True,
            "reminder_lead_days": [1, 7, 3, 7],
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["region"] == "Maharashtra"
    assert body["sectors"] == ["roads", "highways"]
    assert body["reminder_lead_days"] == [7, 3, 1]
    assert body["reminder_opt_in"] is True

    fetched = client.get("/api/company", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["services"] == ["construction", "maintenance"]


def test_evidence_upload_download_patch_and_soft_delete(client, fake_email):
    verified = verify(client, fake_email, email="docs@example.com")
    headers = auth_header(verified["access_token"])

    uploaded = _upload(client, headers)
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert document["sha256"]
    assert document["mime"] == "application/pdf"
    assert document["bytes"] == len(PDF_BYTES)
    assert document["project_value_currency"] == "INR"
    document_id = document["id"]

    listed = client.get("/api/company/documents", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    downloaded = client.get(
        f"/api/company/documents/{document_id}/download", headers=headers
    )
    assert downloaded.status_code == 200
    assert downloaded.content == PDF_BYTES

    patched = client.patch(
        f"/api/company/documents/{document_id}",
        headers=headers,
        json={"title": "Completed NH-48 package", "expires_on": "2031-03-31"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Completed NH-48 package"

    deleted = client.delete(
        f"/api/company/documents/{document_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert client.get("/api/company/documents", headers=headers).json()["items"] == []
    missing = client.get(
        f"/api/company/documents/{document_id}/download", headers=headers
    )
    assert missing.status_code == 404


def test_same_file_is_deduplicated(client, fake_email):
    verified = verify(client, fake_email, email="dedupe@example.com")
    headers = auth_header(verified["access_token"])

    first = _upload(client, headers)
    second = _upload(client, headers, title="The same bytes, another title")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/company/documents", headers=headers).json()["items"]) == 1


def test_another_company_cannot_see_or_change_a_document(client, fake_email):
    owner = verify(client, fake_email, email="owner@example.com")
    other = verify(client, fake_email, email="other@example.com")
    owner_headers = auth_header(owner["access_token"])
    other_headers = auth_header(other["access_token"])

    document_id = _upload(client, owner_headers).json()["id"]

    assert (
        client.get(
            f"/api/company/documents/{document_id}/download", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/company/documents/{document_id}",
            headers=other_headers,
            json={"title": "Hijacked"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/company/documents/{document_id}", headers=other_headers
        ).status_code
        == 404
    )


def test_auth_is_required(client):
    assert client.get("/api/company").status_code == 401
    assert (
        client.get(
            "/api/company", headers={"Authorization": "Bearer not-a-token"}
        ).status_code
        == 401
    )
