from __future__ import annotations

import uuid

from app.core.db import SessionLocal
from app.core.errors import BadRequest, ServiceUnavailable
from app.integrations.ai import build_ai_provider
from app.models.corpus import Tender
from app.models.identity import Company
from app.services.company_intel import CompanyDocumentIntelService
from app.services.job_registry import register_handler
from app.services.job_types import JobResult
from app.services.requirement_service import RequirementService
from app.services.similarity import TenderSimilarityService
from app.services.workflow import WorkflowService


def handle_extract_requirements(context) -> JobResult:
    payload = context.payload or {}
    document_id = uuid.UUID(str(payload["document_id"]))
    replace = bool(payload.get("replace"))
    company_id = context.company_id
    if company_id is None and payload.get("company_id"):
        company_id = uuid.UUID(str(payload["company_id"]))
    user_id = (
        uuid.UUID(str(payload["user_id"])) if payload.get("user_id") else None
    )

    with SessionLocal() as session:
        result = RequirementService(
            session, ai=build_ai_provider()
        ).extract(
            document_id=document_id,
            replace=replace,
            company_id=company_id,
            actor_user_id=user_id,
            progress=context.heartbeat,
        )

    state = "partial" if result.stats.failed_chunks else "done"
    return JobResult(
        state=state,
        metrics=result.metrics(),
        notes=result.message,
    )


def handle_snapshot_tender(context) -> JobResult:
    payload = context.payload or {}
    tender_id = uuid.UUID(str(payload["tender_id"]))
    company_id = context.company_id
    if company_id is None and payload.get("company_id"):
        company_id = uuid.UUID(str(payload["company_id"]))
    user_id = (
        uuid.UUID(str(payload["user_id"])) if payload.get("user_id") else None
    )

    with SessionLocal() as session:
        snapshot, created, changes = WorkflowService(session).capture_snapshot(
            tender_id,
            company_id=company_id,
            actor_user_id=user_id,
        )
        return JobResult(
            state="done",
            metrics={
                "snapshot_id": str(snapshot.id),
                "created": created,
                "changes": len(changes),
            },
            notes=(
                "Captured a new tender snapshot."
                if created
                else "Nothing changed since the latest snapshot."
            ),
        )


def handle_extract_company_document(context) -> JobResult:
    payload = context.payload or {}
    document_id = uuid.UUID(str(payload["document_id"]))
    company_id = context.company_id
    if company_id is None and payload.get("company_id"):
        company_id = uuid.UUID(str(payload["company_id"]))

    with SessionLocal() as session:
        result = CompanyDocumentIntelService(
            session, ai=build_ai_provider()
        ).extract(
            document_id=document_id,
            company_id=company_id,
            progress=context.heartbeat,
        )

    state = "done" if result["state"] == "done" else "partial"
    return JobResult(
        state=state,
        metrics={
            "document_id": result["document_id"],
            "extraction_state": result["state"],
            "terms": result["terms"],
            "understanding_state": result["understanding_state"],
            "capabilities": result["capabilities"],
            "experiences": result["experiences"],
        },
        notes=result.get("error") or result.get("understanding_error"),
    )


def handle_judge_tender_similarity(context) -> JobResult:
    payload = context.payload or {}
    tender_ids = [
        uuid.UUID(str(item)) for item in (payload.get("tender_ids") or [])
    ]
    company_id = context.company_id

    with SessionLocal() as session:
        company = session.get(Company, company_id) if company_id else None
        if company is None:
            return JobResult(
                state="failed",
                notes="The company for this comparison no longer exists.",
            )
        tenders = [
            tender
            for tender in (session.get(Tender, tender_id) for tender_id in tender_ids)
            if tender is not None
        ]
        if not tenders:
            return JobResult(
                state="partial", notes="There were no tenders to compare."
            )
        try:
            result = TenderSimilarityService(
                session, ai=build_ai_provider()
            ).judge_many(company=company, tenders=tenders)
        except (BadRequest, ServiceUnavailable) as exc:
            return JobResult(state="partial", notes=str(exc))

    answers = [item["answer"] for item in result.values()]
    return JobResult(
        state="done",
        metrics={
            "judged": len(result),
            "yes": answers.count("yes"),
            "no": answers.count("no"),
            "unsure": answers.count("unsure"),
        },
        notes=(
            f"Compared {len(result)} tender(s) with your confirmed "
            "capabilities. These answers never decide eligibility."
        ),
    )


def register_builtin_handlers() -> None:
    """Handlers that belong to the base, not to the scraping module.

    Requirement extraction is part of the document base: it works on an
    uploaded PDF even when no portal module is installed. Snapshot capture is
    also base behaviour: it versions the global corpus without a browser.
    Company-profile reading is base behaviour too: it turns an uploaded
    ``about_company`` PDF into work terms, without a browser and without
    touching eligibility.
    """

    register_handler("extract_requirements", handle_extract_requirements)
    register_handler("extract_company_document", handle_extract_company_document)
    register_handler("judge_tender_similarity", handle_judge_tender_similarity)
    register_handler("snapshot_tender", handle_snapshot_tender)
