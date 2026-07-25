"""
classify/rules.py
-----------------
Keyword-based event-type classifier.

Two design decisions to flag:
  1. The source's "matched_keyword" (the GDELT query term that surfaced
     the article) is NOT used as a classification signal. Earlier versions
     concatenated it into the search text, which meant every article
     returned by the "drone strike" query auto-matched DRONE even when
     the title was about something else entirely. Classification now
     uses ONLY the article's title and snippet.

  2. AIR_DEFENSE no longer matches on the bare word "intercepted". That
     word is too noisy — it matched drug seizures, sports stories, and
     crime reports. AIR_DEFENSE now requires a named system (Iron Dome,
     Patriot, S-400, etc.) or a defense-specific phrase. Intercepted
     missile/drone events still classify correctly: they'll match the
     MISSILE or DRONE bucket instead, which is the more useful framing.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from app.classify.scoring import TERM_WEIGHTS

# Keywords grouped by event type. All comparisons are case-insensitive.
EVENT_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "DRONE": [
        "drone strike",
        "drone attack",
        "drone barrage",
        "drone swarm",
        "kamikaze drone",
        "loitering munition",
        "loitering munitions",
        "unmanned aerial vehicle",
        "unmanned aerial vehicles",
        "shahed",
        "uav",
        "uavs",
        # generic (low-weight) fallbacks — only fire when a more specific
        # term doesn't match
        "drone",
        "drones",
    ],
    "MISSILE": [
        "ballistic missile",
        "ballistic missiles",
        "cruise missile",
        "cruise missiles",
        "hypersonic missile",
        "hypersonic missiles",
        "missile attack",
        "missile barrage",
        "missile launch",
        "missile strike",
        "icbm",
        "icbms",
        "missile",
        "missiles",
    ],
    "ROCKET": [
        "rocket attack",
        "rocket barrage",
        "rocket fire",
        "grad rocket",
        "mlrs",
        # Note: "rocket launch" intentionally omitted from generic ROCKET
        # to avoid civilian space-launch coverage. Add it back if you
        # also want SpaceX/NASA launches.
        "rocket",
        "rockets",
    ],
    "AIR_DEFENSE": [
        # Named systems — strong AD signal
        "iron dome",
        "patriot system",
        "patriot missile",
        "patriot battery",
        "s-400",
        "s-300",
        "thaad",
        "nasams",
        # Generic AD phrasing
        "air defense",
        "air defence",
        "missile defense",
        "missile defence",
        "anti-aircraft",
        "anti aircraft",
    ],
    "AIRSPACE": [
        "airspace closure",
        "airspace closures",
        "airspace closed",
        "airspace restriction",
        "airspace restrictions",
        "no-fly zone",
        "no fly zone",
        "temporary flight restriction",
        "flight ban",
        "notam",
        "tfr",
    ],
}

# Tiebreak priority when two buckets score equally. AIR_DEFENSE is last
# because in practice AD mentions appear inside missile/drone stories
# and we'd rather call those by what happened, not by what defended.
TYPE_PRIORITY = ["AIRSPACE", "MISSILE", "DRONE", "ROCKET", "AIR_DEFENSE"]


def _haystack(article: Dict) -> str:
    """Searchable text for classification. Title + snippet only — the
    source's matched_keyword is deliberately excluded."""
    parts = [
        article.get("title") or "",
        article.get("snippet") or "",
    ]
    return " ".join(parts).lower()


def find_matches(article: Dict) -> Tuple[str, List[str]]:
    """Return (event_type, matched_keywords) for an article.

    Bucket selection: the bucket with the highest summed TERM_WEIGHTS
    wins. Ties are broken by TYPE_PRIORITY. This is more robust than a
    pure priority scheme because a single specific term (e.g. "ballistic
    missile", weight 5) outweighs a generic match (e.g. bare "drone",
    weight 1).
    """
    text = _haystack(article)
    bucket_hits: Dict[str, List[str]] = {}
    bucket_scores: Dict[str, int] = {}

    for event_type, kws in EVENT_TYPE_KEYWORDS.items():
        hits = [kw for kw in kws if kw in text]
        if not hits:
            continue
        bucket_hits[event_type] = hits
        bucket_scores[event_type] = sum(TERM_WEIGHTS.get(kw, 1) for kw in hits)

    if not bucket_hits:
        return "UNKNOWN", []

    max_score = max(bucket_scores.values())
    top_candidates = {t for t, s in bucket_scores.items() if s == max_score}

    chosen: Optional[str] = None
    for cand in TYPE_PRIORITY:
        if cand in top_candidates:
            chosen = cand
            break
    if chosen is None:
        chosen = next(iter(top_candidates))

    # Flatten + de-dup matched keywords across all matched buckets,
    # preserving the order in which they were first seen.
    all_hits: List[str] = []
    seen: set[str] = set()
    for hits in bucket_hits.values():
        for kw in hits:
            if kw not in seen:
                seen.add(kw)
                all_hits.append(kw)

    return chosen, all_hits


def classify_event_type(article: Dict) -> str:
    event_type, _ = find_matches(article)
    return event_type
