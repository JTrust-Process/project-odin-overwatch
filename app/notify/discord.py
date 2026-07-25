"""
notify/discord.py
-----------------
Posts a single event to a Discord channel via incoming webhook.

Handles Discord's documented quirks:
- 429 rate-limit with Retry-After (respected, with a sane cap)
- 5xx transient errors (retried with backoff)
- All exceptions are swallowed so the pipeline can keep going
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Discord embed colors are integers (0xRRGGBB).
SEVERITY_COLORS = {
    "LOW": 0x95A5A6,     # gray
    "MEDIUM": 0xE67E22,  # orange
    "HIGH": 0xE74C3C,    # red
}

# Cap title/snippet so we never exceed Discord's embed limits.
TITLE_MAX = 240
DESC_MAX = 1800

# Hard ceiling on Retry-After so a misbehaving server can't stall the run.
_MAX_RETRY_AFTER_SECONDS = 10.0


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_payload(event: dict) -> dict:
    """Build a Discord webhook payload from an event dict.

    Expected keys: title, url, snippet, domain, event_type, severity,
    score, matched_keywords (list[str] or comma-string), published_at.
    """
    severity = (event.get("severity") or "LOW").upper()
    color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["LOW"])

    matched = event.get("matched_keywords") or []
    if isinstance(matched, str):
        matched = [m.strip() for m in matched.split(",") if m.strip()]

    fields = [
        {"name": "Type", "value": event.get("event_type") or "UNKNOWN", "inline": True},
        {"name": "Severity", "value": severity, "inline": True},
        {"name": "Score", "value": str(event.get("score", 0)), "inline": True},
    ]
    if event.get("domain"):
        fields.append({"name": "Source", "value": event["domain"], "inline": True})
    if event.get("published_at"):
        fields.append(
            {"name": "Published", "value": str(event["published_at"]), "inline": True}
        )
    if matched:
        fields.append(
            {
                "name": "Matched keywords",
                "value": ", ".join(matched[:10]),
                "inline": False,
            }
        )

    embed = {
        "title": _truncate(event.get("title"), TITLE_MAX) or "(no title)",
        "url": event.get("url"),
        "description": _truncate(event.get("snippet"), DESC_MAX),
        "color": color,
        "fields": fields,
        "footer": {"text": "Project Odin Overwatch • public-source monitor"},
    }
    return {"embeds": [embed]}


def _parse_retry_after(resp: requests.Response) -> float:
    """Extract a Retry-After value (seconds). Falls back to 1.0 if absent
    or unparseable. Capped at _MAX_RETRY_AFTER_SECONDS."""
    raw = resp.headers.get("Retry-After") or "1"
    try:
        wait = float(raw)
    except ValueError:
        wait = 1.0
    # Discord may also return a JSON body with retry_after in seconds (float).
    try:
        body = resp.json()
        if isinstance(body, dict) and "retry_after" in body:
            wait = max(wait, float(body["retry_after"]))
    except (ValueError, TypeError):
        pass
    return min(max(wait, 0.0), _MAX_RETRY_AFTER_SECONDS)


def send_text(webhook_url: str, text: str, timeout: float = 10.0) -> bool:
    """Send a plain-text Discord message (no embed). Used for heartbeats
    and any other status pings that don't need the structured embed."""
    if not webhook_url or not text:
        return False
    payload = {"content": text[:2000]}  # Discord max
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("Discord text-send failed: %s", exc)
        return False
    if 200 <= resp.status_code < 300:
        return True
    log.warning("Discord text-send HTTP %s: %s", resp.status_code, resp.text[:200])
    return False


def send_event(
    webhook_url: str,
    event: dict,
    timeout: float = 10.0,
    max_retries: int = 2,
) -> bool:
    """Send a single event to Discord. Returns True on success, False otherwise.
    Never raises — failures are logged so the main loop can keep going."""
    if not webhook_url:
        log.debug("No Discord webhook configured; skipping notify.")
        return False

    payload = build_payload(event)

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            log.warning(
                "Discord request failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return False

        # Success
        if 200 <= resp.status_code < 300:
            return True

        # Rate-limited: honor Retry-After, but cap the wait.
        if resp.status_code == 429:
            wait = _parse_retry_after(resp)
            log.info(
                "Discord rate-limited; sleeping %.2fs before retry (attempt %d/%d)",
                wait, attempt + 1, max_retries + 1,
            )
            time.sleep(wait)
            continue

        # Server-side hiccup: retry with backoff.
        if 500 <= resp.status_code < 600 and attempt < max_retries:
            time.sleep(2 ** attempt)
            continue

        # Terminal (4xx other than 429, or 5xx after retries): give up.
        log.warning(
            "Discord webhook returned HTTP %s: %s",
            resp.status_code, resp.text[:200],
        )
        return False

    return False
