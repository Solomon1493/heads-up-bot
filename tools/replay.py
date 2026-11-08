"""tools/replay.py - replay one exact match from a run and record every decision.

Same bot, opponent and seed as the run, so it is the same deals the run scored. Bots with a
TRACE hook (mybot, mybot_v2) record their reasoning per decision; the dashboard's decision
replay reads data/trace/latest.json.

    .venv/bin/python tools/replay.py --bot mybot --opponent rangeaware_mc --seed field-rangeaware_mc-0
"""
import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fastarena  # noqa: E402
from tools.trace_v2 import OUT_DIR, _trace_match  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bot", required=True)
    p.add_argument("--opponent", required=True)
    p.add_argument("--seed", required=True, help="the match seed exactly as the run used it")
    p.add_argument("--deals", type=int, default=1000)
    p.add_argument("--run", default="", help="id of the run this replays (shown on the dashboard)")
    a = p.parse_args(argv)
    from tools.runlock import run_lock
    with run_lock(owner=os.environ.get("RUN_OWNER", "replay.py (terminal)"), tag=f"replay {a.bot} vs {a.opponent}"):
        match = _trace_match((a.bot, a.opponent, a.seed, a.deals))
    bot_mod = fastarena.load_module(fastarena.resolve(a.bot))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    from tools.runlock import code_hash, trace_cache_path
    doc = {"created": stamp, "bot": a.bot, "deals": a.deals, "run": a.run, "code": code_hash(fastarena.resolve(a.bot)),
           "params": getattr(bot_mod, "PARAMS", {}),
           "priors": {s: list(v) for s, v in getattr(bot_mod, "PRIORS", {}).items()},
           "matches": [match]}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"trace_{stamp}.json")
    with open(path, "w") as f:
        json.dump(doc, f)
    shutil.copyfile(path, os.path.join(OUT_DIR, "latest.json"))
    cache = trace_cache_path(a.bot, a.opponent, a.seed)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    shutil.copyfile(path, cache)
    n_err = sum(1 for t in match["decisions"] if "error" in t)
    print(f"{a.bot} vs {a.opponent} ({a.seed}): {match['mean_bb_per_deal']:+.3f} bb/deal, {match['verdict']}, "
          f"{len(match['decisions'])} decisions recorded, {n_err} errors")
    if n_err:
        raise SystemExit(f"{n_err} decisions raised exceptions")


if __name__ == "__main__":
    main()
