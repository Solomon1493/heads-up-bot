"""mybot_v2.py - expected-value bot with a live Bayesian opponent model (see STRATEGY.md).

How a decision is made:
  1. Opponent model. Every opponent action this half updates per-street counts: how often
     they fold to a bet, fold to a RE-raise (after they bet), bet when checked to, and raise
     when facing a bet. Each rate is a posterior: the population prior counts as
     PARAMS["prior_strength"] observations, so early in a half we play the population average
     and later we play THIS opponent.
  2. Opponent range, as a band of hand-strength percentiles. Every action is a slice:
     a bet keeps the top of their range, a check keeps the rest, a call keeps the middle,
     a raise keeps the top of the betting range. A small share of hands outside the band stays
     possible: bluffs after aggression (predicted from how aggressive they are), slow-plays
     after a check.
  3. Range-weighted equity. Monte Carlo: deal the rest of the board, score our hand once, and
     test it against several opponent hands, each weighted by how well it fits the band.
     Samples are reused for later decisions in the same betting round.
  4. Expected value of every legal action, in chips from now on (fold = 0). Pick the best,
     except that with probability PARAMS["mix_eps"] pick another action within 1 bb of it.

Only two numbers are tuned (PARAMS). Everything else is a population estimate (PRIORS,
BLUFF_*) or a documented fixed constant. Never raises out of these functions.
"""
import bisect
import json
import os
import random
import time
from itertools import combinations

from game import Action
from evaluator import NUM_CARDS, cards_to_str, get_table
from botlib.equity import board_masks, hole_masks, score_masks
from botlib import preflop

# ===== THE ONLY TUNED KNOBS =====
PARAMS = {
    "prior_strength": 20.0,   # pseudo-observations behind each population prior
    "mix_eps": 0.03,          # chance of picking a near-equal action, to stay unreadable
}

# ===== Population estimates (provisional; Databricks refits them, see STRATEGY.md) =====
# From the 2026-11-07 export: 8 opponents x 2 seeds x 1000 deals, each opponent weighted equally.
# Per street: (fold to a bet, bet when checked to, raise when facing a bet, fold to a re-raise).
PRIORS = {
    "PREFLOP": (0.174, 0.465, 0.419, 0.071),
    "FLOP": (0.251, 0.474, 0.324, 0.131),
    "TURN": (0.192, 0.504, 0.257, 0.107),
    "RIVER": (0.234, 0.538, 0.164, 0.178),
}
AGGRESSION_PRIOR = 0.432      # share of an opponent's postflop actions that are raises
BLUFF_A, BLUFF_K = 0.067, 0.362   # bluff share = A + K x aggression (fit R^2 = 0.91, 7 bots)

_PRIORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "botlib", "priors.json")
if os.path.exists(_PRIORS_FILE):      # written by the Databricks job; replaces the values above
    try:
        with open(_PRIORS_FILE) as _f:
            _p = json.load(_f)
        PRIORS = {s: tuple(v) + PRIORS[s][len(v):] for s, v in _p["streets"].items()}
        AGGRESSION_PRIOR = _p["aggression_prior"]
        BLUFF_A, BLUFF_K = _p["bluff_a"], _p["bluff_k"]
    except Exception:
        pass                          # keep the built-in values; never fail at import

# ===== Fixed constants (documented, not tuned) =====
KEY_MODE = "blend"                  # how to rank opponent hands: current / final / blend
TRAP_SHARE = 0.05                     # share of a checking range that is a slow-played big hand
NEAR_EQUAL_BB = 1.0                   # "near-equal" actions for mixing: within 1 big blind
MIN_WIDTH = 0.05                      # never model a range narrower than 5% of hands
OPPS_PER_BOARD = 4                    # opponent hands tested per simulated board
TIME_BUDGET = 4.0                     # our own compute per half (bank is 8 s; same margin as v1)
EXPECTED_DECISIONS = 2600
PAIRS = 64                            # (board, opponent hand) samples per decision
MIN_PAIRS = 16                        # floor when running behind the time budget

# Test harness hook: tools/trace_v2.py sets this to a list to record every decision (the
# dashboard's model view). None in the tournament, so it costs nothing there.
TRACE = None

RAISES = (Action.RAISE_HALF, Action.RAISE_POT)
FOLD, BET, RAISE_F, FOLD_RR = 0, 1, 2, 3   # indexes into PRIORS tuples
STREET_OF = {0: "PREFLOP", 3: "FLOP", 4: "TURN", 5: "RIVER"}


