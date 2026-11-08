"""exam_barreler (family C, held out; never traced or tuned on): bets every street when checked
to, whatever it holds, and keeps betting unless raised. Gives up to a raise without a real hand."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action
from botlib.equity import mc_equity, preflop_features


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(7373)


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        if not obs.board:
            s = preflop_features(obs.hole_cards)
            if obs.raises_this_street == 0 and Action.RAISE_HALF in legal:
                return Action.RAISE_HALF
            return Action.CHECK_CALL if s > 0.60 or obs.to_call == 0 else Action.FOLD
        if obs.to_call == 0 and Action.RAISE_HALF in legal:
            return Action.RAISE_HALF
        eq = mc_equity(obs.hole_cards, obs.board, 20, memory["rng"])
        return Action.CHECK_CALL if eq > 0.50 else Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
