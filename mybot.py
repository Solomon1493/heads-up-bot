"""mybot.py - the team's bot. Keep strategy here; agent.py only delegates to it.

Wire it into agent.py (inside the YOUR CODE HERE block):
    import mybot
    def new_half(config, memory):      mybot.new_half(config, memory)
    def choose_action(obs, memory):    return mybot.choose_action(obs, memory)
    def on_hand_end(result, memory):   mybot.on_hand_end(result, memory)

Design rules
  1. Never raise an exception out of these functions, never return an illegal action.
  2. Spend at most TIME_BUDGET seconds per half (bank is 8 s; HTTP overhead and a
     slower tournament machine eat the rest).
"""
import random
import time

from collections import Counter
from game import Action, Street
from botlib.equity import mc_equity, preflop_features

TIME_BUDGET = 4.0          # seconds of our own compute per half
EXPECTED_DECISIONS = 2600  # rough decisions per half, for pacing
MIN_ITERS, MAX_ITERS = 8, 120

# ===== TUNABLE PARAMETERS =====
# tune.py reads this dict and can perturb it during sweeps.
DEFAULT_PARAMS = {
    "pf_btn_raise":        0.58,   # button: preflop strength to raise pot
    "pf_btn_limp":         0.18,   # button: complete the small blind (1 chip) above this
    "pf_btn_call_raise":   0.42,   # button: call a big-blind re-raise above this
    "pf_bb_raise":         0.68,   # big blind: preflop strength to raise pot
    "pf_bb_call":          0.40,   # big blind: call a button raise above this
    "eq_raise_pot":        0.70,   # postflop equity to raise pot
    "eq_raise_half":       0.58,   # postflop equity to raise half (when checked to)
    "eq_bluff_fold_rate":  0.48,   # opponent fold-rate to attempt bluff
    "eq_bluff_min":        0.28,   # minimum equity to attempt a bluff
    "aggro_margin":        0.08,   # pot-odds discount vs aggressive opponents
    "aggro_threshold":     0.48,   # opponent aggression above this triggers margin
    "tight_threshold":     0.38,   # opponent aggression below this triggers tight exploit
    "tight_call_penalty":  0.20,   # extra equity over pot odds required to call a tight bot
    "tight_probe_min_eq":  0.25,   # minimum equity to probe bet when checked to by tight bot
}
PARAMS = dict(DEFAULT_PARAMS)  # active copy; the test harness (fastarena.apply_params)
                               # overrides it in-process. The shipped bot uses the defaults.


def new_half(config, memory):
    try:
        memory.update(
            rng=random.Random(12345), spent=0.0, sec_per_iter=1e-4,
            decisions=0, hands=0,
            opp_raise=0, opp_call=0,          # opponent aggression
            we_bet_hands=0, opp_folds=0,      # how often they fold to our bets
            raised_this_hand=False, seen=(-1, 0),
        )
    except Exception:
        pass


def _iters(memory):
    """Monte Carlo samples we can afford for this decision, paced across the half."""
    left = TIME_BUDGET - memory["spent"]
    remaining = max(200, EXPECTED_DECISIONS - memory["decisions"])
    per_decision = max(0.0, left / remaining)
    n = int(per_decision / max(memory["sec_per_iter"], 1e-6))
    return max(MIN_ITERS if left > 0.3 else 0, min(MAX_ITERS, n))


def _track_opponent(obs, memory):
    """Count opponent actions we have not seen yet in this hand."""
    hand, count = memory["seen"]
    if hand != obs.hand_index:
        count = 0
        memory["raised_this_hand"] = False
    for a in obs.action_history[count:]:
        if not a.is_me:
            if a.action in (Action.RAISE_HALF, Action.RAISE_POT):
                memory["opp_raise"] += 1
            elif a.action == Action.CHECK_CALL and a.amount > 0:
                memory["opp_call"] += 1
    memory["seen"] = (obs.hand_index, len(obs.action_history))


def _opp_fold_rate(memory):
    return (memory["opp_folds"] + 0.4 * 10) / (memory["we_bet_hands"] + 10)   # shrunk toward 40%


def _opp_aggression(memory):
    return (memory["opp_raise"] + 3) / (memory["opp_raise"] + memory["opp_call"] + 10)


