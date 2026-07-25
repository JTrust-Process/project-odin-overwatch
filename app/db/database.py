"""
database.py
-----------
SQLAlchemy bootstrap + a single read helper + lightweight migrations.

Transaction boundaries live in the caller (app/main.py) so we can commit
each event independently — that's the only way to avoid duplicate
notifications when one insert in a batch fails.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, config
from app.db.models import Base, Event

log = logging.getLogger(__name__)


def _resolve_sqlite_path(url: str) -> str:
    """Make sqlite:///data/odin.db resolve against the project root, not cwd.
    Critical when running from cron / Task Scheduler."""
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        rel = url[len(prefix):]
        abs_path = (PROJECT_ROOT / rel).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}{abs_path.as_posix()}"
    return url


_engine = create_engine(
    _resolve_sqlite_path(config.database_url),
    echo=False,
    future=True,
)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


# Columns we've added since the table was first created. Used by the
# tiny migration shim below so users don't have to wipe their DB when
# the schema grows.
_EXPECTED_COLUMNS = {
    "location_name": "VARCHAR(128)",
    "country": "VARCHAR(64)",
    "latitude": "FLOAT",
    "longitude": "FLOAT",
}


def _apply_lightweight_migrations() -> None:
    """Idempotently add any missing columns to events. SQLite-only.
    For production-grade schema management, switch to Alembic."""
    insp = inspect(_engine)
    if not insp.has_table("events"):
        return  # create_all will handle a fresh DB
    existing = {c["name"] for c in insp.get_columns("events")}
    missing = {k: v for k, v in _EXPECTED_COLUMNS.items() if k not in existing}
    if not missing:
        return
    with _engine.begin() as conn:
        for col, coltype in missing.items():
            conn.execute(text(f"ALTER TABLE events ADD COLUMN {col} {coltype}"))
            log.info("Migrated: added column events.%s (%s)", col, coltype)


def init_db() -> None:
    """Create tables if they don't exist, then apply any pending column
    additions. Idempotent — safe to call on every run."""
    Base.metadata.create_all(_engine)
    _apply_lightweight_migrations()
    log.debug("Database ready at %s", _engine.url)


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a session, commit on clean exit, rollback on exception.

    Designed to be opened *per logical unit of work* (typically one event).
    Don't wrap the whole pipeline in a single session.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def event_exists(session: Session, url: str, title: str) -> bool:
    """Dedup check: an event is duplicate if either URL or exact
    (normalized) title is already in the database."""
    stmt = select(Event.id).where((Event.url == url) | (Event.title == title))
    return session.execute(stmt).first() is not None
