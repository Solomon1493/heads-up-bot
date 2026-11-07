"""Loose-aggressive: raises often, bets when checked to, calls wide."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action, Street
from botlib.equity import mc_equity, preflop_features, equity_policy


def new_half(config, memory):
    memory["rng"] = random.Random(3)


def choose_action(obs, memory):
    legal, rng = obs.legal_actions, memory["rng"]
    if obs.street == Street.PREFLOP:
        s = preflop_features(obs.hole_cards)
        if s > 0.35 and Action.RAISE_POT in legal and rng.random() < 0.7:
            return Action.RAISE_POT
        if s > 0.25 or obs.to_call == 0:
            return Action.CHECK_CALL
        return Action.FOLD
    eq = mc_equity(obs.hole_cards, obs.board, 20, rng)
    if eq > 0.60 and Action.RAISE_POT in legal:
        return Action.RAISE_POT
    if obs.to_call == 0 and Action.RAISE_HALF in legal and rng.random() < 0.6:
        return Action.RAISE_HALF
    pot_odds = obs.to_call / (obs.pot + obs.to_call) if obs.to_call else 0.0
    if obs.to_call == 0 or eq >= pot_odds - 0.05:
        return Action.CHECK_CALL
    return Action.FOLD


def on_hand_end(result, memory):
    pass
