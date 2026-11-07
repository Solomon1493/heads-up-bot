"""Preflop hand strength as a percentile (0 = weakest hand, 1 = strongest).

v2 uses this only to RANK hands (for opponent-range weighting), so what matters is the
order, not the scale. Provisional source: the v1 heuristic score, turned into a percentile
by sampling random hands once at import. The Databricks preflop equity table replaces it
when botlib/preflop_table.json exists (same function name, same meaning).
"""
import bisect
import json
import os
import random
from collections import Counter

_TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preflop_table.json")


def heuristic_score(hole):
    """v1's hand-written Short Deck PLO5 score, without the position bonus."""
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts, s_counts = Counter(ranks), Counter(suits)
    score = (sum(ranks) / 40.0) * 0.38
    pairs = [r for r, n in r_counts.items() if n == 2]
    if r_counts[8] == 2:
        score += 0.30
    elif r_counts[7] == 2:
        score += 0.20
    elif r_counts[6] == 2:
        score += 0.14
    elif pairs:
        score += 0.08
    if len(pairs) >= 2:
        score += 0.14
    for n in r_counts.values():
        if n == 3:
            score -= 0.16
        elif n >= 4:
            score -= 0.35
    ace_suits = {c & 3 for c in hole if (c >> 2) == 8}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.12
    two_suited = sum(1 for n in s_counts.values() if n == 2)
    if two_suited >= 2:
        score += 0.12
    elif two_suited == 1 and not ace_suits:
        score += 0.05
    for n in s_counts.values():
        if n == 3:
            score -= 0.06
        elif n >= 4:
            score -= 0.16
    uniq = sorted(set(ranks))
    connects = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a == 1)
    if 8 in uniq and 0 in uniq:
        connects += 1
    score += min(0.12, connects * 0.03)
    return score


def _build_cdf(samples=20000):
    rng = random.Random(2026)            # fixed: identical table in every process
    deck = list(range(36))
    return sorted(heuristic_score(rng.sample(deck, 5)) for _ in range(samples))


_TABLE = None
if os.path.exists(_TABLE_PATH):
    try:
        with open(_TABLE_PATH) as f:
            _TABLE = json.load(f)        # {"<sorted card ids joined by ,>": percentile}
    except Exception:
        _TABLE = None
_CDF = None if _TABLE else _build_cdf()


def percentile(hole):
    """Share of random hands weaker than this one (0..1)."""
    if _TABLE is not None:
        p = _TABLE.get(",".join(map(str, sorted(hole))))
        if p is not None:
            return p
    global _CDF
    if _CDF is None:
        _CDF = _build_cdf()              # only if the table is missing a hand
    return bisect.bisect_left(_CDF, heuristic_score(hole)) / len(_CDF)
