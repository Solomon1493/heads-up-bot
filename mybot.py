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

from game import Action, Street
from botlib.equity import mc_equity, preflop_features

TIME_BUDGET = 4.0          # seconds of our own compute per half
EXPECTED_DECISIONS = 2600  # rough decisions per half, for pacing
MIN_ITERS, MAX_ITERS = 8, 120


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


def _strategy(obs, memory):
    legal = obs.legal_actions
    pot_odds = obs.to_call / (obs.pot + obs.to_call) if obs.to_call else 0.0

    if obs.street == Street.PREFLOP:
        s = preflop_features(obs.hole_cards)
        if s > 0.70 and Action.RAISE_POT in legal and obs.raises_this_street < 2:
            return Action.RAISE_POT
        if s > 0.45 or obs.to_call == 0:
            return Action.CHECK_CALL
        if obs.to_call <= obs.big_blind and s > 0.35:
            return Action.CHECK_CALL
        return Action.FOLD

    n = _iters(memory)
    t0 = time.perf_counter()
    eq = mc_equity(obs.hole_cards, obs.board, n, memory["rng"])
    if n:
        memory["sec_per_iter"] = 0.8 * memory["sec_per_iter"] + 0.2 * (time.perf_counter() - t0) / n

    fold_rate = _opp_fold_rate(memory)
    if eq > 0.72 and Action.RAISE_POT in legal:
        return Action.RAISE_POT
    if obs.to_call == 0 and Action.RAISE_HALF in legal:
        if eq > 0.60 or (fold_rate > 0.55 and eq > 0.30 and memory["rng"].random() < fold_rate):
            return Action.RAISE_HALF
        return Action.CHECK_CALL
    margin = 0.06 if _opp_aggression(memory) > 0.5 else 0.0   # aggressive opponents bluff more
    if obs.to_call == 0 or eq >= pot_odds - margin:
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
