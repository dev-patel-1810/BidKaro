from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.corpus import TenderDocument
from app.repositories.corpus import (
    DocumentRepository,
    RequirementRepository,
    TenderAuxRepository,
    TenderRepository,
)
from app.repositories.identity import AuditRepository
from app.services import storage
from app.services.corpus_rules import document_key, next_version, trim
from app.services.ocr import OcrUnavailable, build_ocr
from app.services.pdf_extraction import PdfExtractionError, extract_pdf_pages

MAX_FILENAME_CHARS = 200


class DocumentService:
    """Tender document storage, page extraction and version lifecycle.

    The corpus is global. A document is keyed by its bytes (`sha256`) and its
    logical identity (`document_key`); a newer file with the same logical name
    becomes a new version and the old version stops being current. Nothing is
    ever deleted here.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.tenders = TenderRepository(session)
        self.documents = DocumentRepository(session)
        self.requirements = RequirementRepository(session)
        self.aux = TenderAuxRepository(session)
        self.audit = AuditRepository(session)

    def upload(
        self,
        *,
        tender_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        source_url: str | None = None,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> TenderDocument:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        document = self.attach(
            tender_id=tender.id,
            filename=filename,
            data=data,
            source_url=source_url,
        )
        self.audit.log(
            action="tender_document.uploaded",
            entity_type="tender_document",
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_id=document.id,
            after={
                "tender_id": str(tender.id),
                "filename": document.filename,
                "sha256": document.sha256,
                "version": document.version,
                "extraction_state": document.extraction_state,
                "ocr_page_count": document.ocr_page_count,
            },
        )
        self.session.commit()
        return document

    def upload_archive(
        self,
        *,
        tender_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        source_url: str | None = None,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[list[TenderDocument], list[str]]:
        """Attach every PDF inside an uploaded zip; report what was skipped.

        A zip is a container, not a document: each PDF inside it becomes its
        own tender document so a page, a citation and a requirement always
        point at one file the user can open. A member that is not a PDF, is too
        large or cannot be read is reported rather than silently dropped.
        """

        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        if data[:2] != b"PK":
            raise BadRequest("That file is not a zip archive.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise BadRequest("The uploaded zip could not be opened.") from exc

        limit = self.settings.max_upload_mb * 1024 * 1024
        documents: list[TenderDocument] = []
        skipped: list[str] = []
        total = 0
        for info in archive.infolist()[:60]:
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name.lower().endswith(".pdf"):
                skipped.append(f"{name}: not a PDF, so it was not attached.")
                continue
            if info.file_size > limit:
                skipped.append(f"{name}: larger than the upload limit.")
                continue
            total += info.file_size
            if total > limit * 3:
                skipped.append("The zip holds more than the upload limit.")
                break
            try:
                content = archive.read(info)
            except Exception:  # noqa: BLE001 - one member must not stop the rest
                skipped.append(f"{name}: could not be read from the zip.")
                continue
            try:
                document = self.attach(
                    tender_id=tender.id,
                    filename=name,
                    data=content,
                    source_url=source_url,
                )
            except BadRequest as exc:
                skipped.append(f"{name}: {exc}")
                continue
            documents.append(document)
            self.audit.log(
                action="tender_document.uploaded",
                entity_type="tender_document",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=document.id,
                after={
                    "tender_id": str(tender.id),
                    "filename": document.filename,
                    "sha256": document.sha256,
                    "version": document.version,
                    "extraction_state": document.extraction_state,
                    "ocr_page_count": document.ocr_page_count,
                    "archive": trim(filename, MAX_FILENAME_CHARS),
                },
            )
        self.session.commit()
        return documents, skipped

    def attach(
        self,
        *,
        tender_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        source_url: str | None = None,
        fetched_at: datetime | None = None,
    ) -> TenderDocument:
        """Attach a PDF without committing, so a manual tender can be atomic."""

        mime = storage.validate_upload(data, self.settings)
        if mime != "application/pdf":
            raise BadRequest("Tender documents must be PDF files.")
        original = storage.sanitize_original_filename(filename)[:MAX_FILENAME_CHARS]
        digest = storage.sha256_bytes(data)

        existing = self.documents.find_by_sha(tender_id, digest)
        if existing is not None:
            return existing

        page_rows, page_count, has_text_layer, ocr_page_count, extraction_state, error = (
            self._extract(data)
        )

        key = document_key(original)
        current = self.documents.current_for_key(tender_id, key)
        version = "v1"
        if current is not None:
            if current.sha256 == digest:
                return current
            version = next_version(self._latest_version_text(tender_id, key))
            current.is_current = False
            self.session.flush()

        relative, _ = storage.save_tender_pdf(
            tender_id=tender_id, data=data, settings=self.settings
        )
        document = self.documents.create(
            {
                "tender_id": tender_id,
                "filename": original,
                "sha256": digest,
                "version": version,
                "document_key": key,
                "supersedes_document_id": current.id if current is not None else None,
                "is_current": True,
                "storage_path": relative,
                "mime": mime,
                "bytes": len(data),
                "page_count": page_count,
                "has_text_layer": has_text_layer,
                "ocr_page_count": ocr_page_count,
                "extraction_state": extraction_state,
                "extraction_error": error,
                "source_url": trim(source_url, 2000),
                "fetched_at": fetched_at,
            }
        )
        for row in page_rows:
            self.documents.add_page(
                document_id=document.id,
                page_index=row.page_index,
                printed_label=row.printed_label,
                text=row.text,
                char_count=row.char_count,
                is_ocr=row.text_source == "ocr",
            )
        self.session.flush()
        tender = self.tenders.get(tender_id)
        if tender is not None:
            self.tenders.touch(tender)
        return document

    def _latest_version_text(self, tender_id: uuid.UUID, key: str) -> str | None:
        latest = self.documents.latest_version(tender_id, key)
        return latest.version if latest is not None else None

    def _extract(self, data: bytes):
        ocr = None
        ocr_status = "disabled" if not self.settings.ocr_enabled else "not_needed"
        if self.settings.ocr_enabled:
            try:
                ocr = build_ocr(
                    language=self.settings.ocr_language,
                    tesseract_cmd=self.settings.tesseract_cmd,
                    timeout_seconds=self.settings.ocr_timeout_seconds,
                )
            except OcrUnavailable:
                ocr_status = "unavailable"

        try:
            extraction = extract_pdf_pages(
                data,
                max_pages=self.settings.pdf_max_pages,
                ocr=ocr,
                ocr_dpi=self.settings.ocr_dpi,
                ocr_max_pages=self.settings.ocr_max_pages,
            )
        except PdfExtractionError as exc:
            raise BadRequest(f"The PDF could not be read: {exc}") from exc

        if self.settings.ocr_enabled and ocr is not None:
            if extraction.ocr_page_count and (
                extraction.ocr_failed_pages or extraction.ocr_skipped_pages
            ):
                ocr_status = "partial"
            elif extraction.ocr_page_count:
                ocr_status = "applied"
            elif extraction.text_page_count < extraction.page_count:
                ocr_status = "failed"
        elif ocr_status == "unavailable" and extraction.text_page_count == 0:
            ocr_status = "unavailable"

        error = None
        if extraction.text_page_count == 0:
            if ocr_status == "unavailable":
                error = (
                    "No text layer was found and OCR is unavailable. "
                    "Upload a clearer digital copy or install Tesseract."
                )
            elif ocr_status == "disabled":
                error = "No text layer was found and OCR is disabled."
            else:
                error = "No usable text was found in this PDF."

        state = "done" if extraction.text_page_count else "no_text_layer"
        has_text_layer = any(
            page.has_text and page.text_source == "text_layer"
            for page in extraction.pages
        )
        return (
            extraction.pages,
            extraction.page_count,
            has_text_layer,
            extraction.ocr_page_count,
            state,
            error,
        )

    def get(self, document_id: uuid.UUID) -> TenderDocument:
        document = self.documents.get(document_id)
        if document is None:
            raise NotFound("Tender document not found.")
        return document

    def list_for_tender(
        self, tender_id: uuid.UUID, *, include_non_current: bool = True
    ) -> list[TenderDocument]:
        if self.tenders.get(tender_id) is None:
            raise NotFound("Tender not found.")
        return self.documents.list_for_tender(
            tender_id, include_non_current=include_non_current
        )

    def pages(self, document_id: uuid.UUID):
        self.get(document_id)
        return self.documents.pages(document_id)

    def page(self, document_id: uuid.UUID, page_index: int):
        self.get(document_id)
        page = self.documents.page(document_id, page_index)
        if page is None:
            raise NotFound("Page not found.")
        return page

    def path(self, document_id: uuid.UUID) -> tuple[TenderDocument, Path]:
        document = self.get(document_id)
        path = storage.resolve_stored_path(document.storage_path, self.settings)
        if not path.is_file():
            raise NotFound("The stored PDF is unavailable.")
        return document, path

    def citation_counts(self, document_id: uuid.UUID) -> dict[str, int]:
        counts = {
            "verified": 0,
            "needs_review": 0,
            "accepted_ocr": 0,
            "unverified": 0,
            "rejected": 0,
            "total": 0,
        }
        rows = self.requirements.list_for_document(document_id)
        for row in rows:
            counts[row.state] = counts.get(row.state, 0) + 1
            counts["total"] += 1
        return counts

    def covers(self, tender_id: uuid.UUID):
        return self.aux.covers(tender_id)

    def fetch_state(self, tender_id: uuid.UUID):
        return self.aux.fetch_state(tender_id)

    def sync_fetch_state(self, tender_id: uuid.UUID) -> None:
        count = self.documents.count_current(tender_id)
        state = self.aux.fetch_state(tender_id)
        if state is None:
            if count == 0:
                return
            self.aux.ensure_fetch_state(tender_id, documents_present=count)
        else:
            state.documents_present = count
            state.updated_at = utcnow()
        self.session.flush()
