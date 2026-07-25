from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main as main_module
from app.api import server as api_server
from app.config import _get_bool, _get_int
from app.db import database
from app.db.models import Base, Event
from app.main import _normalize_url, _parse_iso
from app.sources.faa_tfr import _parse_record
from app.sources.gdelt import fetch_events as fetch_gdelt_events
from app.sources.launch_library import fetch_events as fetch_launch_events
from scripts.purge_stale import _reevaluate as purge_reevaluate


class TimestampParsingTests(unittest.TestCase):
    def test_aware_timestamp_is_normalized_to_naive_utc(self) -> None:
        expected = datetime(2026, 5, 25, 12, 0, 0)

        self.assertEqual(_parse_iso("2026-05-25T12:00:00Z"), expected)
        self.assertEqual(_parse_iso("2026-05-25T14:00:00+02:00"), expected)


class UrlNormalizationTests(unittest.TestCase):
    def test_only_well_formed_http_urls_are_accepted(self) -> None:
        self.assertEqual(_normalize_url("javascript:alert(1)"), "")
        self.assertEqual(_normalize_url("not a url"), "")
        self.assertEqual(_normalize_url("http://[invalid"), "")
        self.assertEqual(
            _normalize_url("example.com/path?utm_source=x&id=1"),
            "https://example.com/path?id=1",
        )


class ConfigParsingTests(unittest.TestCase):
    def test_invalid_integer_does_not_silently_use_default(self) -> None:
        with patch.dict(
            os.environ,
            {"MAX_NOTIFICATIONS_PER_RUN": "twenty"},
        ):
            with self.assertRaisesRegex(ValueError, "MAX_NOTIFICATIONS_PER_RUN"):
                _get_int("MAX_NOTIFICATIONS_PER_RUN", 20)

    def test_invalid_boolean_does_not_silently_disable_source(self) -> None:
        with patch.dict(os.environ, {"ENABLE_FAA_TFR": "treu"}):
            with self.assertRaisesRegex(ValueError, "ENABLE_FAA_TFR"):
                _get_bool("ENABLE_FAA_TFR", True)

        with patch.dict(os.environ, {"ENABLE_FAA_TFR": "false"}):
            self.assertFalse(_get_bool("ENABLE_FAA_TFR", True))


