from fastapi import APIRouter

from app.api.routes import (
    assessment,
    auth,
    company,
    health,
    jobs,
    requirements,
    tender_documents,
    tenders,
    workflow,
)
from app.discovery.routes import fetch_router as discovery_fetch_router
from app.discovery.routes import router as discovery_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(company.router)
api_router.include_router(jobs.router)
api_router.include_router(tenders.router)
api_router.include_router(tender_documents.router)
api_router.include_router(requirements.router)
api_router.include_router(assessment.router)
api_router.include_router(workflow.router)
api_router.include_router(discovery_router)
api_router.include_router(discovery_fetch_router)
