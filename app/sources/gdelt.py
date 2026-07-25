"""
sources/gdelt.py
----------------
Pulls public news articles from the GDELT 2.0 DOC API.

Two query modes:
  - "combined" (default): one OR-query covering all keywords. One HTTP
    call per run, which avoids GDELT's aggressive burst throttling.
  - "per_keyword": legacy mode that issues one query per keyword. Slower
    and prone to 429s, but useful if you want per-keyword counts.

Retries on 429 / 5xx / network errors with exponential backoff.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

USER_AGENT = "ProjectOdinOverwatch/0.1 (+public-source monitor; educational use)"

# Status codes that warrant a retry. Everything else is terminal.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Backoff schedule (seconds) for retries. Aggressive on purpose — GDELT
# throttles per-IP and a quick retry just bounces off the limiter.
_BACKOFF_SCHEDULE = (5, 15, 30)

log = logging.getLogger(__name__)


class _Retryable(Exception):
    """Internal signal that the caller should back off and try again."""


def _quote(term: str) -> str:
    """Wrap multi-word terms in quotes for GDELT's query syntax."""
    return f'"{term}"' if " " in term else term


def _build_combined_query(keywords: Iterable[str], language: str) -> str:
    """One OR-query covering every keyword. Returns e.g.:
        ("drone strike" OR UAV OR "missile launch") sourcelang:english
    """
    or_clause = " OR ".join(_quote(k) for k in keywords)
    query = f"({or_clause})"
    if language:
        query += f" sourcelang:{language}"
    return query


def _build_single_query(keyword: str, language: str) -> str:
    parts = [_quote(keyword)]
    if language:
        parts.append(f"sourcelang:{language}")
    return " ".join(parts)


def _parse_gdelt_date(raw: Optional[str]) -> Optional[str]:
    """GDELT returns 'YYYYMMDDTHHMMSSZ'. Normalize to ISO-8601, or None."""
    if not raw or len(raw) < 15:
        return None
    try:
        return (
            f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T"
            f"{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"
        )
    except Exception:
        log.debug("Could not parse GDELT date %r", raw)
        return None


def _domain_from_url(url: str) -> Optional[str]:
    try:
        netloc = urlparse(url).netloc
        return netloc.lower() or None
    except Exception:
        return None


def _parse_articles(payload: Dict, matched_keyword: Optional[str]) -> List[Dict]:
    articles = payload.get("articles") or []
    results: List[Dict] = []
    for art in articles:
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        results.append(
            {
                "source": "gdelt",
                "title": title,
                "url": url,
                "domain": (
                    (art.get("domain") or _domain_from_url(url) or "").lower()
                    or None
                ),
                "published_at": _parse_gdelt_date(art.get("seendate")),
                "snippet": art.get("excerpt") or art.get("title"),
                # Kept for log/debug only — NOT used by the classifier.
                "matched_keyword": matched_keyword,
            }
        )
    return results


def _query_doc_api(
    query: str,
    *,
    timespan: str,
    max_records: int,
    timeout: float = 25.0,
) -> Dict:
    """Low-level GDELT call. Returns the parsed JSON dict (possibly
    empty). Raises _Retryable on transient errors."""
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "JSON",
        "maxrecords": str(max_records),
        "timespan": timespan,
        "sort": "DateDesc",
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(
            GDELT_DOC_URL, params=params, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        raise _Retryable(f"network error: {exc}") from exc

    if resp.status_code in _RETRYABLE_STATUS:
        raise _Retryable(f"HTTP {resp.status_code}")

    if resp.status_code != 200:
        # Terminal client error (auth, malformed query, etc.). Log and
        # return empty so the pipeline can continue.
        log.warning(
            "GDELT returned HTTP %s: %s",
            resp.status_code, resp.text[:200],
        )
        return {}

    text = resp.text.strip()
    if not text or not text.startswith("{"):
        log.debug("GDELT returned non-JSON body (len=%d)", len(text))
        return {}

    try:
        return resp.json()
    except ValueError:
        log.warning("GDELT returned invalid JSON")
        return {}


def _query_with_retry(
    query: str,
    *,
    timespan: str,
    max_records: int,
    label: str = "",
) -> Dict:
    """Run _query_doc_api with retry-on-transient-error."""
    last_exc: Optional[_Retryable] = None
    for attempt, wait_on_next in enumerate(_BACKOFF_SCHEDULE + (None,)):
        try:
            return _query_doc_api(
                query, timespan=timespan, max_records=max_records
            )
        except _Retryable as exc:
            last_exc = exc
            if wait_on_next is None:
                break
            log.info(
                "GDELT transient error%s (attempt %d/%d): %s; retrying in %ds",
                f" for {label!r}" if label else "",
                attempt + 1, len(_BACKOFF_SCHEDULE) + 1, exc, wait_on_next,
            )
            time.sleep(wait_on_next)
    log.warning(
        "GDELT failed%s after %d attempt(s): %s",
        f" for {label!r}" if label else "",
        len(_BACKOFF_SCHEDULE) + 1, last_exc,
    )
    return {}


def fetch_events(
    keywords: Iterable[str],
    *,
    timespan: str = "24h",
    max_records: int = 75,
    language: str = "english",
    mode: str = "combined",
    sleep_between: float = 1.5,
) -> List[Dict]:
    """Fetch articles from GDELT.

    Args:
        keywords: terms to search for.
        timespan: GDELT timespan string ("15min", "1h", "24h", "7d", ...).
        max_records: in "combined" mode, total cap (GDELT max is 250).
            In "per_keyword" mode, cap per keyword.
        language: GDELT sourcelang filter, or "" for any.
        mode: "combined" (one OR-query, default) or "per_keyword".
        sleep_between: polite delay between per-keyword calls. Ignored
            in combined mode.
    """
    keyword_list = [k for k in keywords if k]
    if not keyword_list:
        return []

    if mode == "combined":
        # GDELT caps maxrecords at 250 regardless of what we send.
        combined_max = min(max(max_records, 1), 250)
        query = _build_combined_query(keyword_list, language)
        log.info(
            "Querying GDELT (combined, %d terms, max=%d)",
            len(keyword_list), combined_max,
        )
        payload = _query_with_retry(
            query,
            timespan=timespan,
            max_records=combined_max,
            label="combined",
        )
        results = _parse_articles(payload, matched_keyword=None)
        log.info("  -> %d article(s) (combined)", len(results))
        return results

    if mode != "per_keyword":
        raise ValueError(
            f"Unknown GDELT query mode {mode!r}. "
            "Use 'combined' or 'per_keyword'."
        )

    all_results: List[Dict] = []
    for kw in keyword_list:
        log.info("Querying GDELT for keyword: %r", kw)
        query = _build_single_query(kw, language)
        payload = _query_with_retry(
            query,
            timespan=timespan,
            max_records=max_records,
            label=kw,
        )
        batch = _parse_articles(payload, matched_keyword=kw)
        log.info("  -> %d article(s) for %r", len(batch), kw)
        all_results.extend(batch)
        if sleep_between:
            time.sleep(sleep_between)
    return all_results
