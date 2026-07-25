"""
sources/faa_tfr.py
------------------
FAA Temporary Flight Restrictions source.

WARNING — FRAGILE:
The FAA's TFR data is published via https://tfr.faa.gov/, which does not
have a stable, documented public API. This module targets the JSON list
endpoint at /tfrapi/exportTfrList. If the FAA changes the endpoint or
response schema, this module will start returning [] and log a warning;
it won't crash anything else.

To debug: open https://tfr.faa.gov/tfr2/list.html in a browser and
compare the structure to what _parse_record expects below.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

FAA_LIST_URL = "https://tfr.faa.gov/tfrapi/exportTfrList"
USER_AGENT = "ProjectOdinOverwatch/0.2 (+public-source monitor; educational use)"

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_BACKOFF_SCHEDULE = (5, 15, 30)

log = logging.getLogger(__name__)


class _Retryable(Exception):
    pass


def _request(url: str, timeout: float = 20.0) -> Optional[Any]:
    last_exc: Optional[Exception] = None
    for attempt, wait_on_next in enumerate(_BACKOFF_SCHEDULE + (None,)):
        try:
            resp = requests.get(
                url, timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except requests.RequestException as exc:
            last_exc = _Retryable(f"network: {exc}")
        else:
            if resp.status_code in _RETRYABLE_STATUS:
                last_exc = _Retryable(f"HTTP {resp.status_code}")
            elif resp.status_code != 200:
                log.warning("FAA TFR HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            else:
                try:
                    return resp.json()
                except ValueError:
                    log.warning(
                        "FAA TFR endpoint returned non-JSON (probably an HTML "
                        "list — the JSON endpoint may have moved). Inspect: %s",
                        url,
                    )
                    return None
        if wait_on_next is None:
            break
        log.info(
            "FAA TFR transient error (attempt %d/%d): %s; retrying in %ds",
            attempt + 1, len(_BACKOFF_SCHEDULE) + 1, last_exc, wait_on_next,
        )
        time.sleep(wait_on_next)
    log.warning("FAA TFR failed after retries: %s", last_exc)
    return None


def _first(d: Dict, *keys, default=None):
    """Return the first non-empty value among the given keys."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f != 0 else None  # FAA sometimes returns 0 as "missing"
    except (TypeError, ValueError):
        return None


