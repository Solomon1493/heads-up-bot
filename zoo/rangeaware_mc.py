"""
rangeaware_mc (family C, held out): an outside test opponent, written by another Claude
session as a typical competition bot. Fixed strategy, never tuned on. Only change from the
original poker_ai.py: a seeded random generator (reset each half), so matches replay
identically and same-deals comparisons stay valid.

poker_ai.py - the brain for a Short Deck 5-card PLO heads-up bot.

Design, grounded in measurements on this exact game engine:

  * Equity by Monte Carlo (deal unknown cards, count wins with omaha_score).
  * RANGE-AWARE opponent: instead of one random opponent hand, deal `k`
    candidate hands and take the STRONGEST. Max-of-k random draws sits around
    the top 1/(k+1) of hands, so k is a clean "how tight is the opponent" dial.
    k=1 is a random opponent; bigger k models a tighter / more aggressive one.
  * EV decision: compare range-aware equity to the pot-odds price.
  * Mixed strategy: a small, fixed bluff frequency so we are not exploitable.
  * Adaptive sample counts tied to a per-half time budget, so we never blow
    the 8-second bank (measured: ~3-4 ms/flop decision at 40-50 samples).
  * Everything is wrapped so a bug can never crash the bot (5 errors = forfeit).

Imported by agent.py. Pure standard library + evaluator.omaha_score.
"""

import random
import time

from evaluator import omaha_score, warmup

warmup()   # as its agent.py does at startup: build the lookup table before the first decision
from game import Action, Street

DECK = list(range(36))
_RNG = random.Random(31337)
_sample = lambda pop, k: _RNG.sample(pop, k)

# ------------------------------------------------------------------ knobs ---
# Time budget (seconds of wall-clock compute per half). The real bank is 8s;
# we aim well under it so a slower tournament machine still finishes. We track
# cumulative time and dial sample counts down in stages.
BANK_TARGET      = 5.0
BANK_TIER2       = 3.0     # past this used, halve the work
BANK_TIER3       = 4.3     # past this, go minimal

SAMP_FLOP_FULL   = 48      # postflop samples when we have time
SAMP_PRE_FULL    = 28      # preflop samples (cheaper decision, matters less)
PER_DECISION_CAP = 0.012   # hard wall-clock cap per decision (s); << 1s limit

# Decision thresholds on RANGE-AWARE equity.
RAISE_POT_EQ   = 0.66      # value raise, pot size
RAISE_HALF_EQ  = 0.55      # value raise, half size
CALL_MARGIN    = 0.015     # cushion over raw pot-odds for estimation error
BLUFF_FREQ     = 0.12      # how often to turn a weak hand into a half-pot bet
SEMIBLUFF_MAXEQ= 0.35      # only bluff genuinely weak hands (not medium ones)


# ------------------------------------------------------------- equity core --
def estimate_equity(hole_cards, board, samples, opp_k, deadline=None):
    """Range-aware win probability (ties = 0.5).

    hole_cards : our 5 cards (list of ints)
    board      : 0, 3, 4 or 5 community cards (list of ints)
    samples    : number of Monte Carlo runouts
    opp_k      : model opponent as the best of this many random hands
    deadline   : optional perf_counter() time to stop early (safety)
    """
    hole = list(hole_cards)
    board = list(board)
    seen = set(hole) | set(board)
    unknown = [c for c in DECK if c not in seen]
    need = 5 - len(board)

    wins = 0.0
    n = 0
    check_every = 8
    while n < samples:
        drawn = _sample(unknown, need) if need else []
        full_board = board + drawn
        # pool for opponent = everything not in our hand or the full board
        fb_set = set(full_board)
        pool = [c for c in unknown if c not in fb_set]

        my = omaha_score(hole, full_board)

        best_opp = 0
        for _ in range(opp_k):
            opp = _sample(pool, 5)
            s = omaha_score(opp, full_board)
            if s > best_opp:
                best_opp = s

        wins += 1.0 if my > best_opp else (0.5 if my == best_opp else 0.0)
        n += 1

        if deadline is not None and (n % check_every == 0) \
           and time.perf_counter() >= deadline:
            break

    return wins / n if n else 0.5