class StructuredSourceParsingTests(unittest.TestCase):
    @patch("app.sources.gdelt._query_with_retry", return_value={})
    def test_gdelt_combined_query_honors_configured_limit(self, query) -> None:
        fetch_gdelt_events(["missile"], max_records=20)

        self.assertEqual(query.call_args.kwargs["max_records"], 20)

    @patch("app.sources.launch_library._request")
    def test_launch_library_reads_coordinates_from_pad(self, request) -> None:
        request.return_value = {
            "results": [
                {
                    "name": "Test Launch",
                    "url": "https://example.test/launch",
                    "pad": {
                        "latitude": "28.458",
                        "longitude": "-80.528",
                        "location": {
                            "name": "Cape Canaveral SFS, FL, USA",
                            "country_code": "USA",
                        },
                    },
                    "launch_service_provider": {},
                    "rocket": {},
                    "mission": {},
                    "status": {},
                    "net": "2026-07-25T12:00:00Z",
                }
            ]
        }

        event = fetch_launch_events(limit=1)[0]

        self.assertEqual(event["latitude"], 28.458)
        self.assertEqual(event["longitude"], -80.528)
        self.assertEqual(event["country"], "USA")

    def test_faa_record_derives_location_from_description_and_state(self) -> None:
        event = _parse_record(
            {
                "notam_id": "6/7491",
                "type": "SPACE OPERATIONS",
                "facility": "ZHU",
                "state": "TX",
                "description": "Boca Chica, TX, Saturday, July 25, 2026 Local",
                "creation_date": "07/21/2026",
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["location_name"], "Boca Chica, TX")
        self.assertEqual(event["latitude"], 25.99)
        self.assertEqual(event["longitude"], -97.16)
        self.assertEqual(event["published_at"], "2026-07-21T00:00:00Z")


class ReclassificationTests(unittest.TestCase):
    def test_reevaluate_returns_current_matched_keywords(self) -> None:
        event = Event(
            source="gdelt",
            title="Ballistic missile launch reported",
            snippet="",
            event_type="DRONE",
            severity="LOW",
            score=1,
            matched_keywords="drone",
        )

        event_type, severity, score, matched = purge_reevaluate(event)

        self.assertEqual(event_type, "MISSILE")
        self.assertEqual(severity, "HIGH")
        self.assertGreater(score, 1)
        self.assertIn("ballistic missile", matched)
        self.assertNotIn("drone", matched)

    def test_structured_sources_replace_stale_unknown_type(self) -> None:
        launch = Event(
            source="launch_library",
            title="Falcon 9 | NRO mission",
            event_type="UNKNOWN",
            matched_keywords="SpaceX, Falcon 9",
        )
        faa = Event(
            source="faa_tfr",
            title="FAA TFR 6/7491",
            event_type="UNKNOWN",
            matched_keywords="FAA TFR, SPACE OPERATIONS, NOTAM",
        )

        for reevaluate in (main_module._reevaluate, purge_reevaluate):
            with self.subTest(reevaluate=reevaluate.__module__):
                self.assertEqual(reevaluate(launch)[0], "ROCKET")
                self.assertEqual(reevaluate(faa)[0], "AIRSPACE")


class PipelineIntegrationTests(unittest.TestCase):
    def test_run_once_persists_and_notifies_with_local_dependencies(self) -> None:
        engine = create_engine("sqlite://", future=True)
        original_engine = database._engine
        original_session_local = database.SessionLocal
        database._engine = engine
        database.SessionLocal = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(engine)

        article = {
            "source": "gdelt",
            "title": "  Ballistic   missile launch reported  ",
            "url": "HTTPS://Example.COM/report/?utm_source=test&item=1#section",
            "domain": "example.com",
            "snippet": "A ballistic missile launch was reported.",
            "published_at": "2026-05-25T14:00:00+02:00",
        }

        try:
            with (
                patch.object(main_module.config, "configure_logging"),
                patch.object(main_module.config, "validate"),
                patch.object(main_module.config, "discord_webhook_url", "https://example.test/hook"),
                patch.object(main_module.config, "min_notify_severity", "MEDIUM"),
                patch.object(main_module.config, "max_notifications_per_run", 5),
                patch.object(main_module.config, "enable_launch_library", False),
                patch.object(main_module.config, "enable_faa_tfr", False),
                patch.object(main_module, "fetch_events", return_value=[article]),
                patch.object(main_module, "send_event", return_value=True) as send_event,
                patch.object(main_module.heartbeat, "check_and_send", return_value=False),
            ):
                main_module.run_once()

            with database.get_session() as session:
                events = list(session.scalars(select(Event)))

            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.url, "https://example.com/report?item=1")
            self.assertEqual(event.title, "Ballistic missile launch reported")
            self.assertEqual(event.published_at, datetime(2026, 5, 25, 12, 0, 0))
            self.assertEqual(event.event_type, "MISSILE")
            self.assertEqual(event.notified, 1)
            send_event.assert_called_once()

            events_response = asyncio.run(
                api_server.list_events(
                    hours=24,
                    event_type="MISSILE",
                    min_severity="MEDIUM",
                    country=None,
                    geo_only=False,
                    include_unknown=False,
                    limit=10,
                )
            )
            events_payload = json.loads(events_response.body)
            self.assertEqual(events_payload["count"], 1)
            self.assertEqual(events_payload["events"][0]["id"], event.id)

            stats_response = asyncio.run(api_server.stats(hours=24))
            stats_payload = json.loads(stats_response.body)
            self.assertEqual(stats_payload["total"], 1)
            self.assertEqual(stats_payload["by_type"], {"MISSILE": 1})
        finally:
            database.SessionLocal = original_session_local
            database._engine = original_engine
            engine.dispose()


if __name__ == "__main__":
    unittest.main()