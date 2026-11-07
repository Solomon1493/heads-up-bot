"""allin_postflop (family C, held out): calls cheaply preflop, then stacks off after the flop.
With equity above 60% it raises pot at every chance until all-in; with less it checks, and
calls only when the price is right. Tests how we handle a bot that shoves made hands."""
import random

from game import Action
from botlib.equity import mc_equity


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(777)


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        if not obs.board:
            if obs.to_call <= 2 * obs.big_blind:
                return Action.CHECK_CALL
            return Action.FOLD if Action.FOLD in legal else Action.CHECK_CALL
        eq = mc_equity(obs.hole_cards, obs.board, 40, memory["rng"])
        if eq > 0.60:
            for a in (Action.RAISE_POT, Action.RAISE_HALF):
                if a in legal:
                    return a
            return Action.CHECK_CALL
        if obs.to_call == 0:
            return Action.CHECK_CALL
        return Action.CHECK_CALL if eq >= obs.to_call / (obs.pot + obs.to_call) else Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
