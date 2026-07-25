"""
notify/heartbeat.py
-------------------
Periodic "I'm still alive" Discord post.

The monitor itself can die quietly (cron task disabled, machine offline,
API key revoked, etc.). The heartbeat is a meta-monitor — if you stop
seeing it, something's broken.

Cadence is controlled by HEARTBEAT_INTERVAL_HOURS in .env. Set to 0
(default) to disable. Set to 24 for a daily heartbeat.

State is stored in data/.heartbeat_last as an ISO timestamp so the
cadence survives process restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.config import PROJECT_ROOT, config
from app.notify.discord import send_text

log = logging.getLogger(__name__)

_MARKER = PROJECT_ROOT / "data" / ".heartbeat_last"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_last() -> datetime | None:
    if not _MARKER.exists():
        return None
    try:
        return datetime.fromisoformat(_MARKER.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_now() -> None:
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(_utcnow_naive().isoformat())
    except OSError as exc:
        log.warning("Could not write heartbeat marker: %s", exc)


def check_and_send(summary_lines: Iterable[str]) -> bool:
    """If HEARTBEAT_INTERVAL_HOURS has elapsed since the last heartbeat,
    post a status summary to Discord. Returns True if a heartbeat was sent."""
    if config.heartbeat_interval_hours <= 0:
        return False
    if not config.discord_webhook_url:
        return False

    last = _read_last()
    now = _utcnow_naive()
    if last is not None:
        elapsed_hours = (now - last).total_seconds() / 3600.0
        if elapsed_hours < config.heartbeat_interval_hours:
            return False

    body = "**Odin Overwatch heartbeat**\n" + "\n".join(f"• {line}" for line in summary_lines)
    body += f"\n_last: {last.isoformat() if last else 'never'} • now: {now.isoformat()}_"
    ok = send_text(config.discord_webhook_url, body)
    if ok:
        _write_now()
        log.info("Heartbeat posted to Discord.")
    else:
        log.warning("Heartbeat send failed; will retry next run.")
    return ok
