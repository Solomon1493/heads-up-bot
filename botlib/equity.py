"""Fast equity helpers for Short Deck PLO5, built on the official evaluator's table.

Results are identical to evaluator.omaha_score, but hole-card masks are computed once
per decision instead of once per simulated board, which roughly halves the cost of
a Monte Carlo sample.
"""
from itertools import combinations

from evaluator import NUM_CARDS, get_table

_PAIRS = tuple(combinations(range(5), 2))
_TRIPLES = tuple(combinations(range(5), 3))


def hole_masks(hole):
    bits = [1 << c for c in hole]
    return [bits[i] | bits[j] for i, j in _PAIRS]


def board_masks(board):
    bits = [1 << c for c in board]
    return [bits[i] | bits[j] | bits[k] for i, j, k in _TRIPLES]


def score_masks(hm, bm, table):
    return max([table[h | b] for h in hm for b in bm])


def mc_equity(hole, board, iters, rng):
    """Equity vs one uniformly random opponent hand. Ties count half."""
    if iters <= 0:
        return 0.5
    table = get_table()
    known = set(hole) | set(board)
    deck = [c for c in range(NUM_CARDS) if c not in known]
    need = 5 - len(board)
    board = list(board)
    my_hm = hole_masks(hole)
    sample = rng.sample
    total = 0.0
    for _ in range(iters):
        draw = sample(deck, need + 5)
        b = board + draw[:need] if need else board
        bm = board_masks(b)
        me = score_masks(my_hm, bm, table)
        opp = score_masks(hole_masks(draw[need:]), bm, table)
        if me > opp:
            total += 1.0
        elif me == opp:
            total += 0.5
    return total / iters


def preflop_features(hole):
    """Cheap preflop strength score (roughly 0..1). Replace with a precomputed table later."""
    ranks = sorted((c >> 2 for c in hole), reverse=True)
    suits = [c & 3 for c in hole]
    score = sum(ranks) / 40.0                                  # high cards
    pairs = len(ranks) - len(set(ranks))
    score += 0.10 * min(pairs, 2)
    suit_counts = sorted((suits.count(s) for s in set(suits)), reverse=True)
    score += 0.08 if suit_counts[0] >= 2 else 0.0              # at least one flush draw
    score += 0.05 if len(suit_counts) > 1 and suit_counts[1] >= 2 else 0.0  # double-suited
    score -= 0.10 if suit_counts[0] >= 4 else 0.0             # too many of one suit
    uniq = sorted(set(ranks))
    gaps = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a <= 2)
    score += 0.03 * gaps                                       # connectedness
    return max(0.0, min(1.0, score / 1.2))


def equity_policy(obs, rng, iters):
    """The 'typical AI bot' policy: equity vs pot odds with fixed raise thresholds."""
    from game import Action
    eq = mc_equity(obs.hole_cards, obs.board, iters, rng)
    legal = obs.legal_actions
    pot_odds = obs.to_call / (obs.pot + obs.to_call) if obs.to_call else 0.0
    if eq > 0.70 and Action.RAISE_POT in legal:
        return Action.RAISE_POT
    if eq > 0.60 and obs.to_call == 0 and Action.RAISE_HALF in legal:
        return Action.RAISE_HALF
    if obs.to_call == 0 or eq >= pot_odds:
        return Action.CHECK_CALL
    return Action.FOLD
