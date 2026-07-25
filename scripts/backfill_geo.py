"""
scripts/backfill_geo.py
-----------------------
One-off script: walk the events table and run geo enrichment on rows
that don't have lat/lon yet. Safe to run multiple times — only updates
rows where latitude IS NULL.

Run from the project root:
    python -m scripts.backfill_geo
"""

from __future__ import annotations

import logging
from typing import List

from sqlalchemy import select

from app.config import config
from app.db.database import get_session, init_db
from app.db.models import Event
from app.enrich.geo import find_location

log = logging.getLogger("odin.backfill")


def main() -> None:
    config.configure_logging()
    init_db()

    log.info("Scanning for un-enriched events...")

    with get_session() as session:
        stmt = select(Event).where(Event.latitude.is_(None))
        events: List[Event] = list(session.scalars(stmt))
        log.info("Found %d event(s) without geo data.", len(events))

        updated = 0
        for ev in events:
            text = " ".join([ev.title or "", ev.snippet or ""])
            place = find_location(text)
            if place is None:
                continue
            ev.location_name = place.name
            ev.country = place.country or place.name
            ev.latitude = place.lat
            ev.longitude = place.lon
            updated += 1
        # commit happens at context-manager exit
    log.info("Backfill complete: %d / %d updated.", updated, len(events))


if __name__ == "__main__":
    main()
