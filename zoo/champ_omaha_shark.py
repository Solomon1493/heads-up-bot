"""zoo/champ_omaha_shark.py - Championship Contender: Short-Deck PLO Specialist.

Built to replicate the playstyle of top-ranking human and machine Short-Deck PLO experts:
1. Short-Deck Hand Dynamics:
   - Flushes beat Full Houses (heavily values double-suited Ace hands).
   - Sets beat Straights (heavily values top sets on dynamic straight boards).
   - High rundowns (wraps) generate up to 20 outs; bets monster draws with equal aggression to made nuts.
2. Card Removal & Blocker Logic:
   - Subtracts value for dead cards in hand (trips/quads in hole block own outs).
   - Exploits Nut Flush blockers (holding the Ace of a 3-flush board) to bluff opponents off chops/straights.
3. Positional Multi-Street Pacing:
   - Button: High aggression, wide steal range, attacks passive checks.
   - Big Blind: Strict defensive filter, check-raises disguised monsters.
4. Range-conditioned Monte Carlo (< 3.0s compute per half, 0 errors, full safety wrapping).
"""
import random
import time
from collections import Counter
from game import Action, Street
from botlib.equity import hole_masks, board_masks, score_masks
from evaluator import NUM_CARDS, get_table

ACE = 8
KING = 7


def _eval_shortdeck_preflop(hole):
    """Accurate Short Deck PLO5 starting hand potential (0.0 to 1.0)."""
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts = Counter(ranks)
    s_counts = Counter(suits)

    # Base high card distribution
    high_score = (sum(r for r in ranks if r >= 5) / 32.0) * 0.25
    score = high_score

    # Pocket pairs
    pairs = [r for r, count in r_counts.items() if count == 2]
    if r_counts[ACE] == 2:
        score += 0.34
    elif r_counts[KING] == 2:
        score += 0.24
    elif r_counts[6] == 2:  # QQ
        score += 0.16
    elif len(pairs) >= 1:
        score += 0.08
    if len(pairs) >= 2:
        score += 0.14

    # Dead cards in hand: in PLO5, 3 or 4 of same rank destroy outs!
    for count in r_counts.values():
        if count == 3:
            score -= 0.20
        elif count >= 4:
            score -= 0.40

    # Nut Flush potential: Ace suited
    ace_suits = {c & 3 for c in hole if (c >> 2) == ACE}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.15  # Nut flush draw potential (flushes beat full houses)
        elif s_counts[s] >= 3:
            score += 0.06  # 3 of same suit in hand kills one of own outs

    # Secondary flush draws (King suited)
    king_suits = {c & 3 for c in hole if (c >> 2) == KING}
    for s in king_suits:
        if s not in ace_suits and s_counts[s] == 2:
            score += 0.07

    # Rundown wrap connectivity
    uniq = sorted(set(ranks))
    connects = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a == 1)
    if ACE in uniq and 0 in uniq:  # Wheel wrap A-6-7-8-9
        connects += 1
    score += min(0.16, connects * 0.04)

    return max(0.0, min(1.0, score))


def _has_nut_flush_blocker(hole, board):
    """Returns True if board has 3+ of a suit and hero holds the Ace of that suit."""
    if len(board) < 3:
        return False
    b_suits = [c & 3 for c in board]
    for s, cnt in Counter(b_suits).items():
        if cnt >= 3:
            nut_card = (ACE << 2) | s
            if nut_card in hole:
                return True
    return False


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
        memory["rng"] = random.Random(314159)
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
            pf_score = _eval_shortdeck_preflop(hole)
            if obs.is_button:
                # Button: Raise premium (AA, KK, double-suited wraps)
                if pf_score >= 0.60 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                # Complete / Limp with playables
                if obs.to_call <= 1:
                    if pf_score >= 0.18 or obs.to_call == 0:
                        return Action.CHECK_CALL
                    if Action.FOLD in legal:
                        return Action.FOLD
                    return Action.CHECK_CALL
                # Facing 3-bet in position
                if pf_score >= 0.40:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL
            else:
                # Big Blind (OOP)
                if obs.to_call == 0:
                    if pf_score >= 0.68 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                        return Action.RAISE_POT
                    return Action.CHECK_CALL
                # Facing Button Open
                if pf_score >= 0.70 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if pf_score >= 0.36:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL

        # 2. POSTFLOP
        if obs.to_call == 0:
            k = 1 if obs.is_button else 2
        else:
            k = min(4, 2 + obs.raises_this_street)

        spent = memory.get("spent", 0.0)
        iters = 14 if spent > 3.4 else (28 if spent > 2.6 else 42)

        t0 = time.perf_counter()
        eq = _mc_range_equity(hole, board, k, iters, rng)
        memory["spent"] = spent + (time.perf_counter() - t0)

        has_nf_blocker = _has_nut_flush_blocker(hole, board)

        # Case A: Checked to us (to_call == 0)
        if obs.to_call == 0:
            # Monster holding: pot raise
            if eq >= 0.68 and Action.RAISE_POT in legal:
                return Action.RAISE_POT
            # Solid value holding: half-pot raise
            if eq >= 0.55 and Action.RAISE_HALF in legal:
                return Action.RAISE_HALF
            # Nut Blocker Bluff on turn or river
            if obs.street in (Street.TURN, Street.RIVER) and has_nf_blocker and eq < 0.40 and Action.RAISE_HALF in legal:
                if rng.random() < 0.30:
                    return Action.RAISE_HALF
            # Button probe on river
            if obs.street == Street.RIVER and obs.is_button and eq >= 0.45 and Action.RAISE_HALF in legal:
                if rng.random() < 0.25:
                    return Action.RAISE_HALF
            return Action.CHECK_CALL

        # Case B: Facing a bet (to_call > 0)
        pot_odds = obs.to_call / float(obs.pot + obs.to_call)

        # Monster re-raise
        if eq >= 0.75 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
            return Action.RAISE_POT

        # Equity beats price + slight margin
        margin = 0.015
        if eq >= pot_odds + margin:
            return Action.CHECK_CALL

        if Action.FOLD in legal:
            return Action.FOLD
        return Action.CHECK_CALL

    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass

