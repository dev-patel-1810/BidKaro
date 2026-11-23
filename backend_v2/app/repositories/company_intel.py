"""Storage for company-document page text and extracted work terms.

The terms are derived read-time signals, so the repository keeps them cheap to
fetch for a whole company and keeps a soft-deleted document out of the answer.
Nothing here touches eligibility: an expired or unverified document still stops
counting as evidence, but its work words stop counting the moment it is read
(or the document is removed).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.identity import (
    CompanyDocument,
    CompanyDocumentPage,
    CompanyDocumentTerm,
    CompanyDocumentVersion,
)


class CompanyDocumentIntelRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_version(
        self, document_id: uuid.UUID
    ) -> CompanyDocumentVersion | None:
        return self.session.scalar(
            select(CompanyDocumentVersion)
            .where(CompanyDocumentVersion.company_document_id == document_id)
            .order_by(
                CompanyDocumentVersion.uploaded_at.desc(),
                CompanyDocumentVersion.created_at.desc(),
            )
            .limit(1)
        )

    def terms_for_company(self, company_id: uuid.UUID) -> list[str]:
        """Every live term for a company, as display text.

        A soft-deleted document's terms stay in the table (the row is kept for
        audit) but are excluded here, which is what makes "delete the profile"
        stop influencing suggestions without a second write.
        """

        rows = self.session.execute(
            select(CompanyDocumentTerm.term)
            .join(
                CompanyDocument,
                CompanyDocument.id == CompanyDocumentTerm.company_document_id,
            )
            .where(
                CompanyDocumentTerm.company_id == company_id,
                CompanyDocument.deleted_at.is_(None),
                # A reclassified document is no longer a work profile. Its
                # terms stay on the row (audit) but stop counting immediately.
                CompanyDocument.document_type == "about_company",
            )
            .order_by(CompanyDocumentTerm.term)
        ).all()
        seen: dict[str, str] = {}
        for (term,) in rows:
            cleaned = " ".join((term or "").split())
            if cleaned and cleaned.casefold() not in seen:
                seen[cleaned.casefold()] = cleaned
        return list(seen.values())

    def terms_for_document(self, document_id: uuid.UUID) -> list[CompanyDocumentTerm]:
        return list(
            self.session.scalars(
                select(CompanyDocumentTerm)
                .where(CompanyDocumentTerm.company_document_id == document_id)
                .order_by(CompanyDocumentTerm.term)
            ).all()
        )

    def get_term(
        self, *, document_id: uuid.UUID, company_id: uuid.UUID, term_id: uuid.UUID
    ) -> CompanyDocumentTerm | None:
        return self.session.scalar(
            select(CompanyDocumentTerm).where(
                CompanyDocumentTerm.id == term_id,
                CompanyDocumentTerm.company_document_id == document_id,
                CompanyDocumentTerm.company_id == company_id,
            )
        )

    def count_terms(self, document_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(CompanyDocumentTerm.id)).where(
                    CompanyDocumentTerm.company_document_id == document_id
                )
            )
            or 0
        )

    def replace_pages(
        self, version_id: uuid.UUID, rows: list[dict]
    ) -> None:
        self.session.execute(
            delete(CompanyDocumentPage).where(
                CompanyDocumentPage.company_document_version_id == version_id
            )
        )
        for row in rows:
            self.session.add(
                CompanyDocumentPage(
                    company_document_version_id=version_id,
                    page_index=row["page_index"],
                    printed_label=row.get("printed_label"),
                    text=row.get("text") or "",
                    char_count=row.get("char_count") or 0,
                    is_ocr=bool(row.get("is_ocr")),
                )
            )
        self.session.flush()

    def replace_terms(
        self, *, document_id: uuid.UUID, company_id: uuid.UUID, rows: list[dict]
    ) -> None:
        self.session.execute(
            delete(CompanyDocumentTerm).where(
                CompanyDocumentTerm.company_document_id == document_id
            )
        )
        for row in rows:
            self.session.add(
                CompanyDocumentTerm(
                    company_id=company_id,
                    company_document_id=document_id,
                    term=row["term"],
                    normalized=row["normalized"],
                    page_index=row["page_index"],
                    printed_label=row.get("printed_label"),
                    excerpt=row["excerpt"],
                    source=row.get("source") or "text_rule",
                    rules_version=row["rules_version"],
                )
            )
        self.session.flush()

    def delete_term(self, term: CompanyDocumentTerm) -> None:
        self.session.delete(term)
        self.session.flush()