def _normalize_published_at(value) -> Optional[str]:
    """Return the FAA date in the ISO form consumed by main._parse_iso."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%m/%d/%Y")
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%dT00:00:00Z")


# US state centroids (approximate, ~1° precision) so we can geo-pin any
# TFR with a "..., XX" state suffix even if the bulk endpoint doesn't
# return lat/lon.
_US_STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.0, -150.0), "AZ": (34.0, -111.5),
    "AR": (34.7, -92.4), "CA": (36.7, -119.5), "CO": (39.0, -105.5),
    "CT": (41.6, -72.7), "DE": (38.9, -75.5), "FL": (27.9, -81.7),
    "GA": (33.0, -83.6), "HI": (20.7, -157.4), "ID": (44.1, -114.7),
    "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.0, -93.2),
    "KS": (38.5, -98.4), "KY": (37.7, -84.6), "LA": (31.1, -91.8),
    "ME": (44.7, -69.8), "MD": (39.0, -76.7), "MA": (42.2, -71.5),
    "MI": (43.3, -84.5), "MN": (46.3, -94.3), "MS": (32.7, -89.7),
    "MO": (38.4, -92.3), "MT": (47.1, -109.6), "NE": (41.5, -99.8),
    "NV": (38.3, -117.0), "NH": (43.6, -71.6), "NJ": (40.2, -74.5),
    "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.6, -79.8),
    "ND": (47.5, -100.0), "OH": (40.3, -82.8), "OK": (35.5, -96.9),
    "OR": (43.9, -120.5), "PA": (40.6, -77.2), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.7, -86.7),
    "TX": (31.1, -97.6), "UT": (39.5, -111.5), "VT": (44.0, -72.7),
    "VA": (37.5, -78.7), "WA": (47.4, -120.4), "WV": (38.5, -80.6),
    "WI": (44.3, -89.6), "WY": (43.0, -107.5), "DC": (38.91, -77.04),
    # Territories
    "PR": (18.2, -66.5), "GU": (13.44, 144.79), "VI": (18.34, -64.93),
}

# More precise coords for named locations that appear in real TFR data.
# Substring match on the uppercased location string — first hit wins.
_US_NAMED_PLACES = {
    "CAPE CANAVERAL": (28.49, -80.58),
    "KENNEDY SPACE CENTER": (28.57, -80.65),
    "VANDENBERG": (34.74, -120.57),
    "BOCA CHICA": (25.99, -97.16),
    "PENTAGON": (38.87, -77.06),
    "WASHINGTON, DC": (38.91, -77.04),
    "WASHINGTON DC": (38.91, -77.04),
    "WASHINGTON": (38.91, -77.04),
    "COLORADO SPRINGS": (38.83, -104.82),
    "NEW ORLEANS": (29.95, -90.07),
    "NEW YORK": (40.71, -74.01),
    "LOS ANGELES": (34.05, -118.24),
    "PALM BEACH": (26.72, -80.04),
    "SAN ANGELO": (31.46, -100.44),
    "GRAND FORKS": (47.92, -97.03),
    "ANDERSEN AFB": (13.58, 144.93),
    "BEALE AFB": (39.13, -121.44),
    "KANSAS CITY": (39.10, -94.58),
    "ATLANTIC CITY": (39.36, -74.42),
    "HARRISBURG, PA": (40.27, -76.88),
    "MORRISTOWN": (40.80, -74.48),
    "THURMONT": (39.62, -77.41),  # Camp David area
    "BOULDER": (40.01, -105.27),
    "DALLAS": (32.78, -96.80),
    "BROWNSVILLE": (25.90, -97.50),
    "MACON": (32.84, -83.63),
    "BRUNSWICK": (31.15, -81.49),
    "MAYPORT": (30.39, -81.42),
    "JACKSON, WY": (43.48, -110.76),
    "WINNEMUCCA": (40.97, -117.74),
    "SANTA BARBARA": (34.42, -119.70),
    "GARDEN GROVE": (33.77, -117.94),
    "SACRAMENTO": (38.58, -121.49),
    "OKLAHOMA CITY": (35.47, -97.52),
    "CHARLOTTESVILLE": (38.03, -78.48),
    "DISNEYLAND": (33.81, -117.92),
    "DISNEY WORLD": (28.39, -81.57),
    "ANAHEIM": (33.84, -117.91),
    "ORLANDO": (28.54, -81.38),
    "HOUSTON": (29.76, -95.37),
    "RUIDOSO": (33.33, -105.67),
    "VIEQUES": (18.13, -65.43),
    "CEIBA": (18.26, -65.65),
    "NOGALES": (31.34, -110.94),
    "CHULA VISTA": (32.64, -117.08),
    "CALEXICO": (32.68, -115.49),
    "CONCORD, CA": (37.98, -122.03),
    "MOYOCK": (36.52, -76.18),
    "OCHOPEE": (25.90, -81.30),
    "CLEAR, AK": (64.30, -149.18),
    "MCGREGOR, TX": (31.44, -97.42),
    "NORTHWEST FIELD": (13.61, 144.85),
    "S. FINEGAYAN": (13.55, 144.86),
}


def _resolve_us_location(loc_name: Optional[str]) -> tuple:
    """Best-effort US geo. Returns (lat, lon) — both may be None.
    Tries named-place substring match first, then falls back to the
    trailing two-letter state code's centroid."""
    if not loc_name:
        return None, None
    upper = loc_name.upper()
    for key, (lat, lon) in _US_NAMED_PLACES.items():
        if key in upper:
            return lat, lon
    # Trailing state code, e.g. "BROWNSVILLE, TX" -> "TX"
    parts = [p.strip() for p in loc_name.split(",")]
    if len(parts) >= 2:
        tail = parts[-1].upper().split()[0] if parts[-1] else ""
        if len(tail) == 2 and tail.isalpha() and tail in _US_STATE_CENTROIDS:
            return _US_STATE_CENTROIDS[tail]
    return None, None


