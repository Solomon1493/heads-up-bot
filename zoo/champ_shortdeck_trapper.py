"""zoo/champ_shortdeck_trapper.py - Championship Contender: Short Deck Trapper & Check-Raise Assassin.

In pokerbot competitions, equity-based bots have a fatal blindspot:
They assume a check indicates weakness and automatically stab/probe.
The Trapper turns this into a devastating trap:
1. Deceptive Checking: Checks monsters (sets, flushes, nut straights) out of position to induce stabs.
2. The Pot Check-Raise: When the opponent stabs into the check, springs a Pot-sized Check-Raise,
   forcing opponents to stack off or surrender the pot.
3. Fast Short Deck PLO5 Preflop Heuristic: Plays high cards, pairs, and double-suited hands.
4. Range-aware Monte Carlo postflop with strict time budget (< 3.0s per half, 0 errors).
"""
import random
import time
from collections import Counter
from game import Action, Street
from botlib.equity import hole_masks, board_masks, score_masks
from evaluator import NUM_CARDS, get_table

ACE = 8


def _preflop_score(hole):
    """Fast Short Deck PLO5 preflop evaluation."""
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts = Counter(ranks)
    s_counts = Counter(suits)

    score = (sum(ranks) / 40.0) * 0.35

    # Pocket pairs
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

    # Blockers/dead card penalty (trips/quads in hole)
    for count in r_counts.values():
        if count == 3:
            score -= 0.18
        elif count >= 4:
            score -= 0.36

    # Suitedness (Flushes beat Full Houses in Short Deck)
    ace_suits = {c & 3 for c in hole if (c >> 2) == ACE}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.14
    two_card_suits = sum(1 for count in s_counts.values() if count == 2)
    if two_card_suits >= 2:
        score += 0.10
    elif two_card_suits == 1 and not ace_suits:
        score += 0.04

    # Connectors & wraps (Wheel: A-6-7-8-9)
    uniq = sorted(set(ranks))
    connects = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a == 1)
    if ACE in uniq and 0 in uniq:
        connects += 1
    score += min(0.14, connects * 0.035)

    return max(0.0, min(1.0, score))


def _mc_range_equity(hole, board, k, iters, rng):
    """Range-conditioned Monte Carlo against top-k opponent hands."""
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
        memory["rng"] = random.Random(654321)
        memory["spent"] = 0.0
        memory["trapping"] = False
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
            score = _preflop_score(hole)
            if obs.is_button:
                # Button: raise top hands, limp playables
                if score >= 0.62 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if obs.to_call <= 1:
                    if score >= 0.20 or obs.to_call == 0:
                        return Action.CHECK_CALL
                    if Action.FOLD in legal:
                        return Action.FOLD
                    return Action.CHECK_CALL
                if score >= 0.40:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL
            else:
                # Big Blind (OOP)
                if obs.to_call == 0:
                    # Limp trap: with super-monsters (AA, KK with suits), check to conceal
                    if score >= 0.78 and Action.RAISE_POT in legal and rng.random() < 0.40:
                        return Action.RAISE_POT
                    return Action.CHECK_CALL
                # Facing button raise: 3-bet pot with top hands, call with playables
                if score >= 0.72 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if score >= 0.38:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL

        # 2. POSTFLOP
        is_oop = not obs.is_button
        k = 1 if (obs.to_call == 0 and obs.is_button) else min(4, 2 + obs.raises_this_street)

        spent = memory.get("spent", 0.0)
        iters = 14 if spent > 3.4 else (28 if spent > 2.6 else 42)

        t0 = time.perf_counter()
        eq = _mc_range_equity(hole, board, k, iters, rng)
        memory["spent"] = spent + (time.perf_counter() - t0)

        # Case A: Checked to us (to_call == 0)
        if obs.to_call == 0:
            if is_oop:
                # OOP TRAP ENGINE:
                # If we have a monster (nuts, high set, made flush, eq > 0.70), check to induce a bet!
                if eq >= 0.70:
                    memory["trapping"] = True
                    return Action.CHECK_CALL
                # If medium hand: check-call
                memory["trapping"] = False
                return Action.CHECK_CALL
            else:
                # In Position (Button):
                # Monster: pot raise
                if eq >= 0.68 and Action.RAISE_POT in legal:
                    return Action.RAISE_POT
                # Solid value stab: half pot
                if eq >= 0.55 and Action.RAISE_HALF in legal:
                    return Action.RAISE_HALF
                # Positional bluff when checked to on turn/river
                if obs.street in (Street.TURN, Street.RIVER) and eq < 0.35 and Action.RAISE_HALF in legal:
                    if rng.random() < 0.25:
                        return Action.RAISE_HALF
                return Action.CHECK_CALL

        # Case B: Facing a bet (to_call > 0)
        # THE CHECK-RAISE SPRING:
        if is_oop and memory.get("trapping"):
            memory["trapping"] = False
            # Opponent fell into the trap! Spring check-raise pot!
            if eq >= 0.65 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                return Action.RAISE_POT

        # Normal defense facing bet:
        pot_odds = obs.to_call / float(obs.pot + obs.to_call)
        if eq >= 0.74 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
            return Action.RAISE_POT

        if eq >= pot_odds + 0.01:
            return Action.CHECK_CALL

        if Action.FOLD in legal:
            return Action.FOLD
        return Action.CHECK_CALL

    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    try:
        memory["trapping"] = False
    except Exception:
        pass

