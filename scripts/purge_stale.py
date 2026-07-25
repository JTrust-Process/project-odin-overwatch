"""
scripts/purge_stale.py
----------------------
One-shot cleanup for events saved under older (broken) classifier rules.

What it does:
  1. Walks every event (default: only notified=0; --all to scan everything).
  2. Re-runs the CURRENT classifier on stored title + snippet.
  3. Updates each row's event_type / severity / score / matched_keywords
     to match current rules.
  4. For rows that reclassify as UNKNOWN, marks notified=1 (dismiss) so
     they stop appearing in the retry queue. With --delete, removes them
     from the table entirely.

The main pipeline's _retry_unnotified() does this incrementally on every
run, so this script is the "do it all right now" button. Useful right
after a rule change.

Run from project root:
    python -m scripts.purge_stale            # dismiss UNKNOWN (default)
    python -m scripts.purge_stale --delete   # actually delete UNKNOWN
    python -m scripts.purge_stale --all      # scan notified=1 rows too
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import delete, select

from app.classify.rules import find_matches
from app.classify.scoring import score_and_severity, severity_at_least
from app.config import config
from app.db.database import get_session, init_db
from app.db.models import Event


def _reevaluate(ev: Event):
    """Source-aware re-evaluation. Mirrors main._reevaluate."""
    src = ev.source or "gdelt"
    stored_matches = [
        k.strip()
        for k in (ev.matched_keywords or "").split(",")
        if k.strip()
    ]
    if src == "launch_library":
        from app.sources.launch_library import reevaluate as ll2_reevaluate
        sev, score = ll2_reevaluate(ev.title or "", ev.matched_keywords or "")
        return "ROCKET", sev, score, stored_matches
    if src == "faa_tfr":
        from app.sources.faa_tfr import reevaluate as faa_reevaluate
        sev, score = faa_reevaluate(ev.title or "", ev.matched_keywords or "")
        return "AIRSPACE", sev, score, stored_matches
    article = {"title": ev.title, "snippet": ev.snippet}
    et, matched = find_matches(article)
    score, sev = score_and_severity(matched)
    return et, sev, score, matched

log = logging.getLogger("odin.purge")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete now-UNKNOWN events instead of marking them notified.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan notified=1 events too (default: only notified=0).",
    )
    args = parser.parse_args()

    config.configure_logging()
    init_db()

    with get_session() as session:
        stmt = select(Event)
        if not args.all:
            stmt = stmt.where(Event.notified == 0)
        events = list(session.scalars(stmt))

    log.info("Scanning %d event(s) (notified%s)...",
             len(events),
             "=0 only" if not args.all else " all")

    reclassified = 0
    dismissed_ids: list[int] = []
    floor = config.min_notify_severity

    # Process each event in its own session to keep transactions small.
    for ev in events:
        new_type, new_severity, new_score, new_matched = _reevaluate(ev)
        new_matched_text = ", ".join(new_matched)

        changed = (
            new_type != ev.event_type
            or new_severity != ev.severity
            or new_score != ev.score
            or new_matched_text != (ev.matched_keywords or "")
        )
        if changed:
            with get_session() as s2:
                row = s2.get(Event, ev.id)
                if row is not None:
                    row.event_type = new_type
                    row.severity = new_severity
                    row.score = new_score
                    row.matched_keywords = new_matched_text
            reclassified += 1

        # Dismiss UNKNOWN events (GDELT-side noise) AND events that no
        # longer meet the notification severity floor (e.g. routine
        # Starlink launches that are now LOW).
        if new_type == "UNKNOWN" or not severity_at_least(new_severity, floor):
            dismissed_ids.append(ev.id)

    log.info("Reclassified %d row(s).", reclassified)
    log.info(
        "%d row(s) will be dismissed (UNKNOWN or below %s under current rules).",
        len(dismissed_ids), floor,
    )

    if not dismissed_ids:
        log.info("Nothing to dismiss. Done.")
        return

    with get_session() as s:
        if args.delete:
            s.execute(delete(Event).where(Event.id.in_(dismissed_ids)))
            log.info("Deleted %d row(s).", len(dismissed_ids))
        else:
            # Mark notified=1 in bulk.
            rows = list(s.scalars(select(Event).where(Event.id.in_(dismissed_ids))))
            for r in rows:
                r.notified = 1
            log.info("Marked %d row(s) as notified (dismissed).", len(rows))


if __name__ == "__main__":
    main()
