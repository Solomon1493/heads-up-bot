"""allin_random (family B): picks 30% of hands at random and raises pot at every chance
(which reaches all-in within a street or two); otherwise checks, and folds to any bet.
A pure bluff-shover: it ignores its cards."""
import random

from game import Action

SHOVE_SHARE = 0.30


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(4242)
    memory["hand"], memory["shoving"] = -1, False


def choose_action(obs, memory):
    try:
        if obs.hand_index != memory["hand"]:
            memory["hand"] = obs.hand_index
            memory["shoving"] = memory["rng"].random() < SHOVE_SHARE
        legal = obs.legal_actions
        if memory["shoving"]:
            for a in (Action.RAISE_POT, Action.RAISE_HALF):
                if a in legal:
                    return a
            return Action.CHECK_CALL
        return Action.CHECK_CALL if obs.to_call == 0 else Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
