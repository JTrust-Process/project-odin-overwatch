"""
config.py
---------
Loads environment variables (from .env if present) and exposes a single
Config object the rest of the app can import.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from the project root (one level above this file's "app/" dir).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# -----------------------------------------------------------------------------
# Keywords used to query GDELT.
#
# In combined mode (the default), these are OR'd into a single query.
# Tradeoff: drop "intercepted" from the search set — as a bare verb it
# matches drug seizures, sports, and crime stories far more often than
# real interception events. Real intercept events still get covered by
# the "air defense", "missile launch", and "drone strike" queries.
# -----------------------------------------------------------------------------
DEFAULT_KEYWORDS: List[str] = [
    "drone strike",
    "drone attack",
    "UAV",
    "unmanned aerial vehicle",
    "loitering munition",
    "missile launch",
    "ballistic missile",
    "cruise missile",
    "hypersonic missile",
    "rocket attack",
    "air defense",
    "iron dome",
    "no-fly zone",
    "airspace restriction",
    "NOTAM",
]

VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_QUERY_MODES = {"combined", "per_keyword"}
VALID_EVENT_TYPES = {"DRONE", "MISSILE", "ROCKET", "AIR_DEFENSE", "AIRSPACE"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be a boolean (true/false, yes/no, on/off, or 1/0); "
        f"got {raw!r}"
    )


@dataclass
class Config:
    # Discord
    discord_webhook_url: str = field(
        default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    )

    # GDELT
    gdelt_timespan: str = field(
        default_factory=lambda: os.getenv("GDELT_TIMESPAN", "24h").strip() or "24h"
    )
    gdelt_max_records: int = field(
        default_factory=lambda: _get_int("GDELT_MAX_RECORDS", 200)
    )
    gdelt_language: str = field(
        default_factory=lambda: os.getenv("GDELT_LANGUAGE", "english").strip()
    )
    gdelt_query_mode: str = field(
        default_factory=lambda: (
            os.getenv("GDELT_QUERY_MODE", "combined").strip().lower() or "combined"
        )
    )

    # Additional sources (in addition to GDELT, which is always on).
    enable_launch_library: bool = field(
        default_factory=lambda: _get_bool("ENABLE_LAUNCH_LIBRARY", True)
    )
    enable_faa_tfr: bool = field(
        default_factory=lambda: _get_bool("ENABLE_FAA_TFR", True)
    )

    # Storage
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite:///data/odin.db"
        ).strip()
    )

    # Notifications
    min_notify_severity: str = field(
        default_factory=lambda: (
            os.getenv("MIN_NOTIFY_SEVERITY", "MEDIUM").strip().upper() or "MEDIUM"
        )
    )
    max_notifications_per_run: int = field(
        default_factory=lambda: _get_int("MAX_NOTIFICATIONS_PER_RUN", 20)
    )
    # Comma-separated allow-list of event types that should trigger Discord.
    # Empty = all types. Use this to mute noisier buckets without dropping
    # them from the DB / dashboard (e.g. set to DRONE,MISSILE,ROCKET,AIRSPACE
    # to skip AIR_DEFENSE alerts).
    notify_event_types: tuple = field(
        default_factory=lambda: tuple(
            t.strip().upper()
            for t in os.getenv("NOTIFY_EVENT_TYPES", "").split(",")
            if t.strip()
        )
    )

    # Logging
    log_level: str = field(
        default_factory=lambda: (
            os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        )
    )
    log_file: str = field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/odin.log").strip()
    )

    # Heartbeat (0 = disabled). Posts a one-liner status to Discord after
    # each run if at least N hours have passed since the last heartbeat.
    heartbeat_interval_hours: int = field(
        default_factory=lambda: _get_int("HEARTBEAT_INTERVAL_HOURS", 0)
    )

    # Keywords (not env-driven for v1; edit DEFAULT_KEYWORDS to extend)
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))

    def validate(self) -> None:
        """Fail fast on invalid config so misconfiguration never silently mutes
        the whole monitor. Call this once at startup, before any work."""
        if self.min_notify_severity not in VALID_SEVERITIES:
            raise ValueError(
                f"MIN_NOTIFY_SEVERITY={self.min_notify_severity!r} is invalid. "
                f"Expected one of: {sorted(VALID_SEVERITIES)}"
            )
        if self.log_level not in VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL={self.log_level!r} is invalid. "
                f"Expected one of: {sorted(VALID_LOG_LEVELS)}"
            )
        if self.gdelt_query_mode not in VALID_QUERY_MODES:
            raise ValueError(
                f"GDELT_QUERY_MODE={self.gdelt_query_mode!r} is invalid. "
                f"Expected one of: {sorted(VALID_QUERY_MODES)}"
            )
        if self.max_notifications_per_run < 0:
            raise ValueError("MAX_NOTIFICATIONS_PER_RUN must be >= 0")
        if self.gdelt_max_records <= 0 or self.gdelt_max_records > 250:
            raise ValueError(
                "GDELT_MAX_RECORDS must be between 1 and 250 (GDELT API cap)."
            )
        if self.notify_event_types:
            bad = set(self.notify_event_types) - VALID_EVENT_TYPES
            if bad:
                raise ValueError(
                    f"NOTIFY_EVENT_TYPES contains unknown types: {sorted(bad)}. "
                    f"Valid types: {sorted(VALID_EVENT_TYPES)}"
                )
        if self.heartbeat_interval_hours < 0:
            raise ValueError("HEARTBEAT_INTERVAL_HOURS must be >= 0")

    def configure_logging(self) -> None:
        """Configure root logger to write to console AND a rotating file.
        Idempotent — clears any pre-existing handlers so the file handler
        always exists (basicConfig is no-op on re-call)."""
        level = getattr(logging, self.log_level, logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        root = logging.getLogger()
        root.setLevel(level)
        # Reset handlers so a second call doesn't double-log.
        for h in list(root.handlers):
            root.removeHandler(h)

        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        root.addHandler(sh)

        if self.log_file:
            from logging.handlers import RotatingFileHandler
            path = Path(self.log_file)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = RotatingFileHandler(
                    path,
                    maxBytes=5 * 1024 * 1024,  # 5 MB
                    backupCount=5,
                    encoding="utf-8",
                )
                fh.setFormatter(formatter)
                root.addHandler(fh)
            except OSError as exc:
                # Don't crash the pipeline if the log dir isn't writable —
                # console logging still works.
                logging.getLogger(__name__).warning(
                    "Could not open log file %s: %s", path, exc
                )


# A single shared instance the rest of the app can import.
config = Config()
