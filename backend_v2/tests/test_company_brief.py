"""The editable, versioned company brief and its four role groups.

These tests pin the distinction the fit ladder depends on: a confirmed
capability is core, a declared service is adjacent, a delivery phrase is
support and a bare technology word is generic recall only. A brief is history:
saving an edit writes a new version and never erases the words a past judgment
used.
"""

from __future__ import annotations

from app.discovery.roles import (
    ROLE_ADJACENT,
    ROLE_CORE,
    ROLE_GENERIC,
    ROLE_SUPPORT,
    classify_term,
    is_generic,
    is_support,
)
from tests.helpers import auth_header, verify


def test_role_rules_are_deterministic_and_narrow():
    # A delivery phrase has no specific work object left.
    assert is_support("technical support") is True
    assert classify_term("technical support") == ROLE_SUPPORT
    assert classify_term("Document retrieval") == ROLE_SUPPORT
    assert classify_term("version control") == ROLE_SUPPORT

    # Bare technology words are recall only.
    assert is_generic("technology") is True
    assert classify_term("web pages") == ROLE_GENERIC
    assert classify_term("software") == ROLE_GENERIC

    # A phrase with a real work object is not generic just because one of its
    # words is common: ``data centre`` is work, ``data`` alone is not.
    assert is_generic("data centre") is False
    assert classify_term("data centre", confirmed=True) == ROLE_CORE

    # A confirmed specific phrase is core; the same declared phrase is adjacent.
    assert classify_term("document scanning", confirmed=True) == ROLE_CORE
    assert classify_term("document scanning", confirmed=False) == ROLE_ADJACENT

    # A support phrase is never promoted to core by confirmation.
    assert classify_term("technical support", confirmed=True) == ROLE_SUPPORT
    assert classify_term("technology", confirmed=True) == ROLE_GENERIC


def _headers(client, fake_email, *, email: str = "brief@example.com"):
    owner = verify(client, fake_email, email=email)
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={
            "region": "Delhi",
            "services": ["Document scanning", "Technical support", "Technology"],
            "keywords": ["data centre"],
        },
    )
    assert profile.status_code == 200, profile.text
    return headers


def test_brief_preview_splits_the_profile_before_any_version_exists(
    client, fake_email
):
    headers = _headers(client, fake_email)

    response = client.get("/api/company/brief", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] is None
    assert body["preview"]["source_kind"] == "derived"
    assert body["preview"]["version"] is None
    terms = body["terms"]
    # A declared service is adjacent, not core: only a confirmed capability is
    # core. The delivery and bare technology words land in their own groups.
    assert "document scanning" in terms["adjacent"]
    assert "technical support" in terms["support"]
    assert "technology" in terms["generic"]
    assert "data centre" in terms["adjacent"]
    assert terms["core"] == []


def test_generate_and_save_are_additive_versions(client, fake_email):
    headers = _headers(client, fake_email, email="versions@example.com")

    generated = client.post("/api/company/brief/generate", headers=headers)
    assert generated.status_code == 200, generated.text
    first = generated.json()["brief"]
    assert first["version"] == 1
    assert first["is_current"] is True
    assert first["source_kind"] == "generated"

    saved = client.post(
        "/api/company/brief",
        headers=headers,
        json={
            "payload": {
                "summary": "We scan and digitise court records.",
                "core_terms": [{"term": "Document scanning"}],
                "adjacent_terms": [{"term": "court records"}],
                "support_terms": [{"term": "technical support"}],
                "generic_terms": [{"term": "technology"}],
                "projects": [
                    {
                        "title": "Revenue records digitisation",
                        "work_family": "document scanning",
                        "client": "Revenue Department",
                    }
                ],
                "certifications": [
                    {"title": "ISO 9001", "document_type": "iso"}
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    second = saved.json()["brief"]
    assert second["version"] == 2
    assert second["source_kind"] == "human"
    assert second["is_current"] is True
    terms = saved.json()["terms"]
    assert terms["core"] == ["document scanning"]
    assert terms["adjacent"] == ["court records"]
    assert terms["support"] == ["technical support"]
    assert terms["generic"] == ["technology"]

    versions = client.get("/api/company/brief/versions", headers=headers)
    assert versions.status_code == 200, versions.text
    rows = versions.json()["items"]
    assert [row["version"] for row in rows] == [2, 1]
    assert rows[0]["is_current"] is True
    assert rows[0]["counts"]["core"] == 1
    assert rows[0]["counts"]["projects"] == 1
    assert rows[0]["counts"]["certifications"] == 1

    # Activating the older version makes it current again; the newer version is
    # still history, not deleted.
    activated = client.post(
        f"/api/company/brief/versions/{first['id']}/activate", headers=headers
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["brief"]["version"] == 1
    assert activated.json()["brief"]["is_current"] is True

    versions = client.get("/api/company/brief/versions", headers=headers)
    rows = versions.json()["items"]
    assert [row["version"] for row in rows] == [2, 1]
    assert [row["is_current"] for row in rows] == [False, True]


def test_a_support_phrase_cannot_be_saved_as_core(client, fake_email):
    headers = _headers(client, fake_email, email="downgrade@example.com")

    saved = client.post(
        "/api/company/brief",
        headers=headers,
        json={
            "payload": {
                "core_terms": [
                    {"term": "technical support"},
                    {"term": "document scanning"},
                ],
            }
        },
    )

    assert saved.status_code == 200, saved.text
    terms = saved.json()["terms"]
    assert "technical support" not in terms["core"]
    assert "technical support" in terms["support"]
    assert "document scanning" in terms["core"]


def test_an_edit_without_a_role_uses_its_section(client, fake_email):
    headers = _headers(client, fake_email, email="section@example.com")

    saved = client.post(
        "/api/company/brief",
        headers=headers,
        json={
            "payload": {
                "core_terms": [{"term": "document scanning"}],
                "adjacent_terms": [{"term": "examination processing"}],
            }
        },
    )

    assert saved.status_code == 200, saved.text
    terms = saved.json()["terms"]
    assert "document scanning" in terms["core"]
    assert "examination processing" in terms["adjacent"]