# ----------------------------------------------------- opponent model + k ---
def _update_opp_model(obs, memory):
    """Count the opponent's voluntary actions, each exactly once, by tracking
    how far into this hand's history we have already looked."""
    if memory.get("cur_hand") != obs.hand_index:
        memory["cur_hand"] = obs.hand_index
        memory["hist_seen"] = 0
    hist = obs.action_history
    seen = memory.get("hist_seen", 0)
    for rec in hist[seen:]:
        if not rec.is_me:
            memory["opp_acts"] = memory.get("opp_acts", 0) + 1
            if rec.action in (Action.RAISE_HALF, Action.RAISE_POT):
                memory["opp_raises"] = memory.get("opp_raises", 0) + 1
    memory["hist_seen"] = len(hist)


def _opp_k(obs, memory):
    """How tight to assume the opponent is, as a max-of-k value.

    Base 1 (random). Each voluntary raise this street means they are not here
    with junk, so bump k. A learned aggression term nudges it from long-run
    behaviour (how often they bet/raise overall)."""
    acts = memory.get("opp_acts", 0)
    raises = memory.get("opp_raises", 0)
    raise_rate = raises / acts if acts >= 40 else None

    # A hyper-aggressive opponent raises with everything, so their raises
    # carry almost no information: do NOT let raises_this_street tighten our
    # read of them. (Confirmed maniac.)
    if raise_rate is not None and raise_rate > 0.50:
        return 1

    k = 1 + min(obs.raises_this_street, 3)
    if raise_rate is not None and obs.to_call > 0:
        if raise_rate < 0.12:          # a passive opp putting chips in = strong
            k += 2
        elif raise_rate < 0.25:
            k += 1
    return min(k, 5)


# --------------------------------------------------------- time management --
def _samples_for(obs, memory):
    """Pick sample count from street and remaining time budget."""
    used = memory.get("t_used", 0.0)
    preflop = (obs.street == Street.PREFLOP)
    base = SAMP_PRE_FULL if preflop else SAMP_FLOP_FULL
    if used > BANK_TIER3:
        return max(6, base // 4)
    if used > BANK_TIER2:
        return max(10, base // 2)
    return base


# ------------------------------------------------------------- the decision --
def _decide(obs, memory):
    _update_opp_model(obs, memory)
    legal = obs.legal_actions
    can_pot = Action.RAISE_POT in legal
    can_half = Action.RAISE_HALF in legal

    samples = _samples_for(obs, memory)
    k = _opp_k(obs, memory)

    t0 = time.perf_counter()
    deadline = t0 + PER_DECISION_CAP
    eq = estimate_equity(obs.hole_cards, obs.board, samples, k, deadline)
    memory["t_used"] = memory.get("t_used", 0.0) + (time.perf_counter() - t0)

    # ---- value raising (same whether or not we currently owe chips) --------
    if eq >= RAISE_POT_EQ and can_pot:
        return Action.RAISE_POT
    if eq >= RAISE_HALF_EQ and can_half:
        return Action.RAISE_HALF

    # ---- nothing owed: we may check for free ------------------------------
    if obs.to_call <= 0:
        # Balanced bluff: occasionally bet a genuinely weak hand so our checks
        # are not always weak. Only with a real hand-improving story (weak now,
        # but position/fold-equity). Keep it rare and only half-pot.
        if can_half and eq < SEMIBLUFF_MAXEQ and _RNG.random() < BLUFF_FREQ:
            return Action.RAISE_HALF
        return Action.CHECK_CALL

    # ---- facing a bet: pot-odds call or fold ------------------------------
    price = obs.to_call / float(obs.pot + obs.to_call)
    if eq >= price + CALL_MARGIN:
        return Action.CHECK_CALL
    if Action.FOLD in legal:
        return Action.FOLD
    return Action.CHECK_CALL


# -------------------------------------------------------------- public API --
def new_half(config, memory):
    _RNG.seed(31337)
    memory["t_used"] = 0.0
    memory["opp_acts"] = 0
    memory["opp_raises"] = 0


def choose_action(obs, memory):
    try:
        action = _decide(obs, memory)
        if action in obs.legal_actions:
            return action
    except Exception:
        pass
    # Safe fallback: free check, else fold, else the first legal action.
    if Action.CHECK_CALL in obs.legal_actions:
        return Action.CHECK_CALL
    if Action.FOLD in obs.legal_actions:
        return Action.FOLD
    return obs.legal_actions[0]


def on_hand_end(result, memory):
    # Learn opponent aggression from the hand we just saw. action_history is
    # not on HandResult, so we approximate from what we can see safely.
    # (We update counts from the live obs inside _decide instead; see note.)
    pass
