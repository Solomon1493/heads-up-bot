"""allin_selective (family B): push-or-fold. With a top-25% hand preflop it raises pot at
every chance until all-in, and calls any bet; with anything else it checks when free and
folds to a bet. The classic short-stack shove strategy."""
from game import Action
from botlib import preflop

PUSH_TOP = 0.25


def new_half(config, memory):
    memory.clear()
    memory["hand"], memory["pushing"] = -1, False


def choose_action(obs, memory):
    try:
        if obs.hand_index != memory["hand"]:
            memory["hand"] = obs.hand_index
            memory["pushing"] = preflop.percentile(obs.hole_cards) >= 1.0 - PUSH_TOP
        legal = obs.legal_actions
        if memory["pushing"]:
            for a in (Action.RAISE_POT, Action.RAISE_HALF):
                if a in legal:
                    return a
            return Action.CHECK_CALL
        return Action.CHECK_CALL if obs.to_call == 0 else Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
