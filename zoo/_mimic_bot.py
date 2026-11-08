"""zoo/_mimic_bot.py - families of bots that play like rangeaware_mc and exam_barreler.

One bot of a kind proves little, so each archetype is a family with randomized parameters:

  ra:N      range-aware value bettor (like rangeaware_mc): equity against the best of k random
            hands (k 2-4, higher after more betting), bets for value (pot or half), bluffs rarely,
            calls only with enough equity against that range.
  barrel:N  barreler (like exam_barreler): bets nearly every street when checked to (mostly half
            pot), keeps firing, gives up to a raise unless it has a real hand.

Use ra:0-29 / barrel:0-29 to find leaks and ra:100-129 / barrel:100-129 to check a fix on
opponents it was not built on. Same seed = same bot. The underscore keeps this file out of the
opponent list.
"""
import random

from game import Action, Street
from botlib import preflop
from botlib.equity import mc_equity
from zoo._swarm_bot import _bestk


def draw_ra(seed):
    rng = random.Random(55_000 + seed)
    return {"kind": "ra", "k": rng.choice([2, 2, 3, 3, 4]), "looseness": round(rng.uniform(0.25, 0.7), 2),
            "value": round(rng.uniform(0.52, 0.65), 2), "size": rng.choice(["pot", "pot", "half"]),
            "bluff": round(rng.uniform(0.0, 0.12), 2), "margin": round(rng.uniform(0.0, 0.08), 2)}


def draw_barrel(seed):
    rng = random.Random(66_000 + seed)
    return {"kind": "barrel", "fire": round(rng.uniform(0.7, 1.0), 2), "looseness": round(rng.uniform(0.4, 0.9), 2),
            "size": rng.choice(["half", "half", "half", "pot"]), "giveup": round(rng.uniform(0.45, 0.65), 2),
            "pf_raise": round(rng.uniform(0.5, 1.0), 2)}


def describe(st):
    if st["kind"] == "ra":
        return (f"range-aware value bettor: best of {st['k']}, plays {st['looseness']:.0%}, bets {st['size']} "
                f"above {st['value']:.0%}, bluffs {st['bluff']:.0%}")
    return (f"barreler: bets {st['fire']:.0%} of unbet streets ({st['size']} pot), plays {st['looseness']:.0%}, "
            f"continues vs a raise above {st['giveup']:.0%} equity")


def _bet(legal, size):
    order = (Action.RAISE_POT, Action.RAISE_HALF) if size == "pot" else (Action.RAISE_HALF, Action.RAISE_POT)
    return next((a for a in order if a in legal), None)


def make(kind, seed):
    st = draw_ra(seed) if kind == "ra" else draw_barrel(seed)

    def new_half(config, memory):
        memory.clear()
        memory["rng"] = random.Random(seed * 7_919 + (1 if kind == "ra" else 2))

    def choose_action(obs, memory):
        try:
            rng, legal, free = memory["rng"], obs.legal_actions, obs.to_call == 0
            if not obs.board:
                pct = preflop.percentile(obs.hole_cards)
                if pct < 1.0 - st["looseness"]:
                    return Action.CHECK_CALL if free else Action.FOLD
                raise_p = st.get("pf_raise", 0.5) if kind == "barrel" else (0.6 if pct > 0.8 else 0.1)
                if obs.raises_this_street < 2 and rng.random() < raise_p:
                    a = _bet(legal, st["size"])
                    if a:
                        return a
                return Action.CHECK_CALL
            if kind == "ra":
                k = min(5, st["k"] + max(0, obs.raises_this_street - 1)) if not free else st["k"]
                eq = _bestk(obs.hole_cards, obs.board, k, 16, rng)
                if eq > st["value"]:
                    a = _bet(legal, st["size"])
                    if a and (free or eq > st["value"] + 0.1):
                        return a
                if free:
                    if rng.random() < st["bluff"]:
                        a = _bet(legal, "half")
                        if a:
                            return a
                    return Action.CHECK_CALL
                return Action.CHECK_CALL if eq >= obs.to_call / (obs.pot + obs.to_call) + st["margin"] else Action.FOLD
            # barreler
            if free:
                if rng.random() < st["fire"]:
                    a = _bet(legal, st["size"])
                    if a:
                        return a
                return Action.CHECK_CALL
            eq = mc_equity(obs.hole_cards, obs.board, 24, rng)
            if eq > 0.8 and obs.raises_this_street < 3:
                a = _bet(legal, "pot")
                if a:
                    return a
            return Action.CHECK_CALL if eq >= st["giveup"] else Action.FOLD
        except Exception:
            return Action.CHECK_CALL

    def on_hand_end(result, memory):
        pass

    return new_half, choose_action, on_hand_end, st
