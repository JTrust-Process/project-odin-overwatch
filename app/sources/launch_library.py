"""
sources/launch_library.py
-------------------------
Launch Library 2 (thespacedevs.com) source — real rocket launch data.

Why this is useful: LL2 returns *structured* launch info (name, pad,
launch provider, lat/lon, NET timestamp) rather than free-form news text.
That means we can skip the keyword classifier entirely and bypass the
geo gazetteer — every event we produce here is already typed
(`ROCKET`), severity-rated, and geo-located.

Free public API, no key. Rate-limited at ~15 req/hour anonymous, which
is plenty for our once-per-run cadence.

Docs: https://ll.thespacedevs.com/docs/
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

LL2_BASE = "https://ll.thespacedevs.com/2.2.0/launch/"
USER_AGENT = "ProjectOdinOverwatch/0.2 (+public-source monitor; educational use)"

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_BACKOFF_SCHEDULE = (5, 15, 30)

log = logging.getLogger(__name__)


class _Retryable(Exception):
    pass


def _request(url: str, params: Optional[Dict] = None, timeout: float = 20.0) -> Optional[Dict]:
    """GET with exponential backoff on transient errors."""
    last_exc: Optional[Exception] = None
    for attempt, wait_on_next in enumerate(_BACKOFF_SCHEDULE + (None,)):
        try:
            resp = requests.get(
                url, params=params or {}, timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            last_exc = _Retryable(f"network: {exc}")
        else:
            if resp.status_code in _RETRYABLE_STATUS:
                last_exc = _Retryable(f"HTTP {resp.status_code}")
            elif resp.status_code != 200:
                log.warning("LL2 HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            else:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("LL2 returned non-JSON body.")
                    return None
        if wait_on_next is None:
            break
        log.info(
            "LL2 transient error (attempt %d/%d): %s; retrying in %ds",
            attempt + 1, len(_BACKOFF_SCHEDULE) + 1, last_exc, wait_on_next,
        )
        time.sleep(wait_on_next)
    log.warning("LL2 failed after retries: %s", last_exc)
    return None


def _country_from_location(name: Optional[str]) -> Optional[str]:
    """Best-effort country extraction from 'Cape Canaveral, FL, USA'."""
    if not name:
        return None
    parts = [p.strip() for p in name.split(",")]
    return parts[-1] if parts else None


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Substrings (lower-cased match) that escalate a launch from LOW to MEDIUM.
# Routine commercial constellation launches stay LOW so they don't dominate
# Discord. Anything defense / crewed / new-vehicle / deep-space gets MEDIUM.
_HIGH_SIGNAL_SUBSTRINGS = (
    # Government / defense payloads
    "sda tranche", "sda ", "nro ", "ussf-", "ussf ", "usa-",
    "icbm", "minuteman", "trident", "classified",
    # Crewed missions
    "crew-", " crewed", "shenzhou", "soyuz ms", "dragon crs",
    "starliner", "boe-cft", "axiom",
    # Major science / exploration
    "starship", "chang'e", "tianzhou", "tianwen", "artemis",
    "europa", "psyche", "james webb", "lunar", " mars ", "mars rover",
    # New vehicle tests
    "maiden flight", "demo flight", "test flight", "first launch",
    "first flight", "inaugural",
)


def _launch_significance(title: str, extras: str = "") -> tuple[str, int]:
    """Heuristic: only escalate launches that are defense-relevant or
    historically significant. Returns (severity, score).

    Routine Starlink / Amazon Leo / Globalstar batches stay LOW so they
    show up on the dashboard but don't ping Discord.
    """
    lower = f"{title or ''} {extras or ''}".lower()
    if any(s in lower for s in _HIGH_SIGNAL_SUBSTRINGS):
        return "MEDIUM", 5
    return "LOW", 1


def reevaluate(title: str, matched_keywords_str: str) -> tuple[str, int]:
    """Re-evaluate stored severity for an LL2 event under current rules.
    Used by main._retry_unnotified to clean up the queue when the
    severity heuristic changes."""
    return _launch_significance(title or "", matched_keywords_str or "")


def fetch_events(*, window: str = "upcoming", limit: int = 50) -> List[Dict]:
    """Fetch launches from Launch Library 2.

    Args:
        window: "upcoming" (default) or "previous".
        limit:  max launches to return; LL2 caps at 100 per call.

    Returns a list of article-shaped dicts that bypass classification +
    geo enrichment downstream (since both are already populated here).
    """
    if window not in ("upcoming", "previous"):
        raise ValueError(f"Unknown LL2 window: {window!r}")

    url = f"{LL2_BASE}{window}/"
    log.info("Querying Launch Library 2 (%s, limit=%d)", window, limit)
    data = _request(url, {"limit": str(min(max(limit, 1), 100))})
    if not data:
        return []

    results = data.get("results") or []
    log.info("  -> %d launch(es) from LL2", len(results))

    events: List[Dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        self_url = (r.get("url") or "").strip()
        if not name or not self_url:
            continue

        pad = r.get("pad") or {}
        if not isinstance(pad, dict):
            pad = {}
        loc = pad.get("location") or {}
        if not isinstance(loc, dict):
            loc = {}
        loc_name = loc.get("name")
        lat = _coerce_float(pad.get("latitude"))
        lon = _coerce_float(pad.get("longitude"))
        country = (
            loc.get("country_code")
            or pad.get("country_code")
            or _country_from_location(loc_name)
        )

        provider = (r.get("launch_service_provider") or {}).get("name", "") or ""
        rocket = ((r.get("rocket") or {}).get("configuration") or {}).get("name", "") or ""
        mission = r.get("mission") or {}
        status = (r.get("status") or {}).get("abbrev", "") or ""
        snippet = (
            mission.get("description")
            or f"{provider} {rocket} launch from {loc_name or 'unknown pad'}"
        )

        matched = [t for t in [provider, rocket, status] if t]
        severity, score = _launch_significance(name, " ".join(matched))

        events.append({
            "source": "launch_library",
            "title": name,
            "url": self_url,
            "domain": "ll.thespacedevs.com",
            "snippet": (snippet or "")[:500],
            "published_at": r.get("net"),  # ISO 8601 string
            # Pre-classified — downstream _classify will pass these through.
            "event_type": "ROCKET",
            "severity": severity,
            "score": score,
            "matched_keywords": matched,
            # Pre-geo-tagged — downstream enrich_geo will see latitude
            # already set and skip overwriting.
            "location_name": loc_name,
            "country": country,
            "latitude": lat,
            "longitude": lon,
            "matched_keyword": None,
        })
    return events