# Most TFR types are routine (motorcades, airshows, drone gatherings,
# persistent border zones) — show on the dashboard, but don't ping Discord.
# Only SPACE OPERATIONS gets MEDIUM because it correlates with launches.
def _tfr_significance(tfr_type: Optional[str]) -> tuple:
    t = (tfr_type or "").upper()
    if "SPACE OPERATIONS" in t:
        return "MEDIUM", 4
    return "LOW", 1


def reevaluate(title: str, matched_keywords_str: str) -> tuple:
    """Re-evaluate stored severity for a FAA TFR event under current rules.
    matched_keywords were stored as "FAA TFR, <tfr_type>, NOTAM" — we
    pull tfr_type from the middle element."""
    parts = [p.strip() for p in (matched_keywords_str or "").split(",")]
    tfr_type = parts[1] if len(parts) >= 2 else ""
    return _tfr_significance(tfr_type)


def _parse_record(r: Dict) -> Optional[Dict]:
    """Convert one FAA TFR record into our article-shaped dict.
    Returns None for unparseable records — never raises."""
    if not isinstance(r, dict):
        return None
    notam = (
        _first(r, "notam_id", "notamId", "notam", "noticeId", default="") or ""
    ).strip()
    if not notam:
        return None

    tfr_type = _first(r, "tfr_type", "type", "noticeType", default="") or ""
    description = _first(r, "description", "name", "minAltitudeText", default="") or ""
    state = str(_first(r, "state", "stateCode", default="") or "").strip().upper()
    location = _first(r, "location", "locationName", "place", default="") or ""

    # The current FAA list schema provides no dedicated location field.
    # Its description starts with "<place>, <state>, ...", so retain that
    # prefix for display and geo fallback instead of dropping the location.
    if not location and state and isinstance(description, str):
        marker = f", {state}"
        marker_pos = description.upper().find(marker)
        if marker_pos >= 0:
            location = description[: marker_pos + len(marker)].strip()

    bits = [t for t in [tfr_type, description or location] if t]
    title = f"FAA TFR {notam}" + (": " + " — ".join(bits) if bits else "")

    # Detail page URL pattern is documented; use safely.
    notam_slug = notam.replace("/", "_").replace(" ", "")
    url = f"https://tfr.faa.gov/save_pages/detail_{notam_slug}.html"

    # Try API-provided coords first; fall back to US gazetteer.
    lat = _coerce_float(_first(r, "lat", "latitude", "centerLat", "lat1"))
    lon = _coerce_float(_first(r, "lon", "lng", "longitude", "centerLon", "lon1"))
    if lat is None or lon is None:
        lat, lon = _resolve_us_location(location)

    severity, score = _tfr_significance(tfr_type)

    return {
        "source": "faa_tfr",
        "title": title[:500],
        "url": url,
        "domain": "tfr.faa.gov",
        "snippet": (description or location or "")[:500],
        "published_at": _normalize_published_at(
            _first(
                r,
                "valid_from",
                "validFrom",
                "issued",
                "effectiveDate",
                "creation_date",
                "creationDate",
            )
        ),
        # Pre-classified — TFRs are unambiguously airspace events.
        "event_type": "AIRSPACE",
        "severity": severity,
        "score": score,
        "matched_keywords": [k for k in ["FAA TFR", tfr_type, "NOTAM"] if k],
        "location_name": location or None,
        "country": "USA",
        "latitude": lat,
        "longitude": lon,
        "matched_keyword": None,
    }


def fetch_events() -> List[Dict]:
    """Fetch the current US TFR list from the FAA. Returns [] on any
    failure (network, schema change, parse error)."""
    log.info("Querying FAA TFR list")
    data = _request(FAA_LIST_URL)
    if data is None:
        log.info("  -> 0 TFR(s) (endpoint unreachable or returned no data)")
        return []

    # The endpoint historically returns either a bare list or {"data":[...]}.
    if isinstance(data, dict):
        records = data.get("data") or data.get("results") or data.get("tfrList") or []
    elif isinstance(data, list):
        records = data
    else:
        log.warning("FAA TFR response was %s; expected list or dict.", type(data).__name__)
        return []

    events: List[Dict] = []
    for r in records:
        parsed = _parse_record(r)
        if parsed is not None:
            events.append(parsed)

    log.info("  -> %d TFR(s) from FAA", len(events))
    return events
