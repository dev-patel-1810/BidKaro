from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.schema import schema_status
from app.core.settings import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Liveness plus dependency status.

    The app is alive even when Postgres is not, so the response is always 200
    and the dependency is reported honestly rather than hidden behind a 500.
    The schema revision is reported with it: a database behind the migration
    head is ``degraded`` and says which revision it is stamped, so a 500 on a
    list route can be traced to the deployment instead of the query.
    """

    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = "down"

    schema: dict = {"revision": None, "head": None, "up_to_date": False}
    if database == "ok":
        try:
            schema = schema_status(db)
        except SQLAlchemyError:
            db.rollback()
            schema = {"revision": None, "head": None, "up_to_date": False}

    healthy = database == "ok" and schema["up_to_date"]
    return {
        "status": "ok" if healthy else "degraded",
        "database": database,
        "schema": schema,
        "features": {
            "discovery": settings.discovery_enabled,
        },
    }
