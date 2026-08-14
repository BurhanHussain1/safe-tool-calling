"""Database engine and sessions.

SQLite by default; Postgres by changing ``NL2API_DATABASE_URL`` and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from nl2api.config import Settings, get_settings
from nl2api.persistence.models import Base


def build_engine(url: str) -> Engine:
    # check_same_thread is a SQLite-only concern: FastAPI serves requests from a
    # thread pool, and the default would reject a connection reused across them.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    engine = build_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    if settings is None:
        return _default_engine()
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on anything else."""
    session = session_factory(engine or _default_engine())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
