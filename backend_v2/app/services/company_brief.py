"""Build, edit and version the company brief that fit ranking reads.

The PDF reader answers "what does the company say it does". The brief is the
human-facing answer to "what should discovery treat as this company's work".
It is deliberately separate from the raw document terms and from eligibility:

* **core** work is a confirmed capability or a project work family;
* **adjacent** work is a specific term the company declared in its profile but
  has not confirmed from a document;
* **support** terms describe delivery (``technical support``, ``document
  retrieval``) and never rank as core;
* **generic** terms are recall-only (``technology``, ``web pages``);
* projects and certifications are kept for evidence, but only projects feed
  work fit. A certification supports eligibility and never makes a tender the
  company's kind of work.

Every save is a new version. The current row is the only one fit reads, so a
human can edit the words without losing the version a past judgment used.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound
from app.core.time import utcnow
from app.discovery.roles import (
    ROLE_ADJACENT,
    ROLE_CORE,
    ROLE_GENERIC,
    ROLE_SUPPORT,
    ROLES,
    classify_term,
    role_term,
    term_text,
)
from app.discovery.work_text import normalize_text
from app.models.identity import Company, CompanyDocument
from app.models.understanding import CompanyBrief
from app.repositories.intelligence import stable_hash
from app.repositories.understanding import (
    CompanyBriefRepository,
    CompanyUnderstandingRepository,
)

BRIEF_VERSION = "2026-11-22.1"
MAX_TERMS_PER_ROLE = 120
MAX_PROJECTS = 60
MAX_CERTIFICATIONS = 40

CERTIFICATION_TYPES = frozenset(
    {
        "certificate",
        "certification",
        "gst",
        "iso",
        "licence",
        "license",
        "msme",
        "pan",
        "registration",
        "udyam",
    }
)

ROLE_KEYS = {
    ROLE_CORE: "core_terms",
    ROLE_ADJACENT: "adjacent_terms",
    ROLE_SUPPORT: "support_terms",
    ROLE_GENERIC: "generic_terms",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _clean(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _dedupe_key(term: str) -> str:
    return normalize_text(term)


def _role_priority(role: str) -> int:
    return {ROLE_CORE: 0, ROLE_ADJACENT: 1, ROLE_SUPPORT: 2, ROLE_GENERIC: 3}.get(
        role, 4
    )


def _merge_term(
    groups: dict[str, dict[str, dict[str, Any]]],
    item: dict[str, Any],
    *,
    allow_short_acronym: bool = False,
) -> None:
    term = _clean(item.get("term"), 200)
    if not term:
        return
    cleaned = role_term(term, allow_short_acronym=allow_short_acronym)
    if not cleaned:
        return
    role = str(item.get("role") or "").strip().lower()
    if role not in ROLES:
        role = classify_term(cleaned, confirmed=item.get("source") == "capability")
    key = _dedupe_key(cleaned)
    bucket = groups[role]
    current = bucket.get(key)
    if current is None or _role_priority(role) < _role_priority(
        str(current.get("role") or role)
    ):
        bucket[key] = {
            **item,
            "term": cleaned,
            "role": role,
            "normalized": key,
        }
        return
    # Keep the stronger citation, but do not lose a page or a source.
    for field in ("evidence", "page_index", "printed_label", "document_id", "source_id"):
        if not current.get(field) and item.get(field):
            current[field] = item[field]


def _groups() -> dict[str, dict[str, dict[str, Any]]]:
    return {role: {} for role in ROLES}


def _payload_from_groups(
    *,
    summary: str,
    groups: dict[str, dict[str, dict[str, Any]]],
    projects: list[dict[str, Any]],
    certifications: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summary,
        "projects": projects[:MAX_PROJECTS],
        "certifications": certifications[:MAX_CERTIFICATIONS],
        "rules_version": BRIEF_VERSION,
    }
    for role, key in ROLE_KEYS.items():
        payload[key] = sorted(
            groups[role].values(),
            key=lambda item: (str(item.get("term") or "").casefold()),
        )[:MAX_TERMS_PER_ROLE]
    return payload


def _summary(payload: dict[str, Any]) -> str:
    def names(key: str, limit: int = 6) -> str:
        values = [
            str(item.get("term"))
            for item in payload.get(key) or []
            if isinstance(item, dict) and item.get("term")
        ]
        return ", ".join(values[:limit]) if values else "none yet"

    return (
        f"Core work: {names('core_terms')}. "
        f"Adjacent declared work: {names('adjacent_terms')}. "
        f"Support services: {names('support_terms')}. "
        f"Generic recall terms: {names('generic_terms')}."
    )


def payload_terms(payload: dict[str, Any] | None) -> dict[str, list[str]]:
    """The four term groups from a stored or derived brief payload."""

    out: dict[str, list[str]] = {role: [] for role in ROLES}
    if not isinstance(payload, dict):
        return out
    for role, key in ROLE_KEYS.items():
        for item in payload.get(key) or []:
            term = _clean(term_text(item), 200)
            if term and term not in out[role]:
                out[role].append(term)
    return out


def payload_signature(payload: dict[str, Any] | None) -> str:
    groups = payload_terms(payload)
    projects = [
        {
            "title": normalize_text(str(item.get("title") or "")),
            "work_family": normalize_text(str(item.get("work_family") or "")),
        }
        for item in (payload or {}).get("projects") or []
        if isinstance(item, dict)
    ]
    return stable_hash(
        {
            "rules_version": BRIEF_VERSION,
            "terms": {role: sorted(values) for role, values in groups.items()},
            "projects": sorted(projects, key=lambda item: (item["title"], item["work_family"])),
        }
    )


class CompanyBriefService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = CompanyBriefRepository(session)
        self.understanding = CompanyUnderstandingRepository(session)

    # ------------------------------------------------------------------ read
    def current(self, company_id: uuid.UUID) -> CompanyBrief | None:
        return self.repository.current(company_id)

    def current_payload(self, company_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.current(company_id)
        return dict(row.payload or {}) if row is not None else None

    def current_or_preview(self, company: Company) -> dict[str, Any]:
        row = self.current(company.id)
        if row is not None:
            return {
                "brief": self.out(row),
                "terms": payload_terms(row.payload),
                "rules_version": BRIEF_VERSION,
            }
        payload = self.derive_payload(company)
        return {
            "brief": None,
            "preview": {
                "payload": payload,
                "source_kind": "derived",
                "version": None,
            },
            "terms": payload_terms(payload),
            "rules_version": BRIEF_VERSION,
        }

    def terms(self, company: Company) -> dict[str, list[str]]:
        """The four groups the read path uses.

        A stored brief wins. Before one is generated, the same deterministic
        role rules are applied to the profile and confirmed understanding, so a
        generic term is recall-only from the first request.
        """

        payload = self.current_payload(company.id)
        if payload is None:
            payload = self.derive_payload(company)
        return payload_terms(payload)

    def signature(self, company: Company) -> str:
        payload = self.current_payload(company.id)
        if payload is None:
            payload = self.derive_payload(company)
        return payload_signature(payload)

    # --------------------------------------------------------------- generate
    def derive_payload(self, company: Company) -> dict[str, Any]:
        groups = _groups()
        capabilities = self.understanding.confirmed_capabilities(company.id)
        experiences = self.understanding.confirmed_experiences(company.id)

        for row in capabilities:
            item = {
                "term": row.name,
                "role": classify_term(row.name, confirmed=True),
                "source": "capability",
                "source_id": str(row.id),
                "document_id": str(row.company_document_id),
                "evidence": row.excerpt,
                "page_index": row.page_index,
                "printed_label": row.printed_label,
                "description": row.description,
                "state": "confirmed",
            }
            _merge_term(groups, item)
            for alias in row.aliases or []:
                _merge_term(
                    groups,
                    {
                        **item,
                        "term": alias,
                        "role": classify_term(alias, confirmed=True),
                        "source": "capability_alias",
                    },
                    allow_short_acronym=True,
                )

        projects: list[dict[str, Any]] = []
        for row in experiences:
            family = _clean(row.work_family, 200)
            if family:
                _merge_term(
                    groups,
                    {
                        "term": family,
                        "role": ROLE_CORE,
                        "source": "project",
                        "source_id": str(row.id),
                        "document_id": str(row.company_document_id),
                        "evidence": row.excerpt,
                        "page_index": row.page_index,
                        "printed_label": row.printed_label,
                        "description": row.description,
                        "state": "confirmed",
                    },
                )
            else:
                # A project title is a proper noun, but it is the only work
                # family the page stated. Keep it as a project-only term and
                # mark it so a human can replace it with a cleaner phrase.
                title = _clean(row.title, 200)
                if title:
                    _merge_term(
                        groups,
                        {
                            "term": title,
                            "role": ROLE_CORE,
                            "source": "project_title",
                            "source_id": str(row.id),
                            "document_id": str(row.company_document_id),
                            "evidence": row.excerpt,
                            "page_index": row.page_index,
                            "printed_label": row.printed_label,
                            "state": "confirmed",
                        },
                    )
            projects.append(
                {
                    "title": row.title,
                    "client": row.client,
                    "work_family": row.work_family,
                    "description": row.description,
                    "value_amount": (
                        float(row.value_amount)
                        if row.value_amount is not None
                        else None
                    ),
                    "currency": row.currency,
                    "completed_on": (
                        row.completed_on.isoformat() if row.completed_on else None
                    ),
                    "document_id": str(row.company_document_id),
                    "source_id": str(row.id),
                    "page_index": row.page_index,
                    "printed_label": row.printed_label,
                    "evidence": row.excerpt,
                }
            )

        profile = getattr(company, "profile", None) or {}
        if not isinstance(profile, dict):
            profile = {}
        declared = (
            _as_list(getattr(company, "services", None))
            + _as_list(getattr(company, "sectors", None))
            + _as_list(getattr(company, "keywords", None))
            + _as_list(profile.get("services"))
            + _as_list(profile.get("sectors"))
            + _as_list(profile.get("keywords"))
            + _as_list(profile.get("products_services"))
            + _as_list(profile.get("product_services"))
        )
        for term in declared:
            cleaned = _clean(term, 200)
            if not cleaned:
                continue
            _merge_term(
                groups,
                {
                    "term": cleaned,
                    "role": classify_term(cleaned, confirmed=False),
                    "source": "profile",
                    "evidence": cleaned,
                    "state": "declared",
                },
            )

        certifications = self._certifications(company.id)
        payload = _payload_from_groups(
            summary="",
            groups=groups,
            projects=projects,
            certifications=certifications,
        )
        payload["summary"] = _summary(payload)
        return payload

    def _certifications(self, company_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = list(
            self.session.scalars(
                select(CompanyDocument)
                .where(
                    CompanyDocument.company_id == company_id,
                    CompanyDocument.deleted_at.is_(None),
                    CompanyDocument.document_type.in_(sorted(CERTIFICATION_TYPES)),
                )
                .order_by(CompanyDocument.created_at.desc())
                .limit(MAX_CERTIFICATIONS)
            ).all()
        )
        return [
            {
                "title": row.title or row.document_type,
                "document_type": row.document_type,
                "issuer": row.issuer,
                "expires_on": row.expires_on.isoformat() if row.expires_on else None,
                "document_id": str(row.id),
                "note": "Evidence for eligibility; never work fit.",
            }
            for row in rows
        ]

    def generate(
        self,
        company: Company,
        *,
        actor_user_id: uuid.UUID | None = None,
        source_document_id: uuid.UUID | None = None,
        source_kind: str = "generated",
        commit: bool = True,
    ) -> CompanyBrief:
        payload = self.derive_payload(company)
        row = self.repository.create(
            company_id=company.id,
            payload=payload,
            source_kind=source_kind,
            source_document_id=source_document_id,
            prompt_version=BRIEF_VERSION,
            created_by=actor_user_id,
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return row

    # ------------------------------------------------------------------ write
    def save(
        self,
        company: Company,
        *,
        payload: dict[str, Any],
        actor_user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> CompanyBrief:
        clean = self._clean_payload(payload)
        row = self.repository.create(
            company_id=company.id,
            payload=clean,
            source_kind="human",
            prompt_version=BRIEF_VERSION,
            created_by=actor_user_id,
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return row

    def activate(
        self, company: Company, *, brief_id: uuid.UUID, commit: bool = True
    ) -> CompanyBrief:
        row = self.repository.activate(company_id=company.id, brief_id=brief_id)
        if row is None:
            raise NotFound("Company brief not found.")
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return row

    def _clean_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BadRequest("A company brief must be a JSON object.")
        groups = _groups()
        for role, key in ROLE_KEYS.items():
            values = payload.get(key) or []
            if not isinstance(values, list):
                raise BadRequest(f"{key} must be a list.")
            for item in values:
                term = _clean(term_text(item), 200)
                if not term:
                    continue
                stored_role = role
                derived = classify_term(term, confirmed=role == ROLE_CORE)
                # The section the human put the term in is the role they chose.
                # A clearly support/generic phrase cannot be saved as core by
                # mistake, though: the deterministic rule downgrades it.
                if derived in (ROLE_SUPPORT, ROLE_GENERIC):
                    stored_role = derived
                _merge_term(
                    groups,
                    {
                        **(item if isinstance(item, dict) else {}),
                        "term": term,
                        "role": stored_role if stored_role in ROLES else role,
                        "source": str(
                            (item.get("source") if isinstance(item, dict) else None)
                            or "human"
                        ),
                        "state": "confirmed"
                        if stored_role == ROLE_CORE
                        else str(
                            (item.get("state") if isinstance(item, dict) else None)
                            or "declared"
                        ),
                    },
                )
        projects = payload.get("projects") or []
        certifications = payload.get("certifications") or []
        clean = _payload_from_groups(
            summary=_clean(payload.get("summary"), 2000) or "",
            groups=groups,
            projects=[item for item in projects if isinstance(item, dict)],
            certifications=[
                item for item in certifications if isinstance(item, dict)
            ],
        )
        if not clean["summary"]:
            clean["summary"] = _summary(clean)
        return clean

    # ------------------------------------------------------------------- out
    @staticmethod
    def out(row: CompanyBrief) -> dict[str, Any]:
        return {
            "id": row.id,
            "company_id": row.company_id,
            "version": row.version,
            "is_current": row.is_current,
            "source_kind": row.source_kind,
            "source_document_id": row.source_document_id,
            "payload": row.payload or {},
            "prompt_version": row.prompt_version,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def version_out(row: CompanyBrief) -> dict[str, Any]:
        payload = row.payload or {}
        return {
            "id": row.id,
            "version": row.version,
            "is_current": row.is_current,
            "source_kind": row.source_kind,
            "source_document_id": row.source_document_id,
            "summary": payload.get("summary"),
            "counts": {
                "core": len(payload.get("core_terms") or []),
                "adjacent": len(payload.get("adjacent_terms") or []),
                "support": len(payload.get("support_terms") or []),
                "generic": len(payload.get("generic_terms") or []),
                "projects": len(payload.get("projects") or []),
                "certifications": len(payload.get("certifications") or []),
            },
            "created_by": row.created_by,
            "created_at": row.created_at,
        }
