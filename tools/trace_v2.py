"""tools/trace_v2.py - record every mybot_v2 decision for the dashboard's model view.

Plays mybot_v2 against each opponent with the official judge code (via fastarena) and saves,
for every decision: the opponent posteriors per street, the modeled range, equity against a
random hand vs against that range, the expected value of each action, and the choice.
Exits with an error if any decision raised an exception (v2 would have silently checked).

    .venv/bin/python tools/trace_v2.py                       # default opponents, 300 deals
    .venv/bin/python tools/trace_v2.py --opponents equity_bot,station --deals 1000
Output: data/trace/latest.json (and a timestamped copy).
"""
import argparse
import datetime
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fastarena  # noqa: E402

OUT_DIR = os.path.join(ROOT, "data", "trace")


def _trace_match(job):
    bot, opp, seed, deals = job
    trace = []
    orig = fastarena.load_module

    def load(path):
        mod = orig(path)
        if os.path.abspath(path) == os.path.abspath(fastarena.resolve(bot)):
            mod.TRACE = trace                    # same list across both halves
        return mod

    fastarena.load_module = load
    try:
        res, _ = fastarena.play(bot, opp, seed, deals)
    finally:
        fastarena.load_module = orig
    half_len = sum(1 for t in trace if t.get("hand", 0) >= 0)
    return {
        "opponent": opp, "seed": seed, "deals": deals,
        "mean_bb_per_deal": res.mean_bb, "ci": list(res.ci), "verdict": res.verdict,
        "points": res.points["agent1"],
        "think_seconds": [s.time_seconds for s in res.agent_stats["agent1"]],
        "decisions": trace, "n_decisions": half_len,
        "errors": [t for t in trace if "error" in t][:5],
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bot", default="mybot_v2")
    p.add_argument("--opponents", default="equity_bot,aggro_bot,tight_bot,station,random")
    p.add_argument("--deals", type=int, default=300)
    p.add_argument("--seed", default="trace")
    a = p.parse_args(argv)
    opps = a.opponents.split(",")
    jobs = [(a.bot, o, f"{a.seed}-{o}", a.deals) for o in opps]
    from tools.runlock import run_lock
    with run_lock(owner=os.environ.get("RUN_OWNER", "trace_v2 (terminal)"), tag=f"trace {a.bot} vs {a.opponents}"):
        with ProcessPoolExecutor(max_workers=min(len(jobs), os.cpu_count() or 1)) as ex:
            matches = list(ex.map(_trace_match, jobs))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bot_mod = fastarena.load_module(fastarena.resolve(a.bot))
    doc = {"created": stamp, "bot": a.bot, "deals": a.deals,
           "params": getattr(bot_mod, "PARAMS", {}),
           "priors": {s: list(v) for s, v in getattr(bot_mod, "PRIORS", {}).items()},
           "matches": matches}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"trace_{stamp}.json")
    with open(path, "w") as f:
        json.dump(doc, f)
    shutil.copyfile(path, os.path.join(OUT_DIR, "latest.json"))
    bad = 0
    for m in matches:
        n_err = sum(1 for t in m["decisions"] if "error" in t)
        bad += n_err
        print(f"  {m['opponent']:16} {m['mean_bb_per_deal']:+7.3f} bb/deal  {m['verdict']:12} "
              f"{len(m['decisions'])} decisions, {n_err} errors, think {max(m['think_seconds']):.2f}s")
        if n_err:
            print("     first error:", m["errors"][0]["error"], "\n", m["errors"][0]["where"])
    print(f"saved {path}")
    if bad:
        raise SystemExit(f"{bad} decisions raised exceptions")


if __name__ == "__main__":
    main()
