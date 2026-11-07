"""mybot_v2.py - expected-value bot with a live Bayesian opponent model (see STRATEGY.md).

How a decision is made:
  1. Opponent model. Every opponent action this half updates per-street counts: how often
     they fold to a bet, bet when checked to, raise when facing a bet. Each rate is a
     posterior: the population prior counts as PARAMS["prior_strength"] observations, so
     early in a half we play the population average and later we play THIS opponent.
  2. Opponent range. Each action this hand narrows their range: a bet keeps their top
     (bet rate) of hands, a call drops their bottom (fold rate), and so on. A bluff share,
     predicted from their aggression, keeps some weak hands in the range.
  3. Range-weighted equity. Monte Carlo: deal the rest of the board and an opponent hand;
     weight each sample by how well that hand fits their range.
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

from game import Action, Street
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
# Per street: (fold to a bet, bet when checked to, raise when facing a bet).
PRIORS = {
    "PREFLOP": (0.174, 0.465, 0.419),
    "FLOP": (0.251, 0.474, 0.324),
    "TURN": (0.192, 0.504, 0.257),
    "RIVER": (0.234, 0.538, 0.164),
}
AGGRESSION_PRIOR = 0.432      # share of an opponent's postflop actions that are raises
BLUFF_A, BLUFF_K = 0.067, 0.362   # bluff share = A + K x aggression (fit R^2 = 0.91, 7 bots)

_PRIORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "botlib", "priors.json")
if os.path.exists(_PRIORS_FILE):      # written by the Databricks job; replaces the values above
    try:
        with open(_PRIORS_FILE) as _f:
            _p = json.load(_f)
        PRIORS = {s: tuple(v) for s, v in _p["streets"].items()}
        AGGRESSION_PRIOR = _p["aggression_prior"]
        BLUFF_A, BLUFF_K = _p["bluff_a"], _p["bluff_k"]
    except Exception:
        pass                          # keep the built-in values; never fail at import

# ===== Fixed constants (documented, not tuned) =====
REALIZE_IP, REALIZE_OOP = 1.0, 0.85   # share of equity realized before the river, by position
NEAR_EQUAL_BB = 1.0                   # "near-equal" actions for mixing: within 1 big blind
MIN_RANGE = 0.05                      # never model a range narrower than the top 5%
TIME_BUDGET = 4.0                     # our own compute per half (bank is 8 s)
EXPECTED_DECISIONS = 2600
MIN_ITERS, MAX_ITERS = 16, 150

# Test harness hook: tools/trace_v2.py sets this to a list to record every decision (the
# dashboard's model view). None in the tournament, so it costs nothing there.
TRACE = None

RAISES = (Action.RAISE_HALF, Action.RAISE_POT)
FOLD, BET, RAISE_F = 0, 1, 2          # indexes into PRIORS tuples


# ----------------------------------------------------------------------------- opponent model
def new_half(config, memory):
    try:
        memory.clear()
        memory.update(
            rng=random.Random(20261107), spent=0.0, sec_per_iter=6e-5, decisions=0,
            faced={s: 0 for s in PRIORS}, fold={s: 0 for s in PRIORS},
            raise_f={s: 0 for s in PRIORS}, checked={s: 0 for s in PRIORS},
            bet={s: 0 for s in PRIORS}, post_n=0, post_raise=0,
            hand=None,
        )
    except Exception:
        pass


def _rate(memory, kind, street):
    n0 = PARAMS["prior_strength"]
    prior = PRIORS[street][kind]
    if kind == FOLD:
        hits, n = memory["fold"][street], memory["faced"][street]
    elif kind == BET:
        hits, n = memory["bet"][street], memory["checked"][street]
    else:
        hits, n = memory["raise_f"][street], memory["faced"][street]
    return (hits + n0 * prior) / (n + n0)


def _bluff_share(memory):
    n0 = PARAMS["prior_strength"]
    aggr = (memory["post_raise"] + n0 * AGGRESSION_PRIOR) / (memory["post_n"] + n0)
    return min(0.6, max(0.02, BLUFF_A + BLUFF_K * aggr))


def _new_hand(obs):
    me_blind = obs.big_blind // 2 if obs.is_button else obs.big_blind
    opp_blind = obs.big_blind if obs.is_button else obs.big_blind // 2
    return {"index": obs.hand_index, "seen": 0, "street": "PREFLOP",
            "st": {"me": me_blind, "opp": opp_blind}, "range_top": 1.0, "pending": None}


def _sync(obs, memory):
    """Replay the actions we have not processed yet; update counts and the opponent's range."""
    h = memory["hand"]
    if h is None or h["index"] != obs.hand_index:
        h = memory["hand"] = _new_hand(obs)
    for a in obs.action_history[h["seen"]:]:
        street = a.street.name if hasattr(a.street, "name") else str(a.street)
        if street != h["street"]:
            h["street"], h["st"] = street, {"me": 0, "opp": 0}
        who = "me" if a.is_me else "opp"
        to_call = max(h["st"].values()) - h["st"][who]
        is_raise = a.action in RAISES
        if who == "opp":
            if to_call > 0:
                memory["faced"][street] += 1
                if a.action == Action.FOLD:
                    memory["fold"][street] += 1
                elif is_raise:
                    memory["raise_f"][street] += 1
                    h["range_top"] *= _rate(memory, RAISE_F, street)
                else:
                    h["range_top"] *= 1.0 - _rate(memory, FOLD, street)
            else:
                memory["checked"][street] += 1
                if is_raise:
                    memory["bet"][street] += 1
                    h["range_top"] *= _rate(memory, BET, street)
            if street != "PREFLOP":
                memory["post_n"] += 1
                memory["post_raise"] += is_raise
            h["pending"] = None
        elif is_raise:
            h["pending"] = street            # our bet; their answer may never reach us
        h["st"][who] += a.amount
    h["seen"] = len(obs.action_history)
    h["range_top"] = max(MIN_RANGE, h["range_top"])
    return h


