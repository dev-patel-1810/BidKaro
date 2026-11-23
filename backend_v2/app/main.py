import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.schema import ensure_schema_current, schema_status
from app.core.settings import get_settings
from app.discovery.handlers import register_discovery
from app.discovery.sessions import close_all as close_discovery_sessions
from app.services.builtin_handlers import register_builtin_handlers
from app.services.runner import JobRunner
from app.services.scheduler import Scheduler

logger = logging.getLogger(__name__)

settings = get_settings()


def _check_schema() -> None:
    """Refuse to serve a schema that is behind the migrations.

    An unreachable database is not this check's business: the app stays alive
    and ``/api/health`` reports the database as down. A reachable database at
    the wrong revision is refused, because every request against it would 500
    with an ``UndefinedTable`` that says nothing about the real cause.
    """

    try:
        with SessionLocal() as session:
            status = schema_status(session)
    except SQLAlchemyError as exc:
        logger.warning("Could not read the database schema at startup: %s", exc)
        return
    ensure_schema_current(status, strict=settings.schema_check)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Check the schema, then start durable workers only when enabled.

    Tests and the scraper-less base set ``JOBS_ENABLED=false``; the routes still
    work and a worker can be constructed explicitly in a test. With jobs on,
    the scheduler is just another job producer, so it is gated behind the same
    switch.
    """

    _check_schema()
    runner: JobRunner | None = None
    scheduler: Scheduler | None = None
    register_builtin_handlers()
    register_discovery()
    if settings.jobs_enabled:
        runner = JobRunner(settings=settings)
        runner.start()
        app.state.job_runner = runner
        if settings.scheduler_enabled:
            scheduler = Scheduler(settings=settings)
            scheduler.start()
            app.state.scheduler = scheduler
    yield
    if scheduler is not None:
        scheduler.stop()
    if runner is not None:
        runner.stop()
    close_discovery_sessions()


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(api_router, prefix=settings.api_prefix)
