"""Deterministic assessment, evidence matching and tender-specific checklists.

The gate and the soft score answer different questions. The gate asks whether
every mandatory eligibility criterion is proven; the score ranks tenders. A high
score never turns an Unknown or a Fail into a pass, and a failed mandatory
criterion is never hidden by the rest of the score.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound
from app.core.time import utcnow
from app.models.assessment import ChecklistTask, EvidenceMatch
from app.repositories.assessment import (
    AssessmentRepository,
    ChecklistRepository,
    CompanyDocumentRepository,
    EvidenceRepository,
    latest_version,
)
from app.repositories.corpus import (
    DocumentRepository,
    RequirementRepository,
    TenderAuxRepository,
    TenderRepository,
)
from app.repositories.identity import AuditRepository
from app.repositories.understanding import RequirementExperienceRepository
from app.services.relevance import profile_fit

_IST = timezone(timedelta(hours=5, minutes=30))

CHECKLIST_CATEGORIES = {"submission", "compliance"}
POST_AWARD_MARKERS = (
    "during the contract",
    "during the contract period",
    "after award",
    "after the award",
    "commencement of work",
    "commencement of the contract",
    "completion of work",
    "completion of the contract",
    "defect liability",
    "warranty",
    "maintenance period",
    "post-award",
    "post award",
    "after the contract",
)

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the", "and", "for", "with", "shall", "must", "bidder", "bidders", "tender",
    "tenders", "document", "documents", "required", "require", "requirement",
    "provide", "provided", "submit", "submitted", "submission", "minimum",
    "during", "last", "three", "years", "year", "from", "that", "this", "have",
    "has", "are", "was", "were", "will", "not", "any", "all", "its", "their",
    "which", "such", "under", "than", "least", "also", "been", "other", "into",
    "per", "copy", "copies", "duly", "self", "attested", "signed", "issued",
    "issue", "valid", "validity", "period", "complete", "completed",
}
SYNONYMS = {
    "certifications": "certificate",
    "certification": "certificate",
    "certified": "certificate",
    "certificates": "certificate",
    "licences": "license",
    "licence": "license",
    "licenses": "license",
    "statements": "statement",
    "returns": "return",
    "documents": "document",
    "registrations": "registration",
    "declarations": "declaration",
    "affidavits": "affidavit",
    "completions": "completion",
    "contracts": "contract",
    "projects": "project",
    "works": "work",
    "orders": "order",
}
STRONG_TERMS = {
    "iso", "9001", "14001", "45001", "pan", "gst", "msme", "udyam", "turnover",
    "financial", "audited", "experience", "completion", "solvency", "epf", "esic",
    "labour", "labor", "license", "registration", "affidavit", "declaration",
    "insurance", "noc", "quality", "safety", "environment", "bank", "emd",
    "contractor", "gstin", "pf", "itr",
}
CATEGORY_TERMS = {
    "eligibility": {
        "registration", "license", "certificate", "pan", "gst", "msme", "udyam",
        "iso", "pf", "esic", "turnover", "financial", "experience",
    },
    "submission": {"declaration", "affidavit", "bank", "emd", "form", "bid"},
    "compliance": {
        "declaration", "affidavit", "insurance", "labour", "safety",
        "environment", "noc", "statutory",
    },
    "form": {"form", "declaration", "affidavit", "undertaking", "certificate"},
    "performance": {
        "performance", "contract", "guarantee", "warranty", "maintenance",
        "completion", "penalty", "liquidated",
    },
}


def _today() -> date:
    return datetime.now(_IST).date()


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def _stem(word: str) -> str:
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ies"):
        if len(word) > len(suffix) + 4 and word.endswith(suffix):
            if suffix == "ies":
                return word[: -len(suffix)] + "y"
            return word[: -len(suffix)]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(value: str | None) -> set[str]:
    words = TOKEN_RE.findall(_normalize(value))
    result: set[str] = set()
    for word in words:
        if len(word) < 3 or word in STOPWORDS:
            continue
        result.add(SYNONYMS.get(word, _stem(word)))
    return result


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return None


def _format_inr(value: float) -> str:
    if value >= 10_000_000:
        return f"INR {value / 10_000_000:.2f} crore"
    if value >= 100_000:
        return f"INR {value / 100_000:.2f} lakh"
    return f"INR {value:,.0f}"


def _parse_amount_inr(text: str) -> float | None:
    match = re.search(
        r"(?:INR|Rs\.?|\u20b9)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
        r"(crore|lakh|lakhs|million|mn|billion|bn)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    multipliers = {
        "crore": 10_000_000,
        "lakh": 100_000,
        "lakhs": 100_000,
        "million": 1_000_000,
        "mn": 1_000_000,
        "billion": 1_000_000_000,
        "bn": 1_000_000_000,
    }
    return amount * multipliers.get(unit, 1)


def _requirement_text(requirement: Any) -> str:
    return " ".join(
        str(getattr(requirement, field, None) or "")
        for field in ("title", "obligation", "excerpt")
    )


def _is_post_award(requirement: Any) -> bool:
    if getattr(requirement, "phase", None) == "post_award":
        return True
    if getattr(requirement, "category", None) == "performance":
        return True
    text = _normalize(
        " ".join(
            str(getattr(requirement, field, None) or "")
            for field in (
                "title", "obligation", "excerpt", "deadline_text", "consequence_text"
            )
        )
    )
    return any(marker in text for marker in POST_AWARD_MARKERS)


def _document_tokens(document: Any) -> set[str]:
    return _tokens(
        " ".join(
            str(getattr(document, field, None) or "")
            for field in (
                "title", "document_type", "issuer", "notes", "financial_year",
                "signatory",
            )
        )
    )


def _document_validity(document: Any, today: date) -> tuple[bool, str | None]:
    if document is None:
        return False, "The confirmed evidence document is no longer available."
    if getattr(document, "deleted_at", None) is not None:
        return False, "The evidence document was removed."
    if document.verification_status in {"rejected", "expired"}:
        return False, f"Document is marked {document.verification_status}."
    expiry = _as_date(document.expires_on)
    if expiry and expiry < today:
        return False, f"Document expired on {expiry.isoformat()}."
    if document.verification_status == "self_declared":
        return True, "Self-declared document; confirm the issuer and signatory."
    return True, None


def profile_hints(company: Any, requirement: Any) -> list[str]:
    text = _requirement_text(requirement)
    normalized = _normalize(text)
    profile = getattr(company, "profile", None) or {}
    hints: list[str] = []

    annual_turnover = profile.get("annual_turnover") if isinstance(profile, dict) else None
    if "turnover" in normalized and annual_turnover is not None:
        stated = _parse_amount_inr(text)
        if stated:
            try:
                actual = float(annual_turnover)
            except (TypeError, ValueError):
                actual = None
            if actual is not None:
                relation = "at or above" if actual >= stated else "below"
                hints.append(
                    f"Company profile turnover {_format_inr(actual)} is {relation} the "
                    f"stated {_format_inr(stated)}. Confirm with audited evidence."
                )

    certifications = profile.get("certifications") if isinstance(profile, dict) else None
    if isinstance(certifications, str):
        certifications = [certifications]
    requirement_tokens = _tokens(text)
    for certification in certifications or []:
        cert_tokens = _tokens(str(certification))
        if cert_tokens & requirement_tokens:
            hints.append(f"Company profile lists certification: {certification}.")
    return hints


def evidence_candidates(
    company: Any,
    requirement: Any,
    documents: list[Any],
    *,
    today: date | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Deterministic, explainable suggestions; they never pass the gate alone."""

    day = today or _today()
    req_tokens = _tokens(_requirement_text(requirement))
    category_terms = CATEGORY_TERMS.get(
        str(getattr(requirement, "category", "") or ""), set()
    )
    results: list[dict[str, Any]] = []

    for document in documents:
        version = latest_version(document)
        if version is None:
            continue
        doc_tokens = _document_tokens(document)
        overlap = req_tokens & doc_tokens
        affinity = bool(
            category_terms & (doc_tokens | _tokens(document.document_type))
        )
        if not overlap and not affinity:
            continue

        score = min(0.55, 0.18 * len(overlap))
        reasons: list[str] = []
        if overlap:
            reasons.append("Matches: " + ", ".join(sorted(overlap)[:6]))
        if overlap & STRONG_TERMS:
            score += 0.25
            reasons.append("Matches a distinctive term from the requirement.")
        if affinity:
            score += 0.15
            reasons.append("Document type fits the requirement category.")
        score = round(min(score, 1.0), 2)
        if score < 0.25:
            continue

        valid, validity_note = _document_validity(document, day)
        results.append(
            {
                "document_id": document.id,
                "document_version_id": version.id,
                "title": document.title or "Untitled evidence",
                "document_type": document.document_type or "other",
                "issuer": document.issuer,
                "expiry_date": _as_date(document.expires_on),
                "score": score,
                "matched_terms": sorted(overlap)[:10],
                "reasons": reasons,
                "valid": valid,
                "validity_note": validity_note,
            }
        )

    results.sort(key=lambda item: (-item["score"], item["title"].lower(), str(item["document_id"])))
    return results[:limit]


class AssessmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.tenders = TenderRepository(session)
        self.requirements = RequirementRepository(session)
        self.tender_documents = DocumentRepository(session)
        self.aux = TenderAuxRepository(session)
        self.company_documents = CompanyDocumentRepository(session)
        self.evidence = EvidenceRepository(session)
        self.assessments = AssessmentRepository(session)
        self.checklist = ChecklistRepository(session)
        self.audit = AuditRepository(session)

    def _tender(self, tender_id: uuid.UUID):
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        return tender

    def _requirements(self, company_id: uuid.UUID, tender_id: uuid.UUID):
        items, _ = self.requirements.list_for_tender(
            tender_id,
            company_id=company_id,
            include_navigation=False,
            include_inactive=False,
            include_ignored=False,
            limit=500,
        )
        return [item for item in items if item.state != "rejected"]

    def _documents(self, company_id: uuid.UUID):
        return self.company_documents.list_for_company(company_id)

    def _refresh_expired(self, documents: list[Any], today: date) -> bool:
        changed = False
        for document in documents:
            if self.evidence.recompute_for_document(document.id, today):
                changed = True
        return changed

    def _evidence_view(
        self, match: EvidenceMatch | None, today: date
    ) -> tuple[str, Any | None, str | None, str | None]:
        """Return (state, company document, note, validity note)."""

        if match is None:
            return "none", None, None, None
        version = match.version
        document = version.document if version is not None else None
        if match.status == "contradicted":
            return "contradicted", document, match.reason, None
        if match.status in {"expired", "missing"}:
            return match.status, document, match.reason, None
        if match.status != "covered":
            return "missing", document, match.reason, None
        valid, validity_note = _document_validity(document, today)
        if not valid:
            return "expired", document, validity_note, validity_note
        if match.confirmed_by is None or match.confirmed_at is None:
            return "missing", document, "Evidence confirmation was cleared.", validity_note
        return "covered", document, match.reason, validity_note

    def _criterion(
        self,
        *,
        requirement: Any,
        match: EvidenceMatch | None,
        documents: list[Any],
        company: Any,
        today: date,
    ) -> dict[str, Any]:
        evidence_state, evidence_document, evidence_note, validity_note = (
            self._evidence_view(match, today)
        )
        citation_ok = requirement.state in {"verified", "accepted_ocr"}
        ocr_confirmed = requirement.state == "accepted_ocr"
        if ocr_confirmed and (
            requirement.reviewed_by is None or requirement.reviewed_at is None
        ):
            citation_ok = False
            ocr_confirmed = False

        reason: str
        gate = "unknown"
        if not citation_ok:
            if requirement.state == "needs_review":
                reason = (
                    "The citation is OCR-only and has not been accepted by a human, "
                    "so eligibility cannot be confirmed."
                )
            else:
                reason = (
                    "The requirement citation is not verified, so eligibility cannot be confirmed."
                )
        elif match is None:
            candidates = evidence_candidates(
                company, requirement, documents, today=today, limit=1
            )
            if candidates:
                reason = (
                    f"Possible evidence found: {candidates[0]['title']}. "
                    "A human must confirm or reject it."
                )
            else:
                reason = "No evidence has been confirmed. Unknown is not a pass."
        elif evidence_state == "contradicted":
            gate = "fail"
            reason = (
                "A human reviewer marked the selected evidence as contradicting this requirement."
            )
        elif evidence_state == "covered":
            gate = "pass"
            if ocr_confirmed:
                reason = (
                    "Pass - OCR citation, human-confirmed. "
                    f"Human-confirmed evidence: {evidence_document.title}."
                )
            else:
                reason = f"Human-confirmed evidence: {evidence_document.title}."
            if validity_note:
                reason += f" {validity_note}"
        elif evidence_state == "expired":
            reason = validity_note or "The confirmed evidence has expired."
        else:
            reason = "The selected document is not confirmed evidence. Unknown is not a pass."

        criterion_text = (
            requirement.title
            or (requirement.obligation or "")[:200]
            or requirement.excerpt[:200]
        )
        return {
            "requirement": requirement,
            "criterion_text": criterion_text,
            "verdict": gate,
            "reason": reason,
            "evidence_state": evidence_state,
            "evidence_match_id": match.id if match is not None else None,
            "evidence_version_id": (
                match.company_document_version_id if match is not None else None
            ),
            "evidence_document": evidence_document,
            "citation_state": requirement.state,
            "ocr_confirmed": ocr_confirmed,
            "profile_hints": profile_hints(company, requirement),
        }

    def _should_gate(self, requirement: Any) -> bool:
        return (
            requirement.mandatory_state == "mandatory"
            and requirement.category == "eligibility"
        )

    def assess(self, *, tender_id: uuid.UUID, company: Any):
        tender = self._tender(tender_id)
        documents = self._documents(company.id)
        today = _today()
        self._refresh_expired(documents, today)

        requirements = self._requirements(company.id, tender_id)
        evidence_map = self.evidence.for_requirements(
            company.id, [item.id for item in requirements]
        )
        criteria = [
            self._criterion(
                requirement=requirement,
                match=evidence_map.get(requirement.id),
                documents=documents,
                company=company,
                today=today,
            )
            for requirement in requirements
            if self._should_gate(requirement)
        ]
        eligibility = self._eligibility(criteria)
        score = self._score(
            company=company,
            tender=tender,
            requirements=requirements,
            criteria=criteria,
            eligibility=eligibility,
        )
        generated_at = score["generated_at"]

        self.assessments.save(
            company_id=company.id,
            tender_id=tender.id,
            score=score["value"],
            components={
                dimension["key"]: dimension["contribution"]
                for dimension in score["dimensions"]
            },
            gate=eligibility["status"],
            gate_reason=eligibility["message"],
            assessed_at=generated_at,
        )
        self.assessments.sync_criteria(
            company_id=company.id,
            tender_id=tender.id,
            items=[
                {
                    "requirement_id": item["requirement"].id,
                    "criterion_text": item["criterion_text"],
                    "verdict": item["verdict"],
                    "reason": item["reason"],
                    "evidence_match_id": item["evidence_match_id"],
                }
                for item in criteria
            ],
        )
        self.session.commit()

        counts = {
            "total": len(requirements),
            "verified": sum(1 for item in requirements if item.state == "verified"),
            "accepted_ocr": sum(
                1 for item in requirements if item.state == "accepted_ocr"
            ),
            "mandatory_gating": len(criteria),
            "checklist_total": self.checklist.count_for_tender(
                company.id, tender.id
            ),
            "checklist_open": self.checklist.count_open(company.id, tender.id),
        }
        return {
            "tender_id": tender.id,
            "score": score,
            "eligibility": eligibility,
            "counts": counts,
            "generated_at": generated_at,
        }

    def _eligibility(self, criteria: list[dict[str, Any]]) -> dict[str, Any]:
        items = []
        for item in criteria:
            requirement = item["requirement"]
            document = item["evidence_document"]
            items.append(
                {
                    "requirement_id": requirement.id,
                    "title": requirement.title or "Untitled requirement",
                    "category": requirement.category,
                    "mandatory_state": requirement.mandatory_state,
                    "excerpt": requirement.excerpt,
                    "page_index": requirement.page_index,
                    "printed_label": requirement.printed_label,
                    "clause_ref": requirement.clause_ref,
                    "form_ref": requirement.form_ref,
                    "document_id": requirement.document_id,
                    "document_title": requirement.document.filename
                    if requirement.document
                    else None,
                    "citation_state": item["citation_state"],
                    "ocr_confirmed": item["ocr_confirmed"],
                    "gate": item["verdict"],
                    "evidence_status": item["evidence_state"],
                    "evidence_document_id": document.id if document else None,
                    "evidence_document_version_id": item["evidence_version_id"],
                    "evidence_document_title": document.title if document else None,
                    "profile_hints": item["profile_hints"],
                    "reason": item["reason"],
                }
            )
        pass_count = sum(1 for item in items if item["gate"] == "pass")
        fail_count = sum(1 for item in items if item["gate"] == "fail")
        unknown_count = sum(1 for item in items if item["gate"] == "unknown")
        if not items:
            status = "unknown"
            message = "No mandatory eligibility requirements have been extracted yet."
        elif fail_count:
            status = "fail"
            message = "At least one mandatory eligibility criterion is marked as failed."
        elif pass_count == len(items):
            status = "pass"
            message = "Every mandatory eligibility criterion has confirmed, current evidence."
        else:
            status = "unknown"
            message = (
                f"{unknown_count} mandatory criteria remain Unknown. Unknown is not a pass."
            )
        return {
            "status": status,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "unknown_count": unknown_count,
            "items": items,
            "message": message,
        }

    def _score(
        self,
        *,
        company: Any,
        tender: Any,
        requirements: list[Any],
        criteria: list[dict[str, Any]],
        eligibility: dict[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        profile_points, profile_reasons, profile_reason = profile_fit(company, tender)
        profile_ratio = profile_points / 25

        verified = sum(
            1 for item in requirements if item.state in {"verified", "accepted_ocr"}
        )
        citation_ratio = verified / len(requirements) if requirements else 0.0

        mandatory_total = len(criteria)
        evidence_ratio = (
            eligibility["pass_count"] / mandatory_total if mandatory_total else 0.0
        )

        covers = self.aux.covers(tender.id)
        expected_covers = len(covers)
        documents_present = self.tender_documents.count_current(tender.id)
        readiness_ratio = (
            min(1.0, documents_present / expected_covers) if expected_covers else 0.0
        )

        dimensions = [
            {
                "key": "profile_fit",
                "label": "Profile fit",
                "weight": 25,
                "score": round(profile_ratio * 100, 2),
                "contribution": round(profile_ratio * 25, 2),
                "reason": (
                    "; ".join(profile_reasons)
                    if profile_reasons
                    else profile_reason
                ),
            },
            {
                "key": "requirements_verified",
                "label": "Citation verification",
                "weight": 20,
                "score": round(citation_ratio * 100, 2),
                "contribution": round(citation_ratio * 20, 2),
                "reason": (
                    f"{verified} verified or human-accepted OCR citations, "
                    f"{len(requirements)} active requirements."
                    if requirements
                    else "No requirements have been extracted."
                ),
            },
            {
                "key": "evidence_coverage",
                "label": "Mandatory evidence coverage",
                "weight": 40,
                "score": round(evidence_ratio * 100, 2),
                "contribution": round(evidence_ratio * 40, 2),
                "reason": (
                    f"{eligibility['pass_count']} of {mandatory_total} mandatory "
                    "eligibility criteria have confirmed, current evidence."
                    if mandatory_total
                    else "No mandatory eligibility criteria have been extracted."
                ),
            },
            {
                "key": "document_readiness",
                "label": "Document readiness",
                "weight": 15,
                "score": round(readiness_ratio * 100, 2),
                "contribution": round(readiness_ratio * 15, 2),
                "reason": (
                    f"{documents_present} of {expected_covers} expected tender "
                    "documents are present."
                    if expected_covers
                    else "No tender cover table is known, so readiness is 0."
                ),
            },
        ]
        value = round(sum(item["contribution"] for item in dimensions), 2)
        if eligibility["status"] == "fail":
            explanation = (
                "Mandatory eligibility has a confirmed failure. The soft score cannot "
                "override a failed mandatory criterion."
            )
        elif eligibility["status"] == "unknown":
            explanation = (
                "Mandatory eligibility is not fully confirmed. Treat this score as "
                "provisional until every Unknown criterion is reviewed."
            )
        elif not requirements:
            explanation = "Not enough extracted requirements to assess this tender."
        else:
            explanation = (
                "Soft fit based on profile match, citation verification, confirmed "
                "evidence coverage and document readiness. It is not an eligibility decision."
            )
        if value >= 75:
            band = "strong"
        elif value >= 50:
            band = "moderate"
        elif value >= 25:
            band = "weak"
        else:
            band = "insufficient"
        return {
            "value": value,
            "band": band,
            "dimensions": dimensions,
            "explanation": explanation,
            "gating_status": eligibility["status"],
            "generated_at": now,
        }

    def _requirement_for_tender(
        self, tender_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> Any:
        requirement = self.requirements.get(requirement_id)
        if (
            requirement is None
            or requirement.document is None
            or requirement.document.tender_id != tender_id
        ):
            raise NotFound("Requirement not found.")
        return requirement

    def evidence_for_requirement(
        self,
        *,
        tender_id: uuid.UUID,
        requirement_id: uuid.UUID,
        company: Any,
    ) -> dict[str, Any]:
        requirement = self._requirement_for_tender(tender_id, requirement_id)
        documents = self._documents(company.id)
        today = _today()
        if self._refresh_expired(documents, today):
            self.session.commit()
        candidates = evidence_candidates(
            company, requirement, documents, today=today
        )
        match = self.evidence.get(company.id, requirement.id)
        state, document, note, validity_note = self._evidence_view(match, today)
        status = state if match is not None else ("suggested" if candidates else "none")
        return {
            "requirement_id": requirement.id,
            "status": status,
            "document_id": document.id if document else None,
            "document_version_id": match.company_document_version_id
            if match
            else None,
            "document_title": document.title if document else None,
            "note": note,
            "confirmed_at": match.confirmed_at if match else None,
            "validity_note": validity_note,
            "candidates": candidates,
        }

    def decide_evidence(
        self,
        *,
        tender_id: uuid.UUID,
        requirement_id: uuid.UUID,
        company: Any,
        user_id: uuid.UUID,
        document_id: uuid.UUID | None,
        document_version_id: uuid.UUID | None,
        status: str,
        note: str | None,
    ) -> dict[str, Any]:
        requirement = self._requirement_for_tender(tender_id, requirement_id)
        if status == "missing":
            self.evidence.delete(company.id, requirement.id)
            self.audit.log(
                company_id=company.id,
                actor_user_id=user_id,
                action="evidence.cleared",
                entity_type="requirement",
                entity_id=requirement.id,
            )
            self.session.commit()
            return self.evidence_for_requirement(
                tender_id=tender_id, requirement_id=requirement_id, company=company
            )
        if document_id is None:
            raise BadRequest("Choose the company document that proves this requirement.")
        document = self.company_documents.get(company.id, document_id)
        if document is None:
            raise NotFound("Evidence document not found.")
        if document_version_id is not None:
            version = self.company_documents.get_version(company.id, document_version_id)
            if version is None or version.company_document_id != document.id:
                raise NotFound("Evidence document version not found.")
        else:
            version = latest_version(document)
        if version is None:
            raise BadRequest("The selected document has no uploaded file.")
        if status == "covered":
            valid, validity_note = _document_validity(document, _today())
            if not valid:
                raise BadRequest(
                    validity_note
                    or "The selected document is not valid evidence; upload the current version."
                )
        now = utcnow()
        match = self.evidence.upsert(
            company_id=company.id,
            requirement_id=requirement.id,
            version_id=version.id,
            status=status,
            reason=note,
            confirmed_by=user_id,
            confirmed_at=now,
        )
        self.audit.log(
            company_id=company.id,
            actor_user_id=user_id,
            action="evidence.decision",
            entity_type="requirement",
            entity_id=requirement.id,
            after={
                "status": status,
                "company_document_id": str(document.id),
                "company_document_version_id": str(version.id),
                "evidence_match_id": str(match.id),
                "note": note,
            },
        )
        self.session.commit()
        return self.evidence_for_requirement(
            tender_id=tender_id, requirement_id=requirement_id, company=company
        )

    def list_legal_obligations(
        self, *, tender_id: uuid.UUID, company_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        self._tender(tender_id)
        items: list[dict[str, Any]] = []
        for requirement in self._requirements(company_id, tender_id):
            if (
                requirement.category not in {"compliance", "performance"}
                and not requirement.consequence_text
            ):
                continue
            document = requirement.document
            items.append(
                {
                    "requirement_id": requirement.id,
                    "title": requirement.title or "Untitled obligation",
                    "category": requirement.category,
                    "phase": "post_award" if _is_post_award(requirement) else "pre_award",
                    "obligation": requirement.obligation or "",
                    "excerpt": requirement.excerpt,
                    "page_index": requirement.page_index,
                    "printed_label": requirement.printed_label,
                    "clause_ref": requirement.clause_ref,
                    "form_ref": requirement.form_ref,
                    "document_id": requirement.document_id,
                    "document_title": document.filename if document else None,
                    "deadline_text": requirement.deadline_text,
                    "consequence_text": requirement.consequence_text,
                    "state": requirement.state,
                    "source_url": document.source_url if document else None,
                    "retrieved_at": document.fetched_at if document else None,
                }
            )
        return items

    def _evidence_check(
        self,
        *,
        requirement: Any,
        match: EvidenceMatch | None,
        today: date,
        experience=None,
    ) -> str:
        state, document, note, _validity = self._evidence_view(match, today)
        if state == "covered":
            label = f"Confirmed evidence: {document.title}"
            if document.expires_on:
                label += f" (expires {document.expires_on.isoformat()})"
            return label
        if state == "contradicted":
            return (
                "A reviewer marked the selected evidence as contradicting this requirement."
            )
        if state == "expired":
            return note or "The selected evidence has expired; confirm current evidence."
        base = (
            requirement.evidence_check
            or "No company evidence confirmed for this requirement."
        )
        if experience is None:
            return base
        if experience.coverage == "new":
            return (
                "New work: no project or product in your confirmed profile "
                f"shows this kind of work. {base}"
            )
        if experience.coverage in {"covered", "partial"} and experience.company_excerpt:
            page = (
                f"page {experience.company_page_index}"
                if experience.company_page_index is not None
                else "your profile"
            )
            return (
                f"{base} Your profile shows related work ({page}): "
                f'"{experience.company_excerpt[:200]}"'
            )
        return base

    def generate_checklist(
        self, *, tender_id: uuid.UUID, company: Any, user_id: uuid.UUID
    ) -> tuple[list[ChecklistTask], int, int]:
        tender = self._tender(tender_id)
        requirements = self._requirements(company.id, tender_id)
        documents = self._documents(company.id)
        today = _today()
        self._refresh_expired(documents, today)
        evidence_map = self.evidence.for_requirements(
            company.id, [item.id for item in requirements]
        )
        # The company's own delivered work is a second, cited signal: a task
        # for work the profile has never done says so, instead of duplicating
        # the same generic "confirm evidence" line on every row.
        experience_map = RequirementExperienceRepository(self.session).for_tender(
            company_id=company.id, tender_id=tender_id
        )
        created: list[ChecklistTask] = []
        skipped = 0
        attempted = 0

        for requirement in requirements:
            post_award = _is_post_award(requirement)
            if post_award:
                if (
                    requirement.category not in {"performance", "compliance"}
                    and requirement.mandatory_state != "mandatory"
                ):
                    continue
                phase = "post_award"
            else:
                if not (
                    requirement.mandatory_state == "mandatory"
                    or requirement.category in CHECKLIST_CATEGORIES
                    or requirement.form_ref
                ):
                    continue
                phase = "pre_award"

            attempted += 1
            match = evidence_map.get(requirement.id)
            due_at = requirement.deadline_at
            trigger = requirement.deadline_text
            if phase == "pre_award":
                due_at = due_at or tender.closing_at
                if trigger is None and tender.closing_at is not None:
                    trigger = "Before the tender submission deadline"
            title = (
                requirement.title
                or (requirement.obligation or "")[:200]
                or requirement.excerpt[:200]
            )
            values = {
                "company_id": company.id,
                "tender_id": tender.id,
                "requirement_id": requirement.id,
                "phase": phase,
                "title": title,
                "category": requirement.category,
                "acceptance_criteria": requirement.acceptance_criteria
                or requirement.excerpt,
                "issuer": requirement.issuer or tender.authority,
                "signatory": requirement.signatory,
                "evidence_check": self._evidence_check(
                    requirement=requirement,
                    match=match,
                    today=today,
                    experience=experience_map.get(requirement.id),
                ),
                "due_at": due_at,
                "trigger": trigger,
                "consequence": requirement.consequence_text,
                "document_id": requirement.document_id,
                "page_index": requirement.page_index,
                "clause_ref": requirement.clause_ref,
                "excerpt": requirement.excerpt,
                "source_url": requirement.document.source_url
                if requirement.document
                else None,
                "status": "open",
                "owner": None,
            }
            task, was_created = self.checklist.create_generated(values)
            if was_created:
                created.append(task)
            else:
                skipped += 1

        self.audit.log(
            company_id=company.id,
            actor_user_id=user_id,
            action="checklist.generated",
            entity_type="tender",
            entity_id=tender.id,
            after={"created": len(created), "skipped": skipped, "attempted": attempted},
        )
        self.session.commit()
        return created, skipped, attempted

    def list_tasks(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        phase: str | None = None,
    ) -> list[ChecklistTask]:
        self._tender(tender_id)
        return self.checklist.list_for_tender(
            company_id, tender_id, phase=phase
        )

    def list_company_tasks(
        self,
        *,
        company_id: uuid.UUID,
        phase: str | None = None,
        status: list[str] | None = None,
        limit: int = 500,
    ) -> list[ChecklistTask]:
        """The global checklist page: every task this company owns.

        Tasks belong to the company, not to the tender corpus, so this is a
        plain company-scoped read with optional phase/status filters.  It never
        hides a task from another company and never merges tenders.
        """

        tasks = self.checklist.list_for_company(company_id, limit=limit)
        if phase is not None:
            tasks = [task for task in tasks if task.phase == phase]
        if status:
            allowed = set(status)
            tasks = [task for task in tasks if task.status in allowed]
        return tasks

    def create_task(
        self,
        *,
        company: Any,
        user_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> ChecklistTask:
        tender = self._tender(payload["tender_id"])
        requirement_id = payload.get("requirement_id")
        if requirement_id is not None:
            requirement = self._requirement_for_tender(tender.id, requirement_id)
            payload = {**payload, "requirement_id": requirement.id}
        document_id = payload.get("document_id")
        if document_id is not None:
            document = self.tender_documents.get(document_id)
            if document is None or document.tender_id != tender.id:
                raise NotFound("Tender document not found.")
        values = {
            **payload,
            "company_id": company.id,
            "tender_id": tender.id,
            "status": "open",
            "owner": None,
        }
        task = self.checklist.create(values)
        self.audit.log(
            company_id=company.id,
            actor_user_id=user_id,
            action="checklist.created",
            entity_type="checklist_task",
            entity_id=task.id,
            after={"tender_id": str(tender.id), "title": task.title},
        )
        self.session.commit()
        return task

    def update_task(
        self,
        *,
        task_id: uuid.UUID,
        company_id: uuid.UUID,
        user_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> ChecklistTask:
        task = self.checklist.get(company_id, task_id)
        if task is None:
            raise NotFound("Checklist task not found.")
        before = {
            "status": task.status,
            "owner": task.owner,
            "due_at": task.due_at.isoformat() if task.due_at else None,
        }
        self.checklist.update(task, changes)
        self.audit.log(
            company_id=company_id,
            actor_user_id=user_id,
            action="checklist.updated",
            entity_type="checklist_task",
            entity_id=task.id,
            before=before,
            after={key: str(value) for key, value in changes.items()},
        )
        self.session.commit()
        return task
