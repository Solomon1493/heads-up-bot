"""zoo/_swarm_bot.py - random opponent styles on demand, for robustness tests ("swarm").

`fastarena.py swarm <bot> --n 80 --base 0` plays <bot> against swarm:0 .. swarm:79. Each seed
draws one style from a wider space than zoo/_param_bot.py (whose gen_NN bots stay unchanged):

  looseness    share of hands it plays preflop
  pf_raise     how often it raises a playable hand preflop
  aggression   how often it bets or raises a strong hand
  value        the equity it calls "strong"
  bluff        how often it bets a weak hand when checked to
  calldown     equity it demands over the price to call (negative = calls light)
  value_size   bet size with strong hands; bluff_size: bet size with bluffs (a size tell if they differ)
  trap         how often it checks a strong hand on the flop or turn (slow-play), to raise later
  shove        how often it stacks off with a very strong hand
  k            equity model: 1 = vs a random hand (typical bot), 2-3 = vs the best of k hands
               (range-aware, like rangeaware_mc)

Same seed = same style and same random choices, so two versions of a bot face identical
opponents. The underscore keeps this file out of the opponent list.
"""
import random

from game import Action, Street
from botlib import preflop
from botlib.equity import board_masks, hole_masks, mc_equity, score_masks
from evaluator import NUM_CARDS, get_table


def draw_style(seed):
    rng = random.Random(77_000 + seed)
    style = {
        "looseness": round(rng.uniform(0.1, 0.95), 2),
        "pf_raise": round(rng.uniform(0.0, 0.7), 2),
        "aggression": round(rng.uniform(0.05, 0.95), 2),
        "value": round(rng.uniform(0.5, 0.85), 2),
        "bluff": round(rng.uniform(0.0, 0.5), 2),
        "calldown": round(rng.uniform(-0.15, 0.2), 2),
        "value_size": rng.choice(["half", "pot"]),
        "bluff_size": rng.choice(["half", "pot"]),
        "trap": round(rng.choice([0.0, 0.0, rng.uniform(0.2, 0.7)]), 2),
        "shove": round(rng.uniform(0.0, 0.35), 2),
        "k": rng.choice([1, 1, 1, 2, 3]),
    }
    if rng.random() < 0.15:                      # some stack-off players
        style["shove"] = round(rng.uniform(0.6, 1.0), 2)
    return style


def describe(style):
    """One plain line for the dashboard."""
    bits = [f"plays {style['looseness']:.0%} of hands", f"bets strong hands {style['aggression']:.0%}",
            f"bluffs {style['bluff']:.0%}", "calls light" if style["calldown"] < 0 else "calls tight"]
    if style["value_size"] != style["bluff_size"]:
        bits.append(f"value {style['value_size']} / bluff {style['bluff_size']} (size tell)")
    if style["trap"]:
        bits.append(f"slow-plays {style['trap']:.0%}")
    if style["k"] > 1:
        bits.append(f"range-aware (best of {style['k']})")
    if style["shove"] >= 0.6:
        bits.append("stacks off")
    return ", ".join(bits)


def _bestk(hole, board, k, iters, rng):
    table = get_table()
    deck = [c for c in range(NUM_CARDS) if c not in set(hole) | set(board)]
    need, my_hm, total = 5 - len(board), hole_masks(hole), 0.0
    for _ in range(iters):
        draw = rng.sample(deck, need + 5 * k)
        bm = board_masks(list(board) + draw[:need])
        me = score_masks(my_hm, bm, table)
        opp = max(score_masks(hole_masks(draw[need + 5 * j:need + 5 * j + 5]), bm, table) for j in range(k))
        total += 1.0 if me > opp else 0.5 if me == opp else 0.0
    return total / iters


def make(seed):
    style = draw_style(seed)

    def new_half(config, memory):
        memory.clear()
        memory["rng"] = random.Random(seed * 104_729 + 7)

    def _bet(legal, size):
        order = (Action.RAISE_POT, Action.RAISE_HALF) if size == "pot" else (Action.RAISE_HALF, Action.RAISE_POT)
        return next((a for a in order if a in legal), None)

    def choose_action(obs, memory):
        try:
            rng, legal, free = memory["rng"], obs.legal_actions, obs.to_call == 0
            if not obs.board:
                pct = preflop.percentile(obs.hole_cards)
                if pct >= 0.9 and rng.random() < style["shove"]:
                    a = _bet(legal, "pot")
                    if a:
                        return a
                if pct < 1.0 - style["looseness"]:
                    return Action.CHECK_CALL if free else Action.FOLD
                if obs.raises_this_street < 2 and rng.random() < style["pf_raise"] * pct:
                    a = _bet(legal, style["value_size"])
                    if a:
                        return a
                return Action.CHECK_CALL
            eq = (mc_equity(obs.hole_cards, obs.board, 30, rng) if style["k"] == 1
                  else _bestk(obs.hole_cards, obs.board, style["k"], 16, rng))
            if eq > 0.85 and rng.random() < style["shove"]:
                a = _bet(legal, "pot")
                if a:
                    return a
            if eq > style["value"]:
                if free and obs.street != Street.RIVER and rng.random() < style["trap"]:
                    return Action.CHECK_CALL                    # slow-play now, raise later
                if rng.random() < style["aggression"]:
                    a = _bet(legal, style["value_size"])
                    if a:
                        return a
            if free:
                if rng.random() < style["bluff"]:
                    a = _bet(legal, style["bluff_size"])
                    if a:
                        return a
                return Action.CHECK_CALL
            if eq >= obs.to_call / (obs.pot + obs.to_call) + style["calldown"]:
                return Action.CHECK_CALL
            return Action.FOLD
        except Exception:
            return Action.CHECK_CALL

    def on_hand_end(result, memory):
        pass

    return new_half, choose_action, on_hand_end, style