# ----------------------------------------------------------------------------- equity
def _iters(memory):
    left = TIME_BUDGET - memory["spent"]
    remaining = max(200, EXPECTED_DECISIONS - memory["decisions"])
    n = int(max(0.0, left / remaining) / max(memory["sec_per_iter"], 1e-6))
    return max(MIN_ITERS if left > 0.3 else 0, min(MAX_ITERS, n))


def _samples(hole, board, iters, rng):
    """Monte Carlo samples: (outcome for us, opponent strength key)."""
    table = get_table()
    known = set(hole) | set(board)
    deck = [c for c in range(NUM_CARDS) if c not in known]
    need = 5 - len(board)
    board = list(board)
    my_hm = hole_masks(hole)
    # masks for the board as it is NOW (3 or 4 cards): rates the opponent's current hand.
    # (botlib's board_masks assumes a full 5-card board.)
    bits = [1 << c for c in board]
    cur_bm = [bits[i] | bits[j] | bits[k] for i, j, k in combinations(range(len(board)), 3)] \
        if len(board) >= 3 else None
    out = []
    for _ in range(iters):
        draw = rng.sample(deck, need + 5)
        opp = draw[need:]
        bm = board_masks(board + draw[:need]) if need else board_masks(board)
        opp_hm = hole_masks(opp)
        me, them = score_masks(my_hm, bm, table), score_masks(opp_hm, bm, table)
        outcome = 1.0 if me > them else 0.5 if me == them else 0.0
        key = score_masks(opp_hm, cur_bm, table) if cur_bm else preflop.percentile(opp)
        out.append((outcome, key))
    return out


def _equity(samples, range_top, bluff):
    """Equity against a range = the opponent's top `range_top` of hands plus a bluff share."""
    if not samples:
        return 0.5
    keys = sorted(k for _, k in samples)
    n = len(keys)
    t = 1.0 - range_top                                   # fraction of hands below the range
    if t <= 0.0:
        return sum(o for o, _ in samples) / n
    beta = bluff * (1.0 - t) / (t * (1.0 - bluff)) if bluff > 0 else 0.0
    num = den = 0.0
    for o, k in samples:
        w = 1.0 if bisect.bisect_left(keys, k) / n >= t else beta
        num += w * o
        den += w
    return num / den if den else sum(o for o, _ in samples) / n


