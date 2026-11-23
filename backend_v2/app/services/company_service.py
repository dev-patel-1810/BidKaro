from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.identity import Company, CompanyDocument, CompanyDocumentVersion
from app.repositories.assessment import EvidenceRepository
from app.repositories.identity import AuditRepository, CompanyRepository
from app.schemas.auth import CompanyOut
from app.schemas.company import CompanyDocumentOut, CompanyDocumentPatch, CompanyUpdateRequest
from app.services import storage

DOCUMENT_TYPES = {
    "about_company",
    "registration",
    "iso",
    "turnover",
    "completion_certificate",
    "pan",
    "gst",
    "other",
}

_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> date:
    return datetime.now(_IST).date()


class CompanyService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.companies = CompanyRepository(session)
        self.audit = AuditRepository(session)

    def get(self, company: Company) -> CompanyOut:
        return CompanyOut.model_validate(company)

    def update(self, company: Company, payload: CompanyUpdateRequest) -> CompanyOut:
        values = payload.model_dump(exclude_unset=True)
        if "name" in values:
            name = (values["name"] or "").strip()
            if not name:
                raise BadRequest("Company name cannot be empty.")
            values["name"] = name
        if "region" in values and values["region"] is not None:
            values["region"] = values["region"].strip() or None

        after = {
            "name": values.get("name", company.name),
            "region": values.get("region", company.region),
            "sectors": values.get("sectors", company.sectors),
            "services": values.get("services", company.services),
            "keywords": values.get("keywords", company.keywords),
            "profile": values.get("profile", company.profile),
            "reminder_opt_in": values.get("reminder_opt_in", company.reminder_opt_in),
            "reminder_lead_days": values.get(
                "reminder_lead_days", company.reminder_lead_days
            ),
        }
        # Discovery terms are validated when a run starts, not when the profile is
        # saved: onboarding saves the company in steps, and a user must be able to
        # add their name and basic details before reaching the capability step.
        before = {
            "name": company.name,
            "region": company.region,
            "sectors": company.sectors,
            "services": company.services,
            "keywords": company.keywords,
            "profile": company.profile,
            "reminder_opt_in": company.reminder_opt_in,
            "reminder_lead_days": company.reminder_lead_days,
        }
        try:
            self.companies.update(company, values)
            self.audit.log(
                company_id=company.id,
                action="company.updated",
                entity_type="company",
                entity_id=company.id,
                before=before,
                after=after,
            )
            self.session.commit()
            self.session.refresh(company)
        except Exception:
            self.session.rollback()
            raise
        return CompanyOut.model_validate(company)

    def list_documents(self, company: Company) -> list[CompanyDocumentOut]:
        documents = self.session.scalars(
            select(CompanyDocument)
            .where(
                CompanyDocument.company_id == company.id,
                CompanyDocument.deleted_at.is_(None),
            )
            .options(selectinload(CompanyDocument.versions))
            .order_by(CompanyDocument.created_at.desc())
        ).all()
        return [self._out(document) for document in documents]

    def upload_document(
        self,
        *,
        company: Company,
        data: bytes,
        filename: str,
        document_type: str,
        title: str | None = None,
        issuer: str | None = None,
        signatory: str | None = None,
        issued_on: date | None = None,
        expires_on: date | None = None,
        financial_year: str | None = None,
        project_value_amount: Decimal | None = None,
        project_value_currency: str | None = None,
        notes: str | None = None,
    ) -> CompanyDocumentOut:
        if document_type not in DOCUMENT_TYPES:
            raise BadRequest(
                "Choose a document type from: " + ", ".join(sorted(DOCUMENT_TYPES)) + "."
            )
        if issued_on and expires_on and expires_on < issued_on:
            raise BadRequest("The expiry date cannot be before the issue date.")

        mime = storage.validate_upload(data, self.settings)
        digest = storage.sha256_bytes(data)

        existing = self.session.scalar(
            select(CompanyDocument)
            .join(
                CompanyDocumentVersion,
                CompanyDocumentVersion.company_document_id == CompanyDocument.id,
            )
            .where(
                CompanyDocument.company_id == company.id,
                CompanyDocument.deleted_at.is_(None),
                CompanyDocumentVersion.sha256 == digest,
            )
            .options(selectinload(CompanyDocument.versions))
            .limit(1)
        )
        if existing is not None:
            return self._out(existing)

        clean_title = (title or "").strip() or Path(filename).stem or "Evidence document"
        currency = (project_value_currency or "INR").upper()
        if len(currency) != 3:
            raise BadRequest("Use a three-letter currency code.")

        try:
            document = CompanyDocument(
                company_id=company.id,
                document_type=document_type,
                title=clean_title,
                issuer=(issuer or "").strip() or None,
                signatory=(signatory or "").strip() or None,
                issued_on=issued_on,
                expires_on=expires_on,
                financial_year=(financial_year or "").strip() or None,
                project_value_amount=project_value_amount,
                project_value_currency=currency,
                notes=(notes or "").strip() or None,
            )
            self.session.add(document)
            self.session.flush()

            relative, digest = storage.save_bytes(
                company_id=company.id, data=data, mime=mime, settings=self.settings
            )
            version = CompanyDocumentVersion(
                company_document_id=document.id,
                sha256=digest,
                storage_path=relative,
                mime=mime,
                bytes=len(data),
            )
            self.session.add(version)
            self.session.flush()

            self.audit.log(
                company_id=company.id,
                action="company.document_uploaded",
                entity_type="company_document",
                entity_id=document.id,
                after={
                    "document_type": document_type,
                    "title": clean_title,
                    "sha256": digest,
                },
            )
            self.session.commit()
            self.session.refresh(document)
        except Exception:
            self.session.rollback()
            raise
        return self._out(document)

    def get_document(self, company: Company, document_id: uuid.UUID) -> CompanyDocument:
        document = self.session.scalar(
            select(CompanyDocument)
            .where(
                CompanyDocument.id == document_id,
                CompanyDocument.company_id == company.id,
                CompanyDocument.deleted_at.is_(None),
            )
            .options(selectinload(CompanyDocument.versions))
        )
        if document is None:
            raise NotFound("Document not found.")
        return document

    def patch_document(
        self,
        *,
        company: Company,
        document_id: uuid.UUID,
        payload: CompanyDocumentPatch,
    ) -> CompanyDocumentOut:
        document = self.get_document(company, document_id)
        values = payload.model_dump(exclude_unset=True)
        if "document_type" in values and values["document_type"] not in DOCUMENT_TYPES:
            raise BadRequest(
                "Choose a document type from: "
                + ", ".join(sorted(DOCUMENT_TYPES))
                + "."
            )
        if "project_value_currency" in values and values["project_value_currency"]:
            currency = values["project_value_currency"].upper()
            if len(currency) != 3:
                raise BadRequest("Use a three-letter currency code.")
            values["project_value_currency"] = currency
        if (
            values.get("issued_on")
            and values.get("expires_on")
            and values["expires_on"] < values["issued_on"]
        ):
            raise BadRequest("The expiry date cannot be before the issue date.")

        before = {
            "document_type": document.document_type,
            "title": document.title,
            "issuer": document.issuer,
            "signatory": document.signatory,
            "issued_on": document.issued_on.isoformat() if document.issued_on else None,
            "expires_on": document.expires_on.isoformat() if document.expires_on else None,
            "financial_year": document.financial_year,
            "project_value_amount": (
                str(document.project_value_amount)
                if document.project_value_amount is not None
                else None
            ),
            "notes": document.notes,
        }
        try:
            for key, value in values.items():
                setattr(document, key, value)
            document.updated_at = utcnow()
            EvidenceRepository(self.session).recompute_for_document(
                document.id, _today()
            )
            self.audit.log(
                company_id=company.id,
                action="company.document_updated",
                entity_type="company_document",
                entity_id=document.id,
                before=before,
                after={key: str(value) for key, value in values.items()},
            )
            self.session.commit()
            self.session.refresh(document)
        except Exception:
            self.session.rollback()
            raise
        return self._out(document)

    def soft_delete_document(self, company: Company, document_id: uuid.UUID) -> None:
        document = self.get_document(company, document_id)
        try:
            document.deleted_at = utcnow()
            document.updated_at = utcnow()
            EvidenceRepository(self.session).recompute_for_document(
                document.id, _today()
            )
            self.audit.log(
                company_id=company.id,
                action="company.document_soft_deleted",
                entity_type="company_document",
                entity_id=document.id,
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def download_document(
        self, company: Company, document_id: uuid.UUID
    ) -> tuple[Path, str, str]:
        document = self.get_document(company, document_id)
        version = self._latest_version(document)
        if version is None:
            raise NotFound("The document has no stored file.")
        path = storage.resolve_path(version.storage_path, self.settings)
        if not path.exists():
            raise NotFound("The stored file is missing.")
        suffix = Path(version.storage_path).suffix or ""
        return path, version.mime, f"{document.title}{suffix}"

    def _latest_version(
        self, document: CompanyDocument
    ) -> CompanyDocumentVersion | None:
        if not document.versions:
            return None
        return max(
            document.versions,
            key=lambda version: (version.uploaded_at, version.created_at),
        )

    def _out(self, document: CompanyDocument) -> CompanyDocumentOut:
        version = self._latest_version(document)
        return CompanyDocumentOut(
            id=document.id,
            document_type=document.document_type,
            title=document.title,
            issuer=document.issuer,
            signatory=document.signatory,
            issued_on=document.issued_on,
            expires_on=document.expires_on,
            financial_year=document.financial_year,
            project_value_amount=document.project_value_amount,
            project_value_currency=document.project_value_currency,
            notes=document.notes,
            verification_status=document.verification_status,
            created_at=document.created_at,
            updated_at=document.updated_at,
            sha256=version.sha256 if version else None,
            mime=version.mime if version else None,
            bytes=version.bytes if version else None,
            extraction_state=document.extraction_state or "pending",
            extraction_error=document.extraction_error,
            extracted_at=document.extracted_at,
            term_count=document.term_count or 0,
            understanding_state=document.understanding_state or "pending",
            understanding_error=document.understanding_error,
            understanding_at=document.understanding_at,
            capability_count=document.capability_count or 0,
            experience_count=document.experience_count or 0,
        )
