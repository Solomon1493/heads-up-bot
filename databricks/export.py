"""databricks/export.py - play arena matches and flatten every hand into tables.

Runs the hero bot against each opponent with the official judge code (via fastarena),
keeps the judge's full hand logs, and writes three JSON-lines tables:

    matches.jsonl    one row per match: result, confidence interval, verdict, time used
    hands.jsonl      one row per hand: cards, board, result, who would have won a showdown
    decisions.jsonl  one row per action: the situation (pot, to-call, street) and, in
                     hindsight, the actor's exact equity against the opponent's REAL cards

The hindsight equity is what makes leak-finding possible: the judge logs both players'
hole cards and the full board for every hand, so for each flop/turn/river decision we
enumerate every possible runout and compute the true chance the actor was ahead.
Preflop equity is left empty here (65,780 runouts per decision is too slow on a laptop);
the Databricks job computes it.

Usage (from the repo root):
    .venv/bin/python databricks/export.py --seeds 2 --deals 1000
    .venv/bin/python databricks/export.py --opponents equity_bot,tight_bot --seeds 4
Output: data/databricks/<run_id>/  (data/ is git-ignored). Then run databricks/sync.py.
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fastarena  # noqa: E402
from botlib.equity import board_masks, hole_masks, score_masks  # noqa: E402
from evaluator import NUM_CARDS, category_name, get_table, omaha_score, str_to_card  # noqa: E402
from game import AgentConfig  # noqa: E402

CFG = AgentConfig()
STACK, BB = CFG.starting_stack, CFG.big_blind
STREET_BOARD = {"PREFLOP": 0, "FLOP": 3, "TURN": 4, "RIVER": 5}
RAISES = ("RAISE_HALF", "RAISE_POT")


def exact_equity(me, opp, board_known):
    """Chance `me` beats `opp` over every possible completion of the board (ties = half)."""
    dead = set(me) | set(opp) | set(board_known)
    remaining = [c for c in range(NUM_CARDS) if c not in dead]
    need = 5 - len(board_known)
    table = get_table()
    hm1, hm2 = hole_masks(me), hole_masks(opp)
    total, n = 0.0, 0
    for extra in combinations(remaining, need):
        bm = board_masks(list(board_known) + list(extra))
        a, b = score_masks(hm1, bm, table), score_masks(hm2, bm, table)
        total += 1.0 if a > b else 0.5 if a == b else 0.0
        n += 1
    return total / n


def _export_match(job):
    run_id, hero, opp, seed, deals = job
    match_id = f"{run_id}:{opp}:{seed}"
    # If the hero exposes a TRACE hook (mybot_v2), record its own estimates for calibration.
    trace, orig = [], fastarena.load_module
    hero_path = os.path.abspath(fastarena.resolve(hero))

    def _load(path):
        mod = orig(path)
        if os.path.abspath(path) == hero_path and hasattr(mod, "TRACE"):
            mod.TRACE = trace
        return mod
    fastarena.load_module = _load
    try:
        res, _ = fastarena.play(hero, opp, seed, deals, keep_logs=True)
    finally:
        fastarena.load_module = orig
    trace = [t for t in trace if "error" not in t]
    t_i = 0
    stats = res.agent_stats
    match = {
        "run_id": run_id, "match_id": match_id, "hero": hero, "opponent": opp, "seed": seed,
        "deals": deals, "mean_bb_per_deal": res.mean_bb, "ci_low": res.ci[0], "ci_high": res.ci[1],
        "verdict": res.verdict, "hero_points": res.points["agent1"],
        "hero_think_seconds_max": max(s.time_seconds for s in stats["agent1"]),
        "hero_errors": sum(s.errors for s in stats["agent1"]),
        "hero_forfeit": any(s.forfeited for s in stats["agent1"]),
    }
    hands, decisions, mismatches = [], [], 0
    for half_key, logs in res.logs.items():
        half = int(half_key[-1])
        hero_seat = 0 if half == 1 else 1          # judge: agent1 is seat 0 in half 1, seat 1 in half 2
        for h in logs:
            hand_id = f"{match_id}:{half}:{h['deal_index']}"
            btn = h["button_seat"]
            holes = [[str_to_card(c) for c in h["hole_cards"][s]] for s in (0, 1)]
            board = [str_to_card(c) for c in h["board"]]
            final_scores = [omaha_score(holes[s], board) for s in (0, 1)]
            contrib = [0, 0]
            contrib[btn], contrib[1 - btn] = BB // 2, BB          # button posts the small blind
            street_tot = list(contrib)
            street, raises = "PREFLOP", 0
            eq_cache = {}
            for i, a in enumerate(h["actions"]):
                if a["street"] != street:
                    street, street_tot, raises = a["street"], [0, 0], 0
                s = a["seat"]
                to_call = max(street_tot) - street_tot[s]
                pot_before = sum(contrib)
                key = (s, street)
                if key not in eq_cache:
                    nb = STREET_BOARD[street]
                    eq_cache[key] = None if nb == 0 else exact_equity(holes[s], holes[1 - s], board[:nb])
                model = {}
                if s == hero_seat and t_i < len(trace):
                    t = trace[t_i]; t_i += 1
                    model = {"model_equity": t.get("equity_vs_range"), "model_equity_random": t.get("equity_vs_random"),
                             "model_range_top": t.get("range_top"), "model_iters": t.get("iters"),
                             "model_p_fold": t.get("posterior", {}).get(street, [None])[0],
                             "model_action": t.get("action"), "model_street": t.get("street")}
                decisions.append({**model, 
                    "run_id": run_id, "match_id": match_id, "hand_id": hand_id,
                    "hero": hero, "opponent": opp, "half": half, "deal_index": h["deal_index"],
                    "seq": i, "actor": "hero" if s == hero_seat else "opponent",
                    "actor_is_button": s == btn, "street": street, "action": a["action"],
                    "amount": a["amount"], "to_call": to_call, "pot_before": pot_before,
                    "stack_before": STACK - contrib[s], "raises_before": raises,
                    "pot_odds": round(to_call / (pot_before + to_call), 4) if to_call else 0.0,
                    "hindsight_equity": None if eq_cache[key] is None else round(eq_cache[key], 4),
                })
                contrib[s] += a["amount"]
                street_tot[s] += a["amount"]
                if street_tot[s] != a["street_total"]:
                    mismatches += 1
                if a["action"] in RAISES:
                    raises += 1
            ho = 1 - hero_seat
            sd = final_scores[hero_seat] - final_scores[ho]
            hands.append({
                "run_id": run_id, "match_id": match_id, "hand_id": hand_id, "hero": hero,
                "opponent": opp, "seed": seed, "half": half, "deal_index": h["deal_index"],
                "hero_is_button": hero_seat == btn, "hero_net_chips": h["nets"][hero_seat],
                "hero_net_bb": h["nets"][hero_seat] / BB, "pot": h["pot"],
                "went_to_showdown": h["went_to_showdown"],
                "final_street": {0: "PREFLOP", 3: "FLOP", 4: "TURN", 5: "RIVER"}[len(h["board_revealed"])],
                "hero_hole": " ".join(h["hole_cards"][hero_seat]),
                "opp_hole": " ".join(h["hole_cards"][ho]), "board": " ".join(h["board"]),
                "hero_would_win_showdown": 1.0 if sd > 0 else 0.5 if sd == 0 else 0.0,
                "hero_final_hand": category_name(final_scores[hero_seat]),
                "opp_final_hand": category_name(final_scores[ho]),
                "n_actions": len(h["actions"]),
            })
    return match, hands, decisions, mismatches


def _run_id(hero):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                      stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        sha = "nogit"
    path = fastarena.resolve(hero)
    code = hashlib.sha256(open(path, "rb").read()).hexdigest()[:8]
    return f"{stamp}_{sha}_{code}"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hero", default="mybot")
    p.add_argument("--opponents", help="comma-separated; default: every zoo and baseline bot")
    p.add_argument("--seeds", type=int, default=2)
    p.add_argument("--deals", type=int, default=1000)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = p.parse_args(argv)

    opps = a.opponents.split(",") if a.opponents else [o for o in fastarena.zoo_specs() if o != a.hero]
    run_id = _run_id(a.hero)
    out = os.path.join(ROOT, "data", "databricks", run_id)
    os.makedirs(out)
    jobs = [(run_id, a.hero, o, f"export-{o}-{s}", a.deals) for o in opps for s in range(a.seeds)]
    print(f"run {run_id}: {len(jobs)} matches ({a.hero} vs {len(opps)} opponents x {a.seeds} seeds, "
          f"{a.deals} deals per half), {a.workers} workers")

    t0 = time.time()
    counts = {"matches": 0, "hands": 0, "decisions": 0}
    mismatches = 0
    files = {k: open(os.path.join(out, f"{k}.jsonl"), "w") for k in counts}
    try:
        from tools.runlock import run_lock
        with run_lock(owner=os.environ.get("RUN_OWNER", "export.py (terminal)"), tag=f"export {a.hero}"), \
             ProcessPoolExecutor(max_workers=min(a.workers, len(jobs))) as ex:
            for match, hands, decisions, mm in ex.map(_export_match, jobs):
                mismatches += mm
                for name, rows in (("matches", [match]), ("hands", hands), ("decisions", decisions)):
                    for r in rows:
                        files[name].write(json.dumps(r) + "\n")
                    counts[name] += len(rows)
                print(f"  {match['opponent']:16} seed {match['seed']:>16}: "
                      f"{match['mean_bb_per_deal']:+.3f} bb/deal ({match['verdict']})")
    finally:
        for f in files.values():
            f.close()
    if mismatches:
        print(f"WARNING: {mismatches} actions where the replayed bet total disagreed with the judge's log")
    print(f"wrote {counts} to {out} in {time.time() - t0:.0f}s")
    print(f"next: .venv/bin/python databricks/sync.py --run {run_id}")


if __name__ == "__main__":
    main()
