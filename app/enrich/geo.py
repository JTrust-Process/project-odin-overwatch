"""
enrich/geo.py
-------------
Best-effort geo enrichment using a built-in gazetteer.

How it works:
  1. The gazetteer lists ~150 places — conflict-zone cities first, then
     a global set of countries as fallback.
  2. For each article, we scan title + snippet for any place name
     (with word boundaries). Longest match wins; cities outrank countries.
  3. We return the matched place with its pre-baked lat/lon.

No external API calls, no rate limits, no surprise costs. Coverage is
coarse but reliable for the regions that show up in public defense
reporting.

To extend: add an entry to GAZETTEER below. The format is:
    Place(name, aliases, country, lat, lon, kind)
where `kind` is "city" or "country". Cities get search-priority over
countries so "Kyiv" beats "Ukraine" when both appear in the same headline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Place:
    name: str
    aliases: Tuple[str, ...]
    country: Optional[str]  # None when the entry IS a country
    lat: float
    lon: float
    kind: str  # "city" or "country"


# -----------------------------------------------------------------------------
# Gazetteer
#
# Coordinates are at ~2-decimal precision (~1 km), good enough for a
# dashboard pin. Aliases cover common transliterations (Kyiv/Kiev).
# Keep entries unambiguous: don't add bare leader names or single-syllable
# country adjectives — they create false positives.
# -----------------------------------------------------------------------------
GAZETTEER: List[Place] = [
    # ===== Ukraine =====
    Place("Kyiv", ("Kiev",), "Ukraine", 50.45, 30.52, "city"),
    Place("Kharkiv", ("Kharkov",), "Ukraine", 49.99, 36.23, "city"),
    Place("Donetsk", (), "Ukraine", 48.00, 37.80, "city"),
    Place("Luhansk", ("Lugansk",), "Ukraine", 48.57, 39.30, "city"),
    Place("Mariupol", (), "Ukraine", 47.10, 37.55, "city"),
    Place("Odesa", ("Odessa",), "Ukraine", 46.48, 30.73, "city"),
    Place("Lviv", ("Lvov",), "Ukraine", 49.84, 24.03, "city"),
    Place("Dnipro", ("Dnepropetrovsk",), "Ukraine", 48.47, 35.04, "city"),
    Place("Kherson", (), "Ukraine", 46.65, 32.62, "city"),
    Place("Zaporizhzhia", ("Zaporozhye",), "Ukraine", 47.82, 35.18, "city"),
    Place("Bakhmut", (), "Ukraine", 48.59, 38.00, "city"),
    Place("Avdiivka", (), "Ukraine", 48.14, 37.74, "city"),
    Place("Sevastopol", (), "Ukraine", 44.62, 33.52, "city"),
    Place("Crimea", (), "Ukraine", 45.30, 34.40, "city"),
    Place("Sumy", (), "Ukraine", 50.91, 34.80, "city"),
    Place("Cherkasy", (), "Ukraine", 49.44, 32.06, "city"),
    Place("Mykolaiv", ("Nikolaev",), "Ukraine", 46.97, 31.99, "city"),
    Place("Donbas", ("Donbass",), "Ukraine", 48.00, 38.00, "city"),

    # ===== Russia =====
    Place("Moscow", (), "Russia", 55.75, 37.62, "city"),
    Place("St Petersburg", ("Saint Petersburg",), "Russia", 59.93, 30.34, "city"),
    Place("Belgorod", (), "Russia", 50.60, 36.59, "city"),
    Place("Rostov", (), "Russia", 47.24, 39.71, "city"),
    Place("Voronezh", (), "Russia", 51.66, 39.20, "city"),
    Place("Kursk", (), "Russia", 51.74, 36.19, "city"),
    Place("Bryansk", (), "Russia", 53.24, 34.36, "city"),
    Place("Krasnodar", (), "Russia", 45.04, 38.98, "city"),
    Place("Sochi", (), "Russia", 43.59, 39.72, "city"),
    Place("Kazan", (), "Russia", 55.79, 49.12, "city"),
    Place("Kuban", (), "Russia", 45.04, 38.97, "city"),

    # ===== Israel / Palestinian Territories =====
    Place("Tel Aviv", (), "Israel", 32.08, 34.78, "city"),
    Place("Jerusalem", (), "Israel", 31.78, 35.22, "city"),
    Place("Haifa", (), "Israel", 32.79, 34.99, "city"),
    Place("Ashkelon", (), "Israel", 31.67, 34.57, "city"),
    Place("Ashdod", (), "Israel", 31.80, 34.65, "city"),
    Place("Gaza City", (), "Gaza", 31.50, 34.47, "city"),
    Place("Gaza Strip", ("Gaza",), "Gaza", 31.45, 34.40, "city"),
    Place("Rafah", (), "Gaza", 31.30, 34.25, "city"),
    Place("Khan Younis", ("Khan Yunis",), "Gaza", 31.34, 34.30, "city"),
    Place("West Bank", (), "Palestinian Territories", 31.95, 35.30, "city"),
    Place("Ramallah", (), "Palestinian Territories", 31.90, 35.20, "city"),

    # ===== Lebanon / Syria =====
    Place("Beirut", (), "Lebanon", 33.89, 35.50, "city"),
    Place("Tyre", (), "Lebanon", 33.27, 35.20, "city"),
    Place("Sidon", (), "Lebanon", 33.56, 35.37, "city"),
    Place("Damascus", (), "Syria", 33.51, 36.29, "city"),
    Place("Aleppo", (), "Syria", 36.20, 37.16, "city"),
    Place("Homs", (), "Syria", 34.73, 36.71, "city"),
    Place("Latakia", (), "Syria", 35.52, 35.79, "city"),
    Place("Idlib", (), "Syria", 35.93, 36.63, "city"),

    # ===== Iran / Iraq / Gulf =====
    Place("Tehran", (), "Iran", 35.69, 51.39, "city"),
    Place("Isfahan", (), "Iran", 32.65, 51.67, "city"),
    Place("Natanz", (), "Iran", 33.51, 51.92, "city"),
    Place("Bandar Abbas", (), "Iran", 27.18, 56.27, "city"),
    Place("Strait of Hormuz", ("Hormuz",), "Iran", 26.57, 56.25, "city"),
    Place("Baghdad", (), "Iraq", 33.31, 44.36, "city"),
    Place("Erbil", ("Arbil",), "Iraq", 36.19, 44.00, "city"),
    Place("Mosul", (), "Iraq", 36.34, 43.13, "city"),
    Place("Basra", (), "Iraq", 30.50, 47.78, "city"),
    Place("Sanaa", ("Sana'a",), "Yemen", 15.37, 44.19, "city"),
    Place("Aden", (), "Yemen", 12.78, 45.04, "city"),
    Place("Hodeidah", ("Hudaydah",), "Yemen", 14.80, 42.95, "city"),
    Place("Riyadh", (), "Saudi Arabia", 24.71, 46.68, "city"),
    Place("Jeddah", (), "Saudi Arabia", 21.49, 39.19, "city"),
    Place("Doha", (), "Qatar", 25.29, 51.53, "city"),
    Place("Dubai", (), "UAE", 25.20, 55.27, "city"),
    Place("Abu Dhabi", (), "UAE", 24.47, 54.37, "city"),
    Place("Manama", (), "Bahrain", 26.23, 50.59, "city"),
    Place("Kuwait City", (), "Kuwait", 29.38, 47.99, "city"),

    # ===== Turkey =====
    Place("Ankara", (), "Turkey", 39.93, 32.86, "city"),
    Place("Istanbul", (), "Turkey", 41.01, 28.97, "city"),

    # ===== Egypt / North Africa =====
    Place("Cairo", (), "Egypt", 30.04, 31.24, "city"),
    Place("Sinai", (), "Egypt", 29.50, 33.74, "city"),
    Place("Tripoli", (), "Libya", 32.89, 13.18, "city"),
    Place("Benghazi", (), "Libya", 32.12, 20.07, "city"),
    Place("Tunis", (), "Tunisia", 36.81, 10.18, "city"),
    Place("Algiers", (), "Algeria", 36.75, 3.04, "city"),

    # ===== Sub-Saharan Africa =====
    Place("Khartoum", (), "Sudan", 15.50, 32.56, "city"),
    Place("Darfur", (), "Sudan", 13.20, 24.27, "city"),
    Place("Juba", (), "South Sudan", 4.85, 31.58, "city"),
    Place("Bamako", (), "Mali", 12.65, -8.00, "city"),
    Place("Ouagadougou", (), "Burkina Faso", 12.37, -1.52, "city"),
    Place("Niamey", (), "Niger", 13.51, 2.11, "city"),
    Place("Mogadishu", (), "Somalia", 2.05, 45.32, "city"),
    Place("Addis Ababa", (), "Ethiopia", 9.03, 38.74, "city"),
    Place("Lagos", (), "Nigeria", 6.45, 3.40, "city"),
    Place("Abuja", (), "Nigeria", 9.06, 7.49, "city"),
    Place("Sahel", (), None, 17.00, 0.00, "city"),

    # ===== South Asia =====
    Place("New Delhi", (), "India", 28.61, 77.21, "city"),
    Place("Mumbai", (), "India", 19.08, 72.88, "city"),
    Place("Kashmir", (), "India", 34.08, 76.69, "city"),
    Place("Islamabad", (), "Pakistan", 33.69, 73.05, "city"),
    Place("Karachi", (), "Pakistan", 24.86, 67.01, "city"),
    Place("Lahore", (), "Pakistan", 31.55, 74.34, "city"),
    Place("Quetta", (), "Pakistan", 30.18, 67.00, "city"),
    Place("Peshawar", (), "Pakistan", 34.02, 71.58, "city"),
    Place("Balochistan", ("Baluchistan",), "Pakistan", 28.49, 65.10, "city"),
    Place("Kabul", (), "Afghanistan", 34.53, 69.17, "city"),
    Place("Kandahar", (), "Afghanistan", 31.61, 65.71, "city"),
    Place("Colombo", (), "Sri Lanka", 6.93, 79.86, "city"),
    Place("Dhaka", (), "Bangladesh", 23.81, 90.41, "city"),

    # ===== East Asia =====
    Place("Beijing", (), "China", 39.90, 116.41, "city"),
    Place("Shanghai", (), "China", 31.23, 121.47, "city"),
    Place("Fujian", (), "China", 26.08, 119.30, "city"),
    Place("Taipei", (), "Taiwan", 25.03, 121.57, "city"),
    Place("Kaohsiung", (), "Taiwan", 22.63, 120.30, "city"),
    Place("Pyongyang", (), "North Korea", 39.03, 125.75, "city"),
    Place("Nampo", (), "North Korea", 38.74, 125.41, "city"),
    Place("Wonsan", (), "North Korea", 39.16, 127.43, "city"),
    Place("Seoul", (), "South Korea", 37.57, 126.98, "city"),
    Place("Busan", ("Pusan",), "South Korea", 35.18, 129.08, "city"),
    Place("Tokyo", (), "Japan", 35.68, 139.65, "city"),
    Place("Okinawa", (), "Japan", 26.50, 127.94, "city"),
    Place("Manila", (), "Philippines", 14.60, 120.98, "city"),

    # ===== Europe =====
    Place("London", (), "United Kingdom", 51.51, -0.13, "city"),
    Place("Paris", (), "France", 48.86, 2.35, "city"),
    Place("Berlin", (), "Germany", 52.52, 13.40, "city"),
    Place("Warsaw", (), "Poland", 52.23, 21.01, "city"),
    Place("Brussels", (), "Belgium", 50.85, 4.35, "city"),
    Place("Madrid", (), "Spain", 40.42, -3.70, "city"),
    Place("Rome", (), "Italy", 41.90, 12.50, "city"),
    Place("Athens", (), "Greece", 37.98, 23.73, "city"),
    Place("Helsinki", (), "Finland", 60.17, 24.94, "city"),
    Place("Stockholm", (), "Sweden", 59.33, 18.06, "city"),
    Place("Oslo", (), "Norway", 59.91, 10.75, "city"),
    Place("Copenhagen", (), "Denmark", 55.68, 12.57, "city"),
    Place("Riga", (), "Latvia", 56.95, 24.11, "city"),
    Place("Vilnius", (), "Lithuania", 54.69, 25.27, "city"),
    Place("Tallinn", (), "Estonia", 59.44, 24.75, "city"),
    Place("Minsk", (), "Belarus", 53.90, 27.57, "city"),
    Place("Bucharest", (), "Romania", 44.43, 26.10, "city"),
    Place("Budapest", (), "Hungary", 47.50, 19.04, "city"),
    Place("Prague", (), "Czech Republic", 50.08, 14.44, "city"),
    Place("Vienna", (), "Austria", 48.21, 16.37, "city"),

    # ===== Americas =====
    Place("Washington", ("Washington DC", "Washington D.C."), "USA", 38.91, -77.04, "city"),
    Place("Pentagon", (), "USA", 38.87, -77.06, "city"),
    Place("New York", (), "USA", 40.71, -74.01, "city"),
    Place("Mexico City", (), "Mexico", 19.43, -99.13, "city"),
    Place("Bogota", ("Bogotá",), "Colombia", 4.71, -74.07, "city"),
    Place("Caracas", (), "Venezuela", 10.49, -66.88, "city"),
    Place("Buenos Aires", (), "Argentina", -34.60, -58.38, "city"),
    Place("Brasilia", ("Brasília",), "Brazil", -15.79, -47.88, "city"),

    # ===== Countries (lower-priority fallback) =====
    Place("Ukraine", (), None, 49.00, 32.00, "country"),
    Place("Russia", (), None, 60.00, 100.00, "country"),
    Place("Belarus", (), None, 53.71, 27.95, "country"),
    Place("Israel", (), None, 31.50, 34.75, "country"),
    Place("Lebanon", (), None, 33.85, 35.85, "country"),
    Place("Syria", (), None, 35.00, 38.00, "country"),
    Place("Iran", (), None, 32.00, 53.00, "country"),
    Place("Iraq", (), None, 33.22, 43.68, "country"),
    Place("Yemen", (), None, 15.55, 48.52, "country"),
    Place("Saudi Arabia", (), None, 23.89, 45.08, "country"),
    Place("Qatar", (), None, 25.35, 51.18, "country"),
    Place("Bahrain", (), None, 26.07, 50.55, "country"),
    Place("Kuwait", (), None, 29.31, 47.48, "country"),
    Place("Oman", (), None, 21.51, 55.92, "country"),
    Place("Jordan", (), None, 30.59, 36.24, "country"),
    Place("Turkey", (), None, 38.96, 35.24, "country"),
    Place("Egypt", (), None, 26.82, 30.80, "country"),
    Place("Libya", (), None, 26.34, 17.23, "country"),
    Place("Tunisia", (), None, 33.89, 9.54, "country"),
    Place("Algeria", (), None, 28.03, 1.66, "country"),
    Place("Morocco", (), None, 31.79, -7.09, "country"),
    Place("Sudan", (), None, 12.86, 30.22, "country"),
    Place("South Sudan", (), None, 6.88, 31.31, "country"),
    Place("Ethiopia", (), None, 9.15, 40.49, "country"),
    Place("Somalia", (), None, 5.15, 46.20, "country"),
    Place("Mali", (), None, 17.57, -3.99, "country"),
    Place("Niger", (), None, 17.61, 8.08, "country"),
    Place("Burkina Faso", (), None, 12.24, -1.56, "country"),
    Place("Chad", (), None, 15.45, 18.73, "country"),
    Place("Nigeria", (), None, 9.08, 8.68, "country"),
    Place("Pakistan", (), None, 30.38, 69.35, "country"),
    Place("Afghanistan", (), None, 33.94, 67.71, "country"),
    Place("India", (), None, 20.59, 78.96, "country"),
    Place("China", (), None, 35.86, 104.20, "country"),
    Place("Taiwan", (), None, 23.70, 120.96, "country"),
    Place("Japan", (), None, 36.20, 138.25, "country"),
    Place("North Korea", (), None, 40.34, 127.51, "country"),
    Place("South Korea", (), None, 35.91, 127.77, "country"),
    Place("Philippines", (), None, 12.88, 121.77, "country"),
    Place("Myanmar", ("Burma",), None, 21.91, 95.96, "country"),
    Place("Thailand", (), None, 15.87, 100.99, "country"),
    Place("Vietnam", (), None, 14.06, 108.28, "country"),
    Place("USA", ("United States", "U.S."), None, 39.83, -98.58, "country"),
    Place("United Kingdom", ("UK", "Britain"), None, 55.38, -3.44, "country"),
    Place("France", (), None, 46.23, 2.21, "country"),
    Place("Germany", (), None, 51.17, 10.45, "country"),
    Place("Poland", (), None, 51.92, 19.15, "country"),
    Place("Finland", (), None, 61.92, 25.75, "country"),
    Place("Sweden", (), None, 60.13, 18.64, "country"),
    Place("Norway", (), None, 60.47, 8.47, "country"),
    Place("Denmark", (), None, 56.26, 9.50, "country"),
    Place("Estonia", (), None, 58.59, 25.01, "country"),
    Place("Latvia", (), None, 56.88, 24.60, "country"),
    Place("Lithuania", (), None, 55.17, 23.88, "country"),
    Place("Romania", (), None, 45.94, 24.97, "country"),
    Place("Bulgaria", (), None, 42.73, 25.49, "country"),
    Place("Greece", (), None, 39.07, 21.82, "country"),
    Place("Italy", (), None, 41.87, 12.57, "country"),
    Place("Spain", (), None, 40.46, -3.75, "country"),
    Place("Netherlands", (), None, 52.13, 5.29, "country"),
    Place("Mexico", (), None, 23.63, -102.55, "country"),
    Place("Colombia", (), None, 4.57, -74.30, "country"),
    Place("Venezuela", (), None, 6.42, -66.59, "country"),
    Place("Brazil", (), None, -14.24, -51.93, "country"),
    Place("Argentina", (), None, -38.42, -63.62, "country"),
    Place("Australia", (), None, -25.27, 133.78, "country"),
]


# Pre-compile regex patterns once at import. Each entry maps a compiled
# pattern -> Place. We use \b word boundaries so "Iran" doesn't match
# inside "Iranian" or hyphenated tokens.
def _build_patterns() -> List[Tuple[re.Pattern, Place, int]]:
    items: List[Tuple[re.Pattern, Place, int]] = []
    for place in GAZETTEER:
        for name in (place.name, *place.aliases):
            # Word-boundary match, case-insensitive.
            pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
            # Match priority: cities trump countries; within a tier,
            # longer names trump shorter (more specific).
            base = 1000 if place.kind == "city" else 100
            priority = base + len(name)
            items.append((pattern, place, priority))
    # Highest priority first so the first match in find_location wins.
    items.sort(key=lambda t: -t[2])
    return items


_PATTERNS: List[Tuple[re.Pattern, Place, int]] = _build_patterns()


def find_location(text: str) -> Optional[Place]:
    """Return the best-matching Place for `text`, or None.

    "Best" = highest priority (city > country, longer name > shorter)
    that appears anywhere in the text with word boundaries.
    """
    if not text:
        return None
    for pattern, place, _prio in _PATTERNS:
        if pattern.search(text):
            return place
    return None


def enrich(article: Dict) -> Dict:
    """In-place geo enrichment. Sets location_name, country, latitude,
    longitude on the article dict if a match is found. Safe to call on
    every article; no network calls.

    If the source already populated latitude (e.g. Launch Library 2 or
    FAA TFR), we leave the existing values alone — the source-provided
    coordinates are precise, ours are a gazetteer guess."""
    if article.get("latitude") is not None:
        return article
    text = " ".join([article.get("title") or "", article.get("snippet") or ""])
    place = find_location(text)
    if place is None:
        return article
    article["location_name"] = place.name
    article["country"] = place.country or place.name
    article["latitude"] = place.lat
    article["longitude"] = place.lon
    return article
