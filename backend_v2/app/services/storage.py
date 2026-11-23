from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from app.core.errors import BadRequest
from app.core.settings import Settings, get_settings

PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_original_filename(filename: str | None) -> str:
    name = Path(filename or "document").name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:200] or "document"


def detect_mime(data: bytes) -> str:
    if data.startswith(PDF_MAGIC):
        return "application/pdf"
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    raise BadRequest("Upload a PDF, PNG or JPEG file.")


def extension_for(mime: str) -> str:
    return {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
    }[mime]


def validate_upload(data: bytes, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not data:
        raise BadRequest("The uploaded file is empty.")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise BadRequest(f"The file is larger than {settings.max_upload_mb} MB.")
    return detect_mime(data)


def save_bytes(
    *,
    company_id: uuid.UUID,
    data: bytes,
    mime: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Store bytes content-addressed and return (relative path, sha256)."""
    settings = settings or get_settings()
    digest = sha256_bytes(data)
    relative = Path("company_documents") / str(company_id) / f"{digest}{extension_for(mime)}"
    target = settings.storage_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return str(relative).replace("\\", "/"), digest


def resolve_path(relative: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = settings.storage_dir.resolve()
    path = (root / relative).resolve()
    if root not in path.parents and path != root:
        raise BadRequest("The stored file path is not valid.")
    return path


def save_tender_pdf(
    *,
    tender_id: uuid.UUID,
    data: bytes,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Store a tender PDF content-addressed and return (relative path, sha256)."""
    settings = settings or get_settings()
    digest = sha256_bytes(data)
    relative = Path("tender_documents") / str(tender_id) / f"{digest}.pdf"
    target = settings.storage_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return str(relative).replace("\\", "/"), digest


def resolve_stored_path(relative: str, settings: Settings | None = None) -> Path:
    return resolve_path(relative, settings)
