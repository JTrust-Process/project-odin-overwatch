"""
models.py
---------
SQLAlchemy ORM models for Project Odin Overwatch.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    """Naive UTC timestamp. SQLite stores DATETIME as ISO strings; mixing
    TZ-aware and TZ-naive values causes comparison warnings, so we
    standardize on naive UTC across the codebase."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Source
    source = Column(String(64), nullable=False, default="gdelt")
    domain = Column(String(255), nullable=True)

    # Article
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False, unique=True)
    snippet = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)

    # Classification
    event_type = Column(String(32), nullable=False, default="UNKNOWN")
    severity = Column(String(16), nullable=False, default="LOW")
    score = Column(Integer, nullable=False, default=0)
    matched_keywords = Column(Text, nullable=True)  # comma-separated

    # Geo enrichment (best-effort; may be NULL)
    location_name = Column(String(128), nullable=True)
    country = Column(String(64), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Lifecycle
    notified = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_events_event_type", "event_type"),
        Index("ix_events_severity", "severity"),
        Index("ix_events_created_at", "created_at"),
        Index("ix_events_title", "title"),
        Index("ix_events_notified", "notified"),
        Index("ix_events_country", "country"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Event id={self.id} type={self.event_type} "
            f"sev={self.severity} loc={self.location_name!r} url={self.url!r}>"
        )
