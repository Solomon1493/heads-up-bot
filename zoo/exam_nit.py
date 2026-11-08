"""exam_nit (family C, held out; never traced or tuned on): plays only premium starting hands,
then gets the money in with pot raises when it has a strong hand. Folds everything else."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action
from botlib.equity import mc_equity, preflop_features


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(9091)


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        if not obs.board:
            s = preflop_features(obs.hole_cards)
            if s > 0.85 and Action.RAISE_POT in legal:
                return Action.RAISE_POT
            if s > 0.79 or obs.to_call == 0:
                return Action.CHECK_CALL
            return Action.FOLD
        eq = mc_equity(obs.hole_cards, obs.board, 24, memory["rng"])
        if eq > 0.70 and Action.RAISE_POT in legal:
            return Action.RAISE_POT
        if obs.to_call == 0 or eq > 0.62:
            return Action.CHECK_CALL
        return Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