# ----------------------------------------------------------------------------- opponent model
def new_half(config, memory):
    try:
        memory.clear()
        z = lambda: {s: 0 for s in PRIORS}
        memory.update(
            rng=random.Random(20261107), spent=0.0, sec_per_pair=4e-5, decisions=0,
            faced=z(), fold=z(), raise_f=z(), checked=z(), bet=z(), faced_rr=z(), fold_rr=z(),
            post_n=0, post_raise=0, hand=None, cache=None,
        )
        get_table()                    # one-time setup here, not inside the first decision
        preflop.percentile([0, 5, 10, 15, 20])
    except Exception:
        pass


def _rate(memory, kind, street):
    n0 = PARAMS["prior_strength"]
    prior = PRIORS[street][kind]
    hits, n = {
        FOLD: (memory["fold"], memory["faced"]),
        BET: (memory["bet"], memory["checked"]),
        RAISE_F: (memory["raise_f"], memory["faced"]),
        FOLD_RR: (memory["fold_rr"], memory["faced_rr"]),
    }[kind]
    return (hits[street] + n0 * prior) / (n[street] + n0)


def _bluff_share(memory):
    n0 = PARAMS["prior_strength"]
    aggr = (memory["post_raise"] + n0 * AGGRESSION_PRIOR) / (memory["post_n"] + n0)
    return min(0.6, max(0.02, BLUFF_A + BLUFF_K * aggr))


def _new_hand(obs):
    me_blind = obs.big_blind // 2 if obs.is_button else obs.big_blind
    opp_blind = obs.big_blind if obs.is_button else obs.big_blind // 2
    return {"index": obs.hand_index, "seen": 0, "street": "PREFLOP",
            "st": {"me": me_blind, "opp": opp_blind},
            "lo": 0.0, "hi": 1.0, "out": 0.0,       # opponent range band and outside share
            "opp_raised": False, "pending": None}


def _clamp(h):
    if h["hi"] - h["lo"] < MIN_WIDTH:
        mid = (h["lo"] + h["hi"]) / 2
        h["lo"] = max(0.0, min(1.0 - MIN_WIDTH, mid - MIN_WIDTH / 2))
        h["hi"] = h["lo"] + MIN_WIDTH


def _sync(obs, memory):
    """Replay unprocessed actions: update the opponent's rates and slice their range."""
    h = memory["hand"]
    if h is None or h["index"] != obs.hand_index:
        h = memory["hand"] = _new_hand(obs)
    for a in obs.action_history[h["seen"]:]:
        street = a.street.name if hasattr(a.street, "name") else str(a.street)
        if street != h["street"]:
            h["street"], h["st"], h["opp_raised"] = street, {"me": 0, "opp": 0}, False
        who = "me" if a.is_me else "opp"
        to_call = max(h["st"].values()) - h["st"][who]
        is_raise = a.action in RAISES
        if who == "opp":
            w = h["hi"] - h["lo"]
            if to_call > 0:
                rr = h["opp_raised"]               # they bet earlier this round: facing our re-raise
                if rr:
                    memory["faced_rr"][street] += 1
                    memory["fold_rr"][street] += a.action == Action.FOLD
                else:
                    memory["faced"][street] += 1
                    memory["fold"][street] += a.action == Action.FOLD
                    memory["raise_f"][street] += is_raise
                if is_raise:                       # top of their range
                    h["lo"] = h["hi"] - _rate(memory, RAISE_F, street) * w
                    h["out"], h["opp_raised"] = _bluff_share(memory), True
                elif a.action != Action.FOLD:      # call: drop the folds below and the raises above
                    h["lo"] += _rate(memory, FOLD_RR if rr else FOLD, street) * w
                    h["hi"] -= _rate(memory, RAISE_F, street) * w
            else:
                memory["checked"][street] += 1
                memory["bet"][street] += is_raise
                b = _rate(memory, BET, street)
                if is_raise:                       # bet: top of their range
                    h["lo"] = h["hi"] - b * w
                    h["out"], h["opp_raised"] = _bluff_share(memory), True
                else:                              # check: everything below their betting range
                    h["hi"] = h["lo"] + (1.0 - b) * w
                    h["out"] = TRAP_SHARE
            _clamp(h)
            if street != "PREFLOP":
                memory["post_n"] += 1
                memory["post_raise"] += is_raise
            h["pending"] = None
        elif is_raise:
            h["pending"] = (street, h["opp_raised"])   # their answer may never reach us
        h["st"][who] += a.amount
    h["seen"] = len(obs.action_history)
    return h