# ----------------------------------------------------------------------------- decision
def _evs(obs, memory, h, samples):
    street = h["street"] if obs.board else "PREFLOP"
    street = {0: "PREFLOP", 3: "FLOP", 4: "TURN", 5: "RIVER"}.get(len(obs.board), street)
    bluff = _bluff_share(memory)
    eq = _equity(samples, h["range_top"], bluff)
    river = len(obs.board) == 5
    realize = REALIZE_IP if obs.is_button else REALIZE_OOP
    pot, legal = obs.pot, obs.legal_actions
    evs = {}
    if Action.FOLD in legal:
        evs[Action.FOLD] = 0.0
    if obs.to_call == 0:
        evs[Action.CHECK_CALL] = (1.0 if river else realize) * eq * pot
    else:
        c = min(obs.to_call, obs.my_stack)
        all_in = c >= obs.my_stack or obs.opp_stack == 0
        evs[Action.CHECK_CALL] = (1.0 if all_in or river else realize) * eq * (pot + c) - c
    p_fold = _rate(memory, FOLD, street)
    call_range = max(MIN_RANGE, h["range_top"] * (1.0 - p_fold))
    eq_called = _equity(samples, call_range, 0.0)          # hands that call are not bluffs
    for a in RAISES:
        if a not in legal:
            continue
        x = obs.raise_to[a]
        ours = x - obs.my_bet
        theirs = min(x - obs.opp_bet, obs.opp_stack)
        all_in = ours >= obs.my_stack or theirs >= obs.opp_stack
        r = 1.0 if all_in or river else realize
        evs[a] = p_fold * pot + (1.0 - p_fold) * (r * eq_called * (pot + ours + theirs) - ours)
    return evs


def _choose(evs, obs, memory):
    best = max(evs, key=evs.get)
    if PARAMS["mix_eps"] > 0 and memory["rng"].random() < PARAMS["mix_eps"]:
        near = [a for a, v in evs.items() if a != best and v >= evs[best] - NEAR_EQUAL_BB * obs.big_blind]
        if near:
            return memory["rng"].choice(near)
    return best


def _strategy(obs, memory):
    h = _sync(obs, memory)
    n = _iters(memory)
    if n == 0:                                             # time bank nearly gone: cheapest safe play
        return Action.CHECK_CALL if obs.to_call <= obs.big_blind else Action.FOLD
    t0 = time.perf_counter()
    samples = _samples(obs.hole_cards, obs.board, n, memory["rng"])
    memory["sec_per_iter"] = 0.8 * memory["sec_per_iter"] + 0.2 * (time.perf_counter() - t0) / n
    evs = _evs(obs, memory, h, samples)
    action = _choose(evs, obs, memory)
    if action in RAISES:
        h["pending"] = h["street"] if obs.board else "PREFLOP"
    if TRACE is not None:
        _trace(obs, memory, h, samples, evs, action, n)
    return action


def _trace(obs, memory, h, samples, evs, action, n):
    street = {0: "PREFLOP", 3: "FLOP", 4: "TURN", 5: "RIVER"}[len(obs.board)]
    bluff = _bluff_share(memory)
    TRACE.append({
        "hand": obs.hand_index, "street": street, "is_button": obs.is_button,
        "hole": cards_to_str(obs.hole_cards), "board": cards_to_str(obs.board),
        "pot": obs.pot, "to_call": obs.to_call, "iters": n,
        "equity_vs_random": round(_equity(samples, 1.0, 0.0), 4),
        "range_top": round(h["range_top"], 4), "bluff_share": round(bluff, 4),
        "equity_vs_range": round(_equity(samples, h["range_top"], bluff), 4),
        "posterior": {s: [round(_rate(memory, k, s), 4) for k in (FOLD, BET, RAISE_F)] for s in PRIORS},
        "observations": {s: memory["faced"][s] + memory["checked"][s] for s in PRIORS},
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
    """Our last bet may have been answered without us seeing it: infer fold or call."""
    try:
        h = memory.get("hand")
        if h and h["index"] == result.hand_index and h["pending"]:
            s = h["pending"]
            memory["faced"][s] += 1
            folded = not result.went_to_showdown and result.winner == "me"
            if folded:
                memory["fold"][s] += 1
            if s != "PREFLOP":
                memory["post_n"] += 1
            h["pending"] = None
    except Exception:
        pass
