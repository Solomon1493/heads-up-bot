"""zoo/champ_range_polarizer.py - High-caliber tournament contender bot.

Design principles:
1. Fast heuristic preflop (0 ms compute wasted preflop).
2. Range-aware Monte Carlo postflop with board texture and nut blocker logic.
3. Polarized bet sizing: pot for monster hands / nut blocker bluffs, half-pot for merged stabs.
4. Paced time management targeting < 3.5s of compute per half.
"""
import random
import time
from collections import Counter
from game import Action, Street
from botlib.equity import hole_masks, board_masks, score_masks
from evaluator import NUM_CARDS, get_table

# Preflop rank/suit constants
ACE = 8


def _preflop_score(hole):
    """Heuristic Short Deck PLO5 strength (0.0 to 1.0) with SD hand order.
    Flushes > Full Houses, Sets > Straights.
    """
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts = Counter(ranks)
    s_counts = Counter(suits)

    # High cards (0..8)
    score = (sum(ranks) / 40.0) * 0.36

    # Pairs & Sets
    pairs = [r for r, count in r_counts.items() if count == 2]
    if r_counts[ACE] == 2:
        score += 0.30
    elif r_counts[ACE - 1] == 2:  # KK
        score += 0.20
    elif r_counts[ACE - 2] == 2:  # QQ
        score += 0.14
    elif len(pairs) >= 1:
        score += 0.08
    if len(pairs) >= 2:
        score += 0.12

    # Blocker penalties (trips / quads in hole are negative in PLO5)
    for count in r_counts.values():
        if count == 3:
            score -= 0.16
        elif count >= 4:
            score -= 0.35

    # Suitedness & Nut Flush draws
    ace_suits = {c & 3 for c in hole if (c >> 2) == ACE}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.12
    two_card_suits = sum(1 for count in s_counts.values() if count == 2)
    if two_card_suits >= 2:
        score += 0.12
    elif two_card_suits == 1 and not ace_suits:
        score += 0.04

    # Connectors
    uniq = sorted(set(ranks))
    connects = sum(1 for a, b in zip(uniq, uniq[1:]) if b - a == 1)
    if ACE in uniq and 0 in uniq:  # A-6 wrap
        connects += 1
    score += min(0.12, connects * 0.03)

    return max(0.0, min(1.0, score))


def _has_nut_flush_blocker(hole, board):
    """True if board has 3+ cards of suit S and we hold the Ace of suit S."""
    if len(board) < 3:
        return False
    b_suits = [c & 3 for c in board]
    s_counts = Counter(b_suits)
    for s, cnt in s_counts.items():
        if cnt >= 3:
            # Nut card in short deck for suit s is Ace of s: (ACE << 2) | s
            nut_card = (ACE << 2) | s
            if nut_card in hole:
                return True
    return False


def _mc_range_equity(hole, board, k, iters, rng):
    """Fast Monte Carlo against the best of k random opponent hands."""
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
        memory["rng"] = random.Random(42001)
        memory["spent"] = 0.0
        memory["opp_bets"] = 0
        memory["opp_checks"] = 0
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
                # Premium hands: 3-bet / raise pot
                if score >= 0.65 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                # Limp / complete small blind with playable hands
                if obs.to_call <= 1:
                    if score >= 0.22 or obs.to_call == 0:
                        return Action.CHECK_CALL
                    if Action.FOLD in legal:
                        return Action.FOLD
                    return Action.CHECK_CALL
                # Facing a raise in position:
                if score >= 0.44:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL
            else:
                # Big Blind
                if obs.to_call == 0:
                    if score >= 0.70 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                        return Action.RAISE_POT
                    return Action.CHECK_CALL
                # Facing an open raise:
                if score >= 0.72 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                if score >= 0.38:
                    return Action.CHECK_CALL
                if Action.FOLD in legal:
                    return Action.FOLD
                return Action.CHECK_CALL

        # 2. POSTFLOP
        # Opponent range parameter k
        if obs.to_call == 0:
            k = 1  # checked to: capped/unpolarized range
        else:
            k = min(4, 2 + max(0, obs.raises_this_street - 1))

        # Time pacing: sample budget
        spent = memory.get("spent", 0.0)
        if spent > 3.6:
            iters = 12
        elif spent > 2.8:
            iters = 24
        else:
            iters = 40

        t0 = time.perf_counter()
        eq = _mc_range_equity(hole, board, k, iters, rng)
        memory["spent"] = spent + (time.perf_counter() - t0)

        # A: Checked to (to_call == 0)
        if obs.to_call == 0:
            # Monster hand: polarized pot raise
            if eq >= 0.70 and Action.RAISE_POT in legal:
                return Action.RAISE_POT
            # Solid value hand: half-pot raise
            if eq >= 0.58 and Action.RAISE_HALF in legal:
                return Action.RAISE_HALF
            # Blocker bluff / semi-bluff with nut flush blocker
            if Action.RAISE_HALF in legal and _has_nut_flush_blocker(hole, board) and eq < 0.40:
                if rng.random() < 0.28:
                    return Action.RAISE_HALF
            # Positional stab on river if checked to twice
            if obs.street == Street.RIVER and Action.RAISE_HALF in legal and eq >= 0.45:
                if rng.random() < 0.20:
                    return Action.RAISE_HALF
            return Action.CHECK_CALL

        # B: Facing a bet (to_call > 0)
        price = obs.to_call / float(obs.pot + obs.to_call)
        # Monster re-raise / raise
        if eq >= 0.75 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
            return Action.RAISE_POT
        # Call if equity beats price with margin
        margin = 0.02
        if eq >= price + margin:
            return Action.CHECK_CALL
        if Action.FOLD in legal:
            return Action.FOLD
        return Action.CHECK_CALL

    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
