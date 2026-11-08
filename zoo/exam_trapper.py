"""exam_trapper (family C, held out; never traced or tuned on): slow-plays strong hands.
Checks and calls with its best hands on the flop and turn, then raises the river. Calls wide."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action, Street
from botlib.equity import mc_equity, preflop_features


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(5150)


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        if not obs.board:
            return Action.CHECK_CALL if preflop_features(obs.hole_cards) > 0.55 or obs.to_call == 0 else Action.FOLD
        eq = mc_equity(obs.hole_cards, obs.board, 24, memory["rng"])
        if obs.street == Street.RIVER and eq > 0.75 and Action.RAISE_POT in legal:
            return Action.RAISE_POT
        if obs.to_call == 0 or eq > 0.45:
            return Action.CHECK_CALL
        return Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
