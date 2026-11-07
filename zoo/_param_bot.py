"""zoo/_param_bot.py - one configurable bot that generates a family of different opponents.

Each zoo/gen_NN.py is this bot with a different seed. The seed draws a playing style:
  looseness   share of hands it plays preflop (by hand-strength percentile)
  aggression  how often it bets or raises when it has a strong hand
  value       the equity it calls "strong"
  bluff       how often it bets with a weak hand when checked to
  calldown    extra equity it demands over the price to call (negative = calls light)
  size        preferred raise: half pot, pot, or mixed
  shove       how often, with a very strong hand, it raises pot at every chance (goes all-in)
It estimates equity like a typical competition bot: Monte Carlo against a random hand.
The underscore keeps this file itself out of the opponent list.
"""
import random

from game import Action
from botlib.equity import mc_equity
from botlib import preflop

STYLE_KEYS = ("looseness", "aggression", "value", "bluff", "calldown", "size", "shove")


def draw_style(seed):
    rng = random.Random(1000 + seed)
    style = {
        "looseness": round(rng.uniform(0.15, 0.9), 2),
        "aggression": round(rng.uniform(0.1, 0.95), 2),
        "value": round(rng.uniform(0.55, 0.8), 2),
        "bluff": round(rng.uniform(0.0, 0.35), 2),
        "calldown": round(rng.uniform(-0.1, 0.15), 2),
        "size": rng.choice(["half", "pot", "mixed"]),
        "shove": round(rng.uniform(0.0, 0.3), 2),
    }
    if seed % 4 == 3:                 # every fourth generated bot is a stack-off (all-in) player
        style["shove"] = round(rng.uniform(0.6, 1.0), 2)
        style["looseness"] = max(style["looseness"], 0.5)
    return style


def make(seed):
    style = draw_style(seed)

    def new_half(config, memory):
        memory.clear()
        memory["rng"] = random.Random(seed * 7919 + 13)

    def _raise(legal, rng, big=False):
        if big or style["size"] == "pot" or (style["size"] == "mixed" and rng.random() < 0.5):
            order = (Action.RAISE_POT, Action.RAISE_HALF)
        else:
            order = (Action.RAISE_HALF, Action.RAISE_POT)
        return next((a for a in order if a in legal), None)

    def choose_action(obs, memory):
        try:
            rng, legal = memory["rng"], obs.legal_actions
            free = obs.to_call == 0
            if not obs.board:                                   # preflop: by hand percentile
                pct = preflop.percentile(obs.hole_cards)
                if pct >= 1.0 - style["looseness"] * 0.35 and rng.random() < style["shove"]:
                    a = _raise(legal, rng, big=True)            # premium hand: stack off
                    if a:
                        return a
                if pct < 1.0 - style["looseness"]:
                    return Action.CHECK_CALL if free else Action.FOLD
                if pct > 0.8 and rng.random() < style["aggression"]:
                    a = _raise(legal, rng)
                    if a:
                        return a
                return Action.CHECK_CALL
            eq = mc_equity(obs.hole_cards, obs.board, 30, rng)
            if eq > 0.8 and rng.random() < style["shove"]:
                a = _raise(legal, rng, big=True)
                if a:
                    return a
            if eq > style["value"] and rng.random() < style["aggression"]:
                a = _raise(legal, rng)
                if a:
                    return a
            if free:
                if rng.random() < style["bluff"]:
                    a = _raise(legal, rng)
                    if a:
                        return a
                return Action.CHECK_CALL
            pot_odds = obs.to_call / (obs.pot + obs.to_call)
            if eq >= pot_odds + style["calldown"]:
                return Action.CHECK_CALL
            return Action.FOLD
        except Exception:
            return Action.CHECK_CALL

    def on_hand_end(result, memory):
        pass

    return new_half, choose_action, on_hand_end, style
