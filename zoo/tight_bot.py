"""Tight-passive: plays strong starting hands only, rarely raises."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action, Street
from botlib.equity import mc_equity, preflop_features, equity_policy


def new_half(config, memory):
    memory["rng"] = random.Random(2)


def choose_action(obs, memory):
    legal = obs.legal_actions
    if obs.street == Street.PREFLOP:
        s = preflop_features(obs.hole_cards)
        if s > 0.75 and Action.RAISE_HALF in legal and obs.raises_this_street == 0:
            return Action.RAISE_HALF
        if s > 0.55 or obs.to_call == 0:
            return Action.CHECK_CALL
        return Action.FOLD
    eq = mc_equity(obs.hole_cards, obs.board, 20, memory["rng"])
    if eq > 0.80 and Action.RAISE_HALF in legal:
        return Action.RAISE_HALF
    if obs.to_call == 0 or eq > 0.58:
        return Action.CHECK_CALL
    return Action.FOLD


def on_hand_end(result, memory):
    pass
