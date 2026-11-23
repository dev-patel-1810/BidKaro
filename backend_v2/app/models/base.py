from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for the v2 models.

    Models arrive stage by stage. The initial Alembic migration is hand-written
    from SCHEMA-SPEC.md, so an empty metadata object here is expected at
    stage A.
    """