def sd_plo5_preflop(hole, is_button=False):
    """Accurate Short Deck 5-Card PLO preflop strength (0.0 .. 1.0).
    Accounts for sets beating straights, flushes beating full houses,
    penalizes unplayable trips/quads in hand, and factors in button position.
    """
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    r_counts = Counter(ranks)
    s_counts = Counter(suits)

    # 1. High cards (0..8)
    score = (sum(ranks) / 40.0) * 0.38

    # 2. Pairs (Sets beat straights in Short Deck)
    pairs = [r for r, count in r_counts.items() if count == 2]
    if r_counts[8] == 2:      # Pocket Aces
        score += 0.30
    elif r_counts[7] == 2:    # Pocket Kings
        score += 0.20
    elif r_counts[6] == 2:    # Pocket Queens
        score += 0.14
    elif len(pairs) >= 1:
        score += 0.08

    if len(pairs) >= 2:       # Two pair (AAKK, etc.)
        score += 0.14

    # 3. Blocker penalties (Trips or quads in hole)
    for count in r_counts.values():
        if count == 3:
            score -= 0.16     # blocks sets, unplayable 3rd card
        elif count >= 4:
            score -= 0.35     # dead hand in PLO

    # 4. Suitedness (Flushes beat Full Houses in Short Deck!)
    ace_suits = {c & 3 for c in hole if (c >> 2) == 8}
    for s in ace_suits:
        if s_counts[s] == 2:
            score += 0.12     # Nut flush draw potential

    two_card_suits = sum(1 for count in s_counts.values() if count == 2)
    if two_card_suits >= 2:
        score += 0.12         # Double suited
    elif two_card_suits == 1 and not ace_suits:
        score += 0.05

    for count in s_counts.values():
        if count == 3:
            score -= 0.06     # blocks flush outs
        elif count >= 4:
            score -= 0.16

    # 5. Connectedness
    sorted_unique = sorted(set(ranks))
    connects = sum(1 for a, b in zip(sorted_unique, sorted_unique[1:]) if b - a == 1)
    if 8 in sorted_unique and 0 in sorted_unique:  # Short Deck wheel: A-6
        connects += 1
    score += min(0.12, connects * 0.03)

    # 6. Button positional advantage
    if is_button:
        score += 0.07

    return max(0.05, min(0.98, score))


