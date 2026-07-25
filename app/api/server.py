"""
api/server.py
-------------
FastAPI app that serves the dashboard HTML and a small JSON API over the
existing SQLite store. Read-only — no mutations from the web layer.

Run with:
    python -m app.api.server
or:
    uvicorn app.api.server:app --reload --port 8000
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.classify.scoring import SEVERITY_ORDER
from app.config import config
from app.db.database import get_session, init_db
from app.db.models import Event

log = logging.getLogger("odin.api")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Project Odin Overwatch",
    description="Public-source defense event monitor dashboard.",
    version="0.2.0",
)

# Static files (just the dashboard HTML for now).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _on_startup() -> None:
    config.configure_logging()
    init_db()
    log.info("Odin Overwatch dashboard ready at http://127.0.0.1:8000/")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH"}
VALID_TYPES = {"DRONE", "MISSILE", "ROCKET", "AIR_DEFENSE", "AIRSPACE", "UNKNOWN"}


def _serialize_event(ev: Event) -> Dict[str, Any]:
    return {
        "id": ev.id,
        "title": ev.title,
        "url": ev.url,
        "domain": ev.domain,
        "snippet": ev.snippet,
        "event_type": ev.event_type,
        "severity": ev.severity,
        "score": ev.score,
        "matched_keywords": ev.matched_keywords,
        "location_name": ev.location_name,
        "country": ev.country,
        "latitude": ev.latitude,
        "longitude": ev.longitude,
        "published_at": ev.published_at.isoformat() if ev.published_at else None,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
        "notified": bool(ev.notified),
    }


def _severity_at_least_clause(min_severity: str):
    """Build a WHERE clause for severity >= min_severity."""
    idx = SEVERITY_ORDER.index(min_severity)
    allowed = SEVERITY_ORDER[idx:]
    return Event.severity.in_(allowed)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    """Serve the dashboard HTML."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Dashboard HTML missing.")
    return FileResponse(str(html_path))


@app.get("/api/events")
async def list_events(
    hours: int = Query(72, ge=1, le=24 * 30, description="Lookback window."),
    event_type: Optional[str] = Query(None, description="Filter by event type."),
    min_severity: Optional[str] = Query(None, description="LOW / MEDIUM / HIGH."),
    country: Optional[str] = Query(None),
    geo_only: bool = Query(True, description="Only return events with lat/lon."),
    include_unknown: bool = Query(
        False,
        description="Include UNKNOWN events (off by default — they're noise).",
    ),
    limit: int = Query(1000, ge=1, le=5000),
) -> JSONResponse:
    """Return events matching the filters. Newest first."""
    if event_type:
        event_type = event_type.upper()
        if event_type not in VALID_TYPES:
            raise HTTPException(400, f"Invalid event_type. Expected one of {sorted(VALID_TYPES)}.")
    if min_severity:
        min_severity = min_severity.upper()
        if min_severity not in VALID_SEVERITIES:
            raise HTTPException(400, f"Invalid min_severity. Expected one of {sorted(VALID_SEVERITIES)}.")

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    stmt = select(Event).where(Event.created_at >= cutoff)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    elif not include_unknown:
        stmt = stmt.where(Event.event_type != "UNKNOWN")
    if min_severity:
        stmt = stmt.where(_severity_at_least_clause(min_severity))
    if country:
        stmt = stmt.where(Event.country == country)
    if geo_only:
        stmt = stmt.where(Event.latitude.is_not(None), Event.longitude.is_not(None))
    stmt = stmt.order_by(Event.created_at.desc()).limit(limit)

    with get_session() as session:
        events = list(session.scalars(stmt))

    return JSONResponse(
        {
            "count": len(events),
            "filters": {
                "hours": hours,
                "event_type": event_type,
                "min_severity": min_severity,
                "country": country,
                "geo_only": geo_only,
                "limit": limit,
            },
            "events": [_serialize_event(e) for e in events],
        }
    )


@app.get("/api/stats")
async def stats(hours: int = Query(72, ge=1, le=24 * 30)) -> JSONResponse:
    """Aggregate counts by event_type, severity, and country."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    with get_session() as session:
        total = session.scalar(
            select(func.count(Event.id)).where(Event.created_at >= cutoff)
        ) or 0

        types = dict(
            session.execute(
                select(Event.event_type, func.count(Event.id))
                .where(Event.created_at >= cutoff)
                .group_by(Event.event_type)
            ).all()
        )

        severities = dict(
            session.execute(
                select(Event.severity, func.count(Event.id))
                .where(Event.created_at >= cutoff)
                .group_by(Event.severity)
            ).all()
        )

        country_rows = session.execute(
            select(Event.country, func.count(Event.id))
            .where(Event.created_at >= cutoff, Event.country.is_not(None))
            .group_by(Event.country)
            .order_by(func.count(Event.id).desc())
            .limit(15)
        ).all()
        countries = [{"country": c, "count": n} for c, n in country_rows]

        geo_count = session.scalar(
            select(func.count(Event.id)).where(
                Event.created_at >= cutoff,
                Event.latitude.is_not(None),
            )
        ) or 0

    return JSONResponse(
        {
            "hours": hours,
            "total": total,
            "geo_tagged": geo_count,
            "by_type": types,
            "by_severity": severities,
            "top_countries": countries,
        }
    )


@app.get("/api/timeline")
async def timeline(
    hours: int = Query(72, ge=1, le=24 * 30),
    include_unknown: bool = Query(False),
) -> JSONResponse:
    """Return event counts bucketed by time, broken down by event_type.

    Bucket size is adaptive: hourly for short windows, 6h for medium,
    daily for long. The dashboard's timeline chart consumes this.
    """
    if hours <= 48:
        bucket_hours = 1
    elif hours <= 24 * 7:
        bucket_hours = 6
    else:
        bucket_hours = 24

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    stmt = select(Event.created_at, Event.event_type).where(Event.created_at >= cutoff)
    if not include_unknown:
        stmt = stmt.where(Event.event_type != "UNKNOWN")

    with get_session() as session:
        rows = session.execute(stmt).all()

    # Build bucket keys aligned to bucket boundaries (anchored at the
    # cutoff so the chart always has the same x-axis layout).
    bucket_seconds = bucket_hours * 3600
    buckets: Dict[int, Dict[str, int]] = {}
    for created_at, etype in rows:
        if created_at is None:
            continue
        offset = int((created_at - cutoff).total_seconds() // bucket_seconds)
        buckets.setdefault(offset, {}).setdefault(etype, 0)
        buckets[offset][etype] = buckets[offset].get(etype, 0) + 1

    # Emit every bucket from 0 to last, including zeros, so the chart
    # x-axis has a contiguous timeline.
    last_offset = int((now - cutoff).total_seconds() // bucket_seconds)
    series: List[Dict[str, Any]] = []
    for i in range(last_offset + 1):
        ts = cutoff + timedelta(seconds=i * bucket_seconds)
        item = {"ts": ts.isoformat(), **buckets.get(i, {})}
        series.append(item)

    return JSONResponse({
        "hours": hours,
        "bucket_hours": bucket_hours,
        "series": series,
    })


@app.get("/api/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Run as a script (convenient alternative to invoking uvicorn directly).
# -----------------------------------------------------------------------------

def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "app.api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
