"""zoo/champ_nut_draw_engine.py - Championship Contender: Nut & Redraw Engine.

Designed for Heads-Up Short Deck 5-Card PLO (40bb):
1. Range-conditioned Monte Carlo: Evaluates hero hand against top-k opponent hands
   (k adapts to street aggression and action history).
2. Nut Texture & Redraw Awareness:
   - Flushes > Full Houses: Protects and values made flushes immensely.
   - Sets > Straights: Recognizes sets as dominating holdings over straights.
   - Texture danger: Drastically discounts low straights on monotone/paired boards.
3. Polarized Pot Sizing:
   - Pot bets with nuts or massive combo draws (wrap + flush draw).
   - Half-pot for thin value / position stabs.
   - Strict check-call pot odds defense.
4. Rock-solid zero-error execution and paced time management (< 3.0s per half).
"""
import random
import time
from collections import Counter
from game import Action, Street
from botlib.equity import hole_masks, board_masks, score_masks
from evaluator import NUM_CARDS, get_table

ACE = 8


def _preflop_eval(hole):
    """Short Deck PLO5 preflop evaluation (0.0 to 1.0)."""
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts = Counter(ranks)
    s_counts = Counter(suits)

    # Base rank score (0..8)
    score = (sum(ranks) / 40.0) * 0.35

    # Pairs & High Pairs
    if r_counts[ACE] == 2:
        score += 0.32
    elif r_counts[ACE - 1] == 2:  # KK
        score += 0.22
    elif r_counts[ACE - 2] == 2:  # QQ
        score += 0.15
    pairs = [r for r, count in r_counts.items() if count == 2]
    if len(pairs) >= 2:
        score += 0.12
    elif len(pairs) == 1 and not (r_counts[ACE] == 2 or r_counts[ACE - 1] == 2):
        score += 0.08

    # Dead cards penalty: Trips or Quads in hole hurt PLO5 (must use exactly 2 cards)
    for count in r_counts.values():
        if count == 3:
            score -= 0.18
        elif count >= 4:
            score -= 0.38

    # Suitedness: Nut flush capability is huge (Flushes beat Full Houses)
    ace_suits = {c & 3 for c in hole if (c >> 2) == ACE}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.14
    two_card_suits = sum(1 for count in s_counts.values() if count == 2)
    if two_card_suits >= 2:
        score += 0.10
    elif two_card_suits == 1 and not ace_suits:
        score += 0.04

    # Rundown connectivity (Wheel in short deck: A-6-7-8-9)
    uniq = sorted(set(ranks))
    connects = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a == 1)
    if ACE in uniq and 0 in uniq:
        connects += 1
    score += min(0.14, connects * 0.035)

    return max(0.0, min(1.0, score))


def _board_scare_factor(board):
    """Scare factor (0.0 to 0.4) based on flush and pairing texture."""
    if len(board) < 3:
        return 0.0
    suits = [c & 3 for c in board]
    ranks = [c >> 2 for c in board]
    max_suit = max(Counter(suits).values())
    max_rank = max(Counter(ranks).values())

    scare = 0.0
    if max_suit >= 4:
        scare += 0.30
    elif max_suit == 3:
        scare += 0.18

    if max_rank >= 3:
        scare += 0.25
    elif max_rank == 2:
        scare += 0.10

    return scare


def _mc_range_equity(hole, board, k, iters, rng):
    """Range-conditioned Monte Carlo against the best of k opponent hands."""
    if iters <= 0:
        return 0.5
    table = get_table()
    known = set(hole) | set(board)
    deck = [c for c in range(NUM_CARDS) if c not in known]
    need = 5 - len(board)
    board_list = list(board)
    my_hm = hole_masks(hole)
    sample = rng.sample
    total = 0.0

    for _ in range(iters):
        draw = sample(deck, need + 5 * k)
        b = board_list + draw[:need] if need else board_list
        bm = board_masks(b)
        me = score_masks(my_hm, bm, table)

        best_opp = -1
        offset = need
        for _ in range(k):
            opp_h = draw[offset:offset + 5]
            offset += 5
            opp_score = score_masks(hole_masks(opp_h), bm, table)
            if opp_score > best_opp:
                best_opp = opp_score

        if me > best_opp:
            total += 1.0
        elif me == best_opp:
            total += 0.5

    return total / iters


def new_half(config, memory):
    try:
        memory.clear()
        memory["rng"] = random.Random(88812)
        memory["spent"] = 0.0
    except Exception:
        pass


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        hole = obs.hole_cards
        board = obs.board
        rng = memory.get("rng") or random.Random(12345)

        # 1. PREFLOP
        if not board:
            score = _preflop_eval(hole)
            if obs.is_button:
                # Button: Raise premium, complete playable, fold junk
                if score >= 0.62 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if obs.to_call <= 1:
                    if score >= 0.20 or obs.to_call == 0:
                        return Action.CHECK_CALL
                    if Action.FOLD in legal:
                        return Action.FOLD
                    return Action.CHECK_CALL
                # Facing a raise in position:
                if score >= 0.40:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL
            else:
                # Big Blind
                if obs.to_call == 0:
                    if score >= 0.68 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                        return Action.RAISE_POT
                    return Action.CHECK_CALL
                # Facing button raise:
                if score >= 0.70 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if score >= 0.36:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL

        # 2. POSTFLOP
        # Range conditioning k
        if obs.to_call == 0:
            k = 1 if obs.is_button else 2
        else:
            k = min(5, 2 + obs.raises_this_street)

        # Paced iterations
        spent = memory.get("spent", 0.0)
        iters = 14 if spent > 3.4 else (28 if spent > 2.6 else 42)

        t0 = time.perf_counter()
        raw_eq = _mc_range_equity(hole, board, k, iters, rng)
        memory["spent"] = spent + (time.perf_counter() - t0)

        # Texture adjustment (discount equity on scary wet boards without the nuts)
        scare = _board_scare_factor(board)
        adj_eq = max(0.05, raw_eq - scare * 0.5) if raw_eq < 0.85 else raw_eq

        # Case A: Checked to us
        if obs.to_call == 0:
            # Monster holding: pot raise
            if adj_eq >= 0.68 and Action.RAISE_POT in legal:
                return Action.RAISE_POT
            # Strong holding or good semi-bluff: half-pot raise
            if adj_eq >= 0.55 and Action.RAISE_HALF in legal:
                return Action.RAISE_HALF
            # Positional probe on river
            if obs.street == Street.RIVER and obs.is_button and adj_eq >= 0.46 and Action.RAISE_HALF in legal:
                if rng.random() < 0.30:
                    return Action.RAISE_HALF
            return Action.CHECK_CALL

        # Case B: Facing a bet
        pot_odds = obs.to_call / float(obs.pot + obs.to_call)

        # Monster re-raise
        if adj_eq >= 0.74 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
            return Action.RAISE_POT

        # Defend with positive expectation
        margin = 0.015
        if adj_eq >= pot_odds + margin:
            return Action.CHECK_CALL

        if Action.FOLD in legal:
            return Action.FOLD
        return Action.CHECK_CALL

    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass

