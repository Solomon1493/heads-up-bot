"""exam_potodds (family C, held out): the bot most teams will write from the starter guide.
Monte Carlo equity against a random hand; preflop by simple features; call when equity beats
the pot odds; raise half pot above 65% equity, pot above 80%."""
import random

from game import Action
from botlib.equity import mc_equity


def new_half(config, memory):
    memory.clear()
    memory["rng"] = random.Random(2468)


def _preflop_ok(hole):
    ranks = [c >> 2 for c in hole]
    suits = [c & 3 for c in hole]
    pairs = len(ranks) - len(set(ranks))
    suited = max(suits.count(s) for s in set(suits))
    high = sum(r >= 6 for r in ranks)              # Q, K, A
    return pairs >= 1 or high >= 2 or suited >= 2 and high >= 1


def choose_action(obs, memory):
    try:
        legal = obs.legal_actions
        if not obs.board:
            if _preflop_ok(obs.hole_cards):
                if Action.RAISE_HALF in legal and obs.raises_this_street < 1:
                    return Action.RAISE_HALF
                return Action.CHECK_CALL
            return Action.CHECK_CALL if obs.to_call <= obs.big_blind // 2 or obs.to_call == 0 else Action.FOLD
        eq = mc_equity(obs.hole_cards, obs.board, 60, memory["rng"])
        if eq > 0.80 and Action.RAISE_POT in legal:
            return Action.RAISE_POT
        if eq > 0.65 and Action.RAISE_HALF in legal:
            return Action.RAISE_HALF
        if obs.to_call == 0:
            return Action.CHECK_CALL
        return Action.CHECK_CALL if eq >= obs.to_call / (obs.pot + obs.to_call) else Action.FOLD
    except Exception:
        return Action.CHECK_CALL


def on_hand_end(result, memory):
    pass
