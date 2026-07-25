"""
main.py
-------
Entry point for Project Odin Overwatch.

Pipeline (one run):
  0. Validate config; fail fast on bad env values.
  1. Retry any previously-saved-but-unnotified events (catch-up).
  2. Fetch fresh articles from GDELT for each configured keyword.
  3. Dedup in-batch, classify each article by event type + severity.
  4. For each new event:
       a. Save in its own transaction so it's durable BEFORE we notify.
       b. If severity meets the floor and we haven't hit the per-run cap,
          send a Discord alert.
       c. On successful send, mark the event notified in a second
          transaction. (Two transactions on purpose: an in-flight
          notification must not be rolled back by a later save failure.)

Run with:
    python -m app.main
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.classify.rules import find_matches
from app.classify.scoring import score_and_severity, severity_at_least
from app.config import config
from app.db.database import event_exists, get_session, init_db
from app.db.models import Event
from app.enrich.geo import enrich as enrich_geo
from app.notify import heartbeat
from app.notify.discord import send_event
from app.sources.faa_tfr import fetch_events as fetch_faa_tfr
from app.sources.gdelt import fetch_events
from app.sources.launch_library import fetch_events as fetch_launch_library

log = logging.getLogger("odin")

# URL query-param prefixes that are tracking junk, not content identity.
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "yclid")


def _normalize_url(url: str) -> str:
    """Light URL normalization so the same article doesn't get inserted twice
    just because of tracking params or a trailing slash.

    - lowercase scheme + host
    - drop fragment
    - strip common tracking query params
    - strip a single trailing slash (but not the root "/")
    """
    if not url:
        return ""
    try:
        raw = url.strip()
        if not raw:
            return ""
        initial_parts = urlsplit(raw)
        if initial_parts.scheme and initial_parts.scheme.lower() not in {"http", "https"}:
            return ""
        if raw.startswith("//"):
            raw = "https:" + raw
        elif not initial_parts.scheme:
            raw = "https://" + raw
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        if (
            scheme not in {"http", "https"}
            or not parts.hostname
            or any(char.isspace() for char in netloc)
        ):
            return ""
        path = parts.path or ""
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        # Filter query params.
        if parts.query:
            kept = []
            for pair in parts.query.split("&"):
                key = pair.split("=", 1)[0].lower()
                if any(key.startswith(p) for p in _TRACKING_PREFIXES):
                    continue
                kept.append(pair)
            query = "&".join(kept)
        else:
            query = ""
        return urlunsplit((scheme, netloc, path, query, ""))
    except (AttributeError, TypeError, ValueError):
        return ""


def _normalize_title(title: str) -> str:
    """Collapse whitespace so subtle formatting differences don't defeat dedup."""
    return re.sub(r"\s+", " ", title or "").strip()