# ----------------------------------------------------------------------------- equity
def _budget_pairs(memory):
    """A fixed sample count, so the same settings always play the same way (that is what
    lets tune.py compare versions deal by deal). Only if we fall behind the time budget,
    e.g. on a slow machine, does it scale down to fit."""
    left = TIME_BUDGET - memory["spent"]
    if left <= 0.3:
        return 0
    allowed = TIME_BUDGET * min(1.0, (memory["decisions"] + 200) / EXPECTED_DECISIONS)
    if memory["spent"] <= allowed:
        return PAIRS
    n = int(left / max(200, EXPECTED_DECISIONS - memory["decisions"]) / max(memory["sec_per_pair"], 1e-6))
    return max(MIN_PAIRS, min(PAIRS, n))


def _samples(hole, board, pairs, rng, need_keys):
    """(outcome for us, opponent strength key) per sample: each simulated board is scored
    for us once and tested against OPPS_PER_BOARD opponent hands."""
    table = get_table()
    known = set(hole) | set(board)
    deck = [c for c in range(NUM_CARDS) if c not in known]
    need = 5 - len(board)
    board = list(board)
    my_hm = hole_masks(hole)
    bits = [1 << c for c in board]
    cur_bm = [bits[i] | bits[j] | bits[k] for i, j, k in combinations(range(len(board)), 3)] \
        if len(board) >= 3 else None
    k = OPPS_PER_BOARD
    out = []
    for _ in range(max(1, pairs // k)):
        draw = rng.sample(deck, need + 5 * k)
        bm = board_masks(board + draw[:need]) if need else board_masks(board)
        me = score_masks(my_hm, bm, table)
        for j in range(k):
            opp = draw[need + 5 * j: need + 5 * j + 5]
            ohm = hole_masks(opp)
            them = score_masks(ohm, bm, table)
            o = 1.0 if me > them else 0.5 if me == them else 0.0
            if not need_keys:
                out.append((o, 0, 0))
                continue
            now = score_masks(ohm, cur_bm, table) if cur_bm else preflop.percentile(opp)
            out.append((o, now, them))      # their hand now, and once the board completes
    return out


def _equity(samples, lo, hi, out):
    """Equity against a range band [lo, hi] of strength percentiles, plus `out` share outside it."""
    if not samples:
        return 0.5
    n = len(samples)
    if lo <= 0.0 and hi >= 1.0:
        return sum(x[0] for x in samples) / n
    w = hi - lo
    beta = out * w / ((1.0 - w) * (1.0 - out)) if 0 < out < 1 and w < 1 else 0.0
    now_sorted = sorted(x[1] for x in samples)
    fin_sorted = sorted(x[2] for x in samples)
    num = den = 0.0
    for o, now, fin in samples:
        p_now = bisect.bisect_left(now_sorted, now) / n
        p_fin = bisect.bisect_left(fin_sorted, fin) / n
        p = {"current": p_now, "final": p_fin}.get(KEY_MODE, (p_now + p_fin) / 2)
        wt = 1.0 if lo <= p <= hi else beta
        num += wt * o
        den += wt
    return num / den if den else sum(x[0] for x in samples) / n


def _get_samples(obs, memory, h):
    need_keys = not (h["lo"] <= 0.0 and h["hi"] >= 1.0)
    key = (obs.hand_index, len(obs.board))
    c = memory["cache"]
    if c and c["key"] == key and (c["keys"] or not need_keys):
        return c["samples"], 0                           # same betting round: reuse
    n = _budget_pairs(memory)
    if n == 0:
        return None, 0
    t0 = time.perf_counter()
    # Common random numbers: the same spot always gets the same samples, whatever happened
    # earlier, so two parameter settings are compared on identical simulations.
    spot = obs.hand_index * 1000003 + len(obs.board) * 7919 + sum(c << (i % 16) for i, c in enumerate(obs.hole_cards))
    samples = _samples(obs.hole_cards, obs.board, n, random.Random(spot ^ 0x5EED), need_keys)
    memory["sec_per_pair"] = 0.8 * memory["sec_per_pair"] + 0.2 * (time.perf_counter() - t0) / len(samples)
    memory["cache"] = {"key": key, "samples": samples, "keys": need_keys}
    return samples, len(samples)


# ----------------------------------------------------------------------------- decision
def _evs(obs, memory, h, samples):
    street = STREET_OF[len(obs.board)]
    eq = _equity(samples, h["lo"], h["hi"], h["out"])
    pot, legal = obs.pot, obs.legal_actions
    evs = {}
    if Action.FOLD in legal:
        evs[Action.FOLD] = 0.0
    if obs.to_call == 0:
        evs[Action.CHECK_CALL] = eq * pot
    else:
        c = min(obs.to_call, obs.my_stack)
        evs[Action.CHECK_CALL] = eq * (pot + c) - c
    p_fold = _rate(memory, FOLD_RR if h["opp_raised"] else FOLD, street)
    w = h["hi"] - h["lo"]
    eq_called = _equity(samples, min(h["hi"] - MIN_WIDTH, h["lo"] + p_fold * w), h["hi"], 0.0)
    for a in RAISES:
        if a not in legal:
            continue
        x = obs.raise_to[a]
        ours = x - obs.my_bet
        theirs = min(x - obs.opp_bet, obs.opp_stack)
        evs[a] = p_fold * pot + (1.0 - p_fold) * (eq_called * (pot + ours + theirs) - ours)
    return evs, eq, p_fold


def _choose(evs, obs, memory):
    best = max(evs, key=evs.get)
    if PARAMS["mix_eps"] > 0 and memory["rng"].random() < PARAMS["mix_eps"]:
        near = [a for a, v in evs.items() if a != best and v >= evs[best] - NEAR_EQUAL_BB * obs.big_blind]
        if near:
            return memory["rng"].choice(near)
    return best


def _strategy(obs, memory):
    h = _sync(obs, memory)
    samples, fresh = _get_samples(obs, memory, h)
    if samples is None:                                  # time bank nearly gone: cheapest safe play
        return Action.CHECK_CALL if obs.to_call <= obs.big_blind else Action.FOLD
    evs, eq, p_fold = _evs(obs, memory, h, samples)
    action = _choose(evs, obs, memory)
    if action in RAISES:
        h["pending"] = (STREET_OF[len(obs.board)], h["opp_raised"])
    if TRACE is not None:
        _trace(obs, memory, h, samples, evs, action, eq, p_fold, fresh)
    return action


def _trace(obs, memory, h, samples, evs, action, eq, p_fold, fresh):
    TRACE.append({
        "hand": obs.hand_index, "street": STREET_OF[len(obs.board)], "is_button": obs.is_button,
        "hole": cards_to_str(obs.hole_cards), "board": cards_to_str(obs.board),
        "pot": obs.pot, "to_call": obs.to_call, "iters": len(samples), "reused": fresh == 0,
        "equity_vs_random": round(_equity(samples, 0.0, 1.0, 0.0), 4),
        "range_lo": round(h["lo"], 4), "range_hi": round(h["hi"], 4), "range_top": round(h["hi"] - h["lo"], 4),
        "outside_share": round(h["out"], 4), "bluff_share": round(_bluff_share(memory), 4),
        "equity_vs_range": round(eq, 4), "p_fold_used": round(p_fold, 4),
        "posterior": {s: [round(_rate(memory, k, s), 4) for k in (FOLD, BET, RAISE_F, FOLD_RR)] for s in PRIORS},
        "observations": {s: memory["faced"][s] + memory["checked"][s] + memory["faced_rr"][s] for s in PRIORS},
        "evs": {a.name: round(v, 3) for a, v in evs.items()}, "action": action.name,
    })


def choose_action(obs, memory):
    t0 = time.perf_counter()
    try:
        action = _strategy(obs, memory)
        if action not in obs.legal_actions:
            action = Action.CHECK_CALL
    except Exception as exc:
        if TRACE is not None:
            import traceback
            TRACE.append({"hand": obs.hand_index, "error": f"{type(exc).__name__}: {exc}",
                          "where": traceback.format_exc()[-600:]})
        action = Action.CHECK_CALL                         # always legal
    try:
        memory["decisions"] += 1
        memory["spent"] += time.perf_counter() - t0
    except Exception:
        pass
    return action


def on_hand_end(result, memory):
    """Our last raise may have been answered without us seeing it: infer fold or call."""
    try:
        h = memory.get("hand")
        if h and h["index"] == result.hand_index and h["pending"]:
            street, rr = h["pending"]
            folded = not result.went_to_showdown and result.winner == "me"
            if rr:
                memory["faced_rr"][street] += 1
                memory["fold_rr"][street] += folded
            else:
                memory["faced"][street] += 1
                memory["fold"][street] += folded
            if street != "PREFLOP":
                memory["post_n"] += 1
            h["pending"] = None
    except Exception:
        pass
