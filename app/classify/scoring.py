"""
classify/scoring.py
-------------------
Turns an article's keyword matches into a numeric score and a coarse
severity label (LOW / MEDIUM / HIGH).

Higher weights = stronger signals of a concrete, high-impact event.
Bare nouns ("drone", "missile") are intentionally low so a passing
mention doesn't inflate severity.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# Per-term weights. Anything not listed defaults to +1.
TERM_WEIGHTS: Dict[str, int] = {
    # --- MISSILE ---
    "ballistic missile": 5,
    "ballistic missiles": 5,
    "cruise missile": 4,
    "cruise missiles": 4,
    "hypersonic missile": 5,
    "hypersonic missiles": 5,
    "missile attack": 5,
    "missile barrage": 5,
    "missile launch": 4,
    "missile strike": 5,
    "icbm": 6,
    "icbms": 6,

    # --- DRONE ---
    "drone strike": 4,
    "drone attack": 4,
    "drone barrage": 4,
    "drone swarm": 4,
    "kamikaze drone": 4,
    "loitering munition": 4,
    "loitering munitions": 4,
    "shahed": 3,
    "unmanned aerial vehicle": 2,
    "unmanned aerial vehicles": 2,

    # --- ROCKET ---
    "rocket attack": 4,
    "rocket barrage": 4,
    "rocket fire": 3,
    "grad rocket": 3,
    "mlrs": 3,

    # --- AIR DEFENSE (named systems weighted higher) ---
    "iron dome": 4,
    "patriot system": 4,
    "patriot missile": 4,
    "patriot battery": 4,
    "s-400": 4,
    "s-300": 4,
    "thaad": 4,
    "nasams": 4,
    "air defense": 3,
    "air defence": 3,
    "missile defense": 3,
    "missile defence": 3,
    "anti-aircraft": 3,
    "anti aircraft": 3,

    # --- AIRSPACE ---
    "airspace closure": 4,
    "airspace closures": 4,
    "airspace closed": 4,
    "airspace restriction": 3,
    "airspace restrictions": 3,
    "no-fly zone": 4,
    "no fly zone": 4,
    "temporary flight restriction": 4,
    "flight ban": 3,
    "notam": 3,
    "tfr": 3,
}

# Severity thresholds applied to the final integer score.
# Order matters: highest threshold first.
SEVERITY_THRESHOLDS = [
    ("HIGH", 7),
    ("MEDIUM", 3),
    ("LOW", 0),
]

# Ordered weakest -> strongest, so we can compare with index().
SEVERITY_ORDER: List[str] = ["LOW", "MEDIUM", "HIGH"]


def score_matches(matched_keywords: List[str]) -> int:
    """Sum the per-term weights for the matched keywords."""
    total = 0
    for kw in matched_keywords:
        total += TERM_WEIGHTS.get(kw, 1)
    return total


def severity_from_score(score: int) -> str:
    for label, threshold in SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return "LOW"


def score_and_severity(matched_keywords: List[str]) -> Tuple[int, str]:
    score = score_matches(matched_keywords)
    return score, severity_from_score(score)


def severity_at_least(actual: str, minimum: str) -> bool:
    """True if `actual` severity is >= `minimum` in SEVERITY_ORDER."""
    try:
        return SEVERITY_ORDER.index(actual) >= SEVERITY_ORDER.index(minimum)
    except ValueError:
        return False
