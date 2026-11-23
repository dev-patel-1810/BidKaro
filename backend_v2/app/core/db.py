from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings

_settings = get_settings()

# A single pool, one transaction boundary. The service layer owns commits;
# repositories receive this session and never open their own (CODING-GUIDE §5).
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