def _parse_iso(dt: Optional[str]) -> Optional[datetime]:
    """Parse 'YYYY-MM-DDTHH:MM:SSZ' (GDELT's normalized form) into naive UTC."""
    if not dt:
        return None
    try:
        if dt.endswith("Z"):
            dt_inner = dt[:-1] + "+00:00"
        else:
            dt_inner = dt
        parsed = datetime.fromisoformat(dt_inner)
        # Standardize on naive UTC for SQLite-friendliness.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _deduplicate_in_memory(articles: List[Dict]) -> List[Dict]:
    """Drop duplicates within this batch before we hit the database.
    Also normalizes URL and title so downstream code sees the canonical form."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: List[Dict] = []
    for art in articles:
        url = _normalize_url(art.get("url") or "")
        title = _normalize_title(art.get("title") or "")
        if not url or not title:
            continue
        if url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        art["url"] = url
        art["title"] = title
        out.append(art)
    return out


def _classify(article: Dict) -> Dict:
    # If the source already pre-classified this article (LL2, FAA TFR,
    # etc.), trust it and just fill in any missing defaults. The
    # rule-based classifier targets free-form news text and would only
    # mangle structured input.
    if article.get("event_type"):
        article.setdefault("severity", "LOW")
        article.setdefault("score", 0)
        article.setdefault("matched_keywords", [])
        return article
    event_type, matched = find_matches(article)
    score, severity = score_and_severity(matched)
    return {
        **article,
        "event_type": event_type,
        "matched_keywords": matched,
        "score": score,
        "severity": severity,
    }


def _to_orm(article: Dict) -> Event:
    return Event(
        source=article.get("source") or "gdelt",
        domain=article.get("domain"),
        title=article.get("title") or "",
        url=article.get("url") or "",
        snippet=article.get("snippet"),
        published_at=_parse_iso(article.get("published_at")),
        event_type=article.get("event_type") or "UNKNOWN",
        severity=article.get("severity") or "LOW",
        score=int(article.get("score") or 0),
        matched_keywords=", ".join(article.get("matched_keywords") or []),
        location_name=article.get("location_name"),
        country=article.get("country"),
        latitude=article.get("latitude"),
        longitude=article.get("longitude"),
    )


def _save_new_event(article: Dict) -> Optional[int]:
    """Persist one event in its own transaction. Returns the new event id,
    or None if it was a duplicate / save failed. Never raises."""
    try:
        with get_session() as session:
            if event_exists(session, article["url"], article["title"]):
                return None
            ev = _to_orm(article)
            session.add(ev)
            session.flush()  # populate ev.id while still inside the session
            return ev.id
    except IntegrityError:
        # Race condition (e.g. two runs overlapping) or stale dedup miss.
        # Either way: treat as duplicate and skip.
        log.debug("IntegrityError on insert; treating as duplicate: %s", article.get("url"))
        return None
    except Exception as exc:
        log.warning("Failed to save event %s: %s", article.get("url"), exc)
        return None


def _mark_notified(event_id: int) -> None:
    """Set notified=1 in its own transaction. Logs (but doesn't raise)
    on failure — losing this update only causes a single re-notification
    on the next run, which is preferable to crashing the pipeline."""
    try:
        with get_session() as s:
            ev = s.get(Event, event_id)
            if ev is not None:
                ev.notified = 1
    except Exception as exc:
        log.warning("Could not mark event %s as notified: %s", event_id, exc)


def _update_classification(
    event_id: int,
    event_type: str,
    severity: str,
    score: int,
    matched: List[str],
) -> None:
    """Overwrite stored classification with current-rules output. Used by
    the retry pass when stored values are stale (classifier rules changed
    since the event was first saved)."""
    try:
        with get_session() as s:
            ev = s.get(Event, event_id)
            if ev is not None:
                ev.event_type = event_type
                ev.severity = severity
                ev.score = score
                ev.matched_keywords = ", ".join(matched)
    except Exception as exc:
        log.warning("Could not update classification for event %s: %s", event_id, exc)


def _build_notify_payload_from_event(ev: Event) -> Dict:
    return {
        "title": ev.title,
        "url": ev.url,
        "snippet": ev.snippet,
        "domain": ev.domain,
        "event_type": ev.event_type,
        "severity": ev.severity,
        "score": ev.score,
        "matched_keywords": ev.matched_keywords or "",
        "published_at": ev.published_at.isoformat() if ev.published_at else None,
    }


def _should_notify(event_type: str, severity: str) -> bool:
    """Single source of truth for the notification gate. Used by both
    the main run loop and the retry pass."""
    if not severity_at_least(severity, config.min_notify_severity):
        return False
    if config.notify_event_types and event_type not in config.notify_event_types:
        return False
    return True


def _reevaluate(ev: Event):
    """Return (event_type, severity, score, matched_keywords) under
    CURRENT rules. Source-aware: each source's events get their own
    classifier so structured sources (LL2, FAA TFR) don't get wrongly
    marked UNKNOWN by the GDELT keyword classifier."""
    src = ev.source or "gdelt"
    if src == "launch_library":
        from app.sources.launch_library import reevaluate as ll2_reevaluate
        sev, score = ll2_reevaluate(ev.title or "", ev.matched_keywords or "")
        kws = [k.strip() for k in (ev.matched_keywords or "").split(",") if k.strip()]
        return "ROCKET", sev, score, kws
    if src == "faa_tfr":
        from app.sources.faa_tfr import reevaluate as faa_reevaluate
        sev, score = faa_reevaluate(ev.title or "", ev.matched_keywords or "")
        kws = [k.strip() for k in (ev.matched_keywords or "").split(",") if k.strip()]
        return "AIRSPACE", sev, score, kws
    # gdelt or any other free-form-text source: rule-based classifier.
    article = {"title": ev.title, "snippet": ev.snippet}
    et, matched = find_matches(article)
    score, sev = score_and_severity(matched)
    return et, sev, score, matched


def _retry_unnotified(limit: int) -> int:
    """Process saved-but-unnotified events.

    Critical: we RE-CLASSIFY each pending event using current rules before
    deciding what to do. This auto-corrects stale rows that were saved
    under older (broken) classifier logic — they would otherwise replay
    forever via this pass.

    - If current rules say UNKNOWN → silently dismiss (mark notified=1).
    - If current rules say a type/severity that doesn't pass _should_notify
      → silently dismiss.
    - Otherwise → update the stored classification (so the dashboard
      reflects current logic) and try to send to Discord, respecting `limit`.

    Returns the number of events actually sent to Discord.
    """
    if limit <= 0:
        return 0

    with get_session() as session:
        # Over-fetch so silent dismissals don't starve legitimate sends.
        stmt = (
            select(Event)
            .where(Event.notified == 0)
            .order_by(Event.created_at.desc())
            .limit(max(limit * 5, 50))
        )
        pending = list(session.scalars(stmt))

    if not pending:
        return 0

    sent = 0
    dismissed = 0

    for ev in pending:
        # Source-aware re-evaluation. Running the GDELT keyword classifier
        # on a launch like "Falcon 9 | Starlink 10-47" or a TFR like
        # "FAA TFR 6/2755: VIP" would return UNKNOWN and wrongly dismiss
        # legitimate structured events.
        new_type, new_severity, new_score, new_matched = _reevaluate(ev)

        # If the classification drifted, update storage so the dashboard
        # and future runs see the correct values.
        classification_changed = (
            new_type != ev.event_type
            or new_severity != ev.severity
            or new_score != ev.score
        )
        if classification_changed:
            _update_classification(ev.id, new_type, new_severity, new_score, new_matched)
            # Keep local ev in sync for the rest of this iteration.
            ev.event_type = new_type
            ev.severity = new_severity
            ev.score = new_score
            ev.matched_keywords = ", ".join(new_matched)

        # Dismissal cases: no longer a defense event, or no longer meets
        # the notify gate. Mark notified=1 so we don't see it again.
        if new_type == "UNKNOWN" or not _should_notify(new_type, new_severity):
            _mark_notified(ev.id)
            dismissed += 1
            continue

        # Eligible but we're at cap or have no webhook → leave in queue.
        if sent >= limit:
            continue
        if not config.discord_webhook_url:
            continue

        ok = send_event(config.discord_webhook_url, _build_notify_payload_from_event(ev))
        if ok:
            _mark_notified(ev.id)
            sent += 1

    if dismissed:
        log.info(
            "Dismissed %d stale unnotified event(s) under current rules.",
            dismissed,
        )
    return sent


def run_once() -> None:
    """One pass of the monitor pipeline."""
    # 0. Configure + validate.
    config.configure_logging()
    config.validate()  # fail fast on bad env values
    init_db()

    if not config.discord_webhook_url:
        log.warning(
            "DISCORD_WEBHOOK_URL is not set; events will be saved but no "
            "alerts will be posted. Unsent events will be retried on future "
            "runs once the webhook is configured."
        )

    log.info(
        "Odin Overwatch starting | timespan=%s max=%s lang=%s min_sev=%s cap=%d types=%s",
        config.gdelt_timespan,
        config.gdelt_max_records,
        config.gdelt_language or "any",
        config.min_notify_severity,
        config.max_notifications_per_run,
        ",".join(config.notify_event_types) or "ALL",
    )

    # 1. Catch-up: retry unsent notifications from prior runs.
    caught_up = _retry_unnotified(limit=config.max_notifications_per_run)
    if caught_up:
        log.info("Re-sent %d previously unsent notification(s)", caught_up)

    # 2. Fetch from each enabled source. Per-source failures are isolated
    # so one bad source doesn't kill the run.
    raw: List[Dict] = []

    try:
        raw.extend(fetch_events(
            config.keywords,
            timespan=config.gdelt_timespan,
            max_records=config.gdelt_max_records,
            language=config.gdelt_language,
            mode=config.gdelt_query_mode,
        ))
    except Exception as exc:
        log.warning("GDELT fetch failed: %s", exc)

    if config.enable_launch_library:
        try:
            raw.extend(fetch_launch_library(window="upcoming", limit=50))
        except Exception as exc:
            log.warning("Launch Library 2 fetch failed: %s", exc)

    if config.enable_faa_tfr:
        try:
            raw.extend(fetch_faa_tfr())
        except Exception as exc:
            log.warning("FAA TFR fetch failed: %s", exc)

    log.info("Fetched %d raw item(s) across all sources", len(raw))

    # 3. In-batch dedup + classify.
    batch = _deduplicate_in_memory(raw)
    log.info("After in-batch dedup: %d unique article(s)", len(batch))

    classified = [enrich_geo(_classify(a)) for a in batch]

    type_counts: Dict[str, int] = {}
    sev_counts: Dict[str, int] = {}
    geo_count = 0
    for art in classified:
        type_counts[art["event_type"]] = type_counts.get(art["event_type"], 0) + 1
        sev_counts[art["severity"]] = sev_counts.get(art["severity"], 0) + 1
        if art.get("latitude") is not None:
            geo_count += 1
    log.info("Event types: %s", type_counts)
    log.info("Severity:    %s", sev_counts)
    log.info("Geo-tagged:  %d / %d", geo_count, len(classified))

    # 4. Persist + notify each event independently.
    new_count = 0
    notified_count = 0
    remaining_cap = max(0, config.max_notifications_per_run - caught_up)
    cap_warned = False

    for art in classified:
        if art["event_type"] == "UNKNOWN":
            continue

        event_id = _save_new_event(art)
        if event_id is None:
            continue  # duplicate or save error (already logged)

        new_count += 1
        log.info(
            "NEW [%s/%s] %s — %s",
            art["event_type"], art["severity"],
            art.get("domain") or "?",
            (art.get("title") or "")[:120],
        )

        # Notification gate: severity floor + (optional) event-type allow-list.
        if not _should_notify(art["event_type"], art["severity"]):
            continue

        # Alert-storm cap. Saved events that don't get notified will be
        # retried on the next run via _retry_unnotified.
        if notified_count >= remaining_cap:
            if not cap_warned:
                log.warning(
                    "Notification cap (%d) reached; remaining qualifying "
                    "events will be retried next run.",
                    config.max_notifications_per_run,
                )
                cap_warned = True
            continue

        ok = send_event(
            config.discord_webhook_url,
            {
                "title": art["title"],
                "url": art["url"],
                "snippet": art.get("snippet"),
                "domain": art.get("domain"),
                "event_type": art["event_type"],
                "severity": art["severity"],
                "score": art["score"],
                "matched_keywords": art.get("matched_keywords") or [],
                "published_at": art.get("published_at"),
            },
        )
        if ok:
            _mark_notified(event_id)
            notified_count += 1

    log.info(
        "Done. new_events=%d notified=%d retried=%d cap=%d min_severity=%s",
        new_count, notified_count, caught_up,
        config.max_notifications_per_run, config.min_notify_severity,
    )

    # 5. Heartbeat (no-op unless HEARTBEAT_INTERVAL_HOURS is set).
    heartbeat.check_and_send([
        f"new events: {new_count}",
        f"notified: {notified_count}",
        f"retried: {caught_up}",
        f"types seen: {dict(type_counts)}",
        f"severity: {dict(sev_counts)}",
    ])


if __name__ == "__main__":
    run_once()