def _strategy(obs, memory):
    P = PARAMS
    legal = obs.legal_actions
    pot_odds = obs.to_call / (obs.pot + obs.to_call) if obs.to_call else 0.0
    opp_af = _opp_aggression(memory)
    fold_rate = _opp_fold_rate(memory)

    # Preflop round
    if obs.street == Street.PREFLOP:
        s = sd_plo5_preflop(obs.hole_cards, obs.is_button)
        if obs.is_button:
            # ON THE BUTTON: We have postflop position!
            # Steal / value raise with top hands
            if s > P["pf_btn_raise"] and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                return Action.RAISE_POT
            # It only costs 1 chip to call on the button (getting 3:1 pot odds + position).
            # Never fold playable hands! Only fold total unplayable trash.
            if obs.to_call <= 1:
                if s > P["pf_btn_limp"]:
                    return Action.CHECK_CALL
                return Action.FOLD
            # Facing a 3-bet / raise from big blind:
            if s > P["pf_btn_call_raise"]:
                return Action.CHECK_CALL
            return Action.FOLD
        else:
            # IN THE BIG BLIND:
            # If button just completed/limped (to_call == 0):
            if obs.to_call == 0:
                if s > P["pf_bb_raise"] and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                    return Action.RAISE_POT
                return Action.CHECK_CALL
            # Facing a button raise (out of position):
            if s > P["pf_bb_raise"] and Action.RAISE_POT in legal and obs.raises_this_street < 2:
                return Action.RAISE_POT
            if s > P["pf_bb_call"]:
                return Action.CHECK_CALL
            return Action.FOLD

    # Postflop rounds (Flop, Turn, River)
    n = _iters(memory)
    t0 = time.perf_counter()
    eq = mc_equity(obs.hole_cards, obs.board, n, memory["rng"])
    if n:
        memory["sec_per_iter"] = 0.8 * memory["sec_per_iter"] + 0.2 * (time.perf_counter() - t0) / n

    # Did opponent show strength this hand (raised flop or turn)?
    opp_raised_previously = any(not a.is_me and a.action in (Action.RAISE_HALF, Action.RAISE_POT) for a in obs.action_history)

    # SCENARIO A: Free check or opening bet (to_call == 0)
    if obs.to_call == 0:
        # Monster value bet
        if eq > P["eq_raise_pot"] and Action.RAISE_POT in legal:
            return Action.RAISE_POT
        # Standard value bet
        if eq > P["eq_raise_half"] and Action.RAISE_HALF in legal:
            return Action.RAISE_HALF
        # Exploit passive/tight opponents who fold when checked to
        # BUT: do not probe if opponent already showed strength (raised earlier), and never probe-bluff river!
        if Action.RAISE_HALF in legal and not opp_raised_previously:
            if obs.street in (Street.FLOP, Street.TURN):
                if (opp_af < P["tight_threshold"] or fold_rate > 0.45) and eq > P["tight_probe_min_eq"]:
                    return Action.RAISE_HALF
            if fold_rate > P["eq_bluff_fold_rate"] and eq > P["eq_bluff_min"] and memory["rng"].random() < fold_rate:
                return Action.RAISE_HALF
        return Action.CHECK_CALL

    # SCENARIO B: Facing a bet or raise (to_call > 0)
    # 1. Re-raising: ONLY with true monster equity!
    if Action.RAISE_POT in legal or Action.RAISE_HALF in legal:
        reraise_min_eq = 0.92 if (opp_af < P["tight_threshold"] or obs.street == Street.RIVER) else 0.84
        if eq > reraise_min_eq and obs.raises_this_street < 2:
            if Action.RAISE_POT in legal:
                return Action.RAISE_POT
            if Action.RAISE_HALF in legal:
                return Action.RAISE_HALF

    # 2. Decision to Call vs Fold
    # Pot commitment check: if we already invested >=35 chips and the call is an all-in,
    # folding is mathematically catastrophic if equity > pot_odds - 0.05!
    already_invested = (obs.starting_stack - obs.my_stack) if hasattr(obs, 'starting_stack') else (80 - obs.my_stack)
    is_pot_committed = already_invested >= 35 or obs.my_stack <= obs.to_call

    if opp_af < P["tight_threshold"]:
        # Tight/passive opponent: their bets are monster hands (never pay them off with marginal equity)
        if obs.street == Street.RIVER and opp_raised_previously and not is_pot_committed:
            req_eq = max(0.65, pot_odds + 0.25)
        else:
            req_eq = max(0.58, pot_odds + P["tight_call_penalty"])
    elif opp_af > P["aggro_threshold"]:
        # Maniac / aggressive opponent: lots of bluffs, call down wider!
        # If pot-committed against maniac, NEVER fold if we have any decent shot!
        if is_pot_committed:
            req_eq = max(0.14, pot_odds - 0.10)
        else:
            req_eq = max(0.18, pot_odds - P["aggro_margin"])
    else:
        req_eq = pot_odds

    if eq >= req_eq:
        return Action.CHECK_CALL
    return Action.FOLD


def choose_action(obs, memory):
    t0 = time.perf_counter()
    try:
        _track_opponent(obs, memory)
        action = _strategy(obs, memory)
        if action in (Action.RAISE_HALF, Action.RAISE_POT):
            memory["raised_this_hand"] = True
        if action not in obs.legal_actions:
            action = Action.CHECK_CALL
    except Exception:
        action = Action.CHECK_CALL          # always legal
    try:
        memory["decisions"] = memory.get("decisions", 0) + 1
        memory["spent"] = memory.get("spent", 0.0) + time.perf_counter() - t0
    except Exception:
        pass
    return action


def on_hand_end(result, memory):
    try:
        memory["hands"] += 1
        if memory.get("raised_this_hand"):
            memory["we_bet_hands"] += 1
            if not result.went_to_showdown and result.winner == "me":
                memory["opp_folds"] += 1    # they can only fold when facing a bet
        memory["raised_this_hand"] = False
    except Exception:
        pass
