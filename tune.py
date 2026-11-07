"""tune.py — Automated experiment runner and telemetry logger.

Usage:
    .venv/bin/python tune.py run                     # one field evaluation, append to log
    .venv/bin/python tune.py run --tag "added bluff"  # same, with a human label
    .venv/bin/python tune.py sweep --trials 20        # random param search
    .venv/bin/python tune.py history                   # print experiment log

Every run snapshots the current mybot.py params, the git diff, runs the full
field evaluation, and appends a structured JSON record to data/experiments.jsonl.
The dashboard reads this file.
"""

import argparse
import copy
import datetime
import difflib
import hashlib
import importlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, "data")
EXPERIMENT_LOG = os.path.join(DATA_DIR, "experiments.jsonl")
SNAPSHOTS_DIR = os.path.join(DATA_DIR, "snapshots")
MYBOT_PATH = os.path.join(ROOT, "mybot.py")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

# ===== OPPONENT SPLIT =====
# Defined in families.py (STRATEGY.md): tune on families A + B, check on C + D + E.
# New bots dropped into zoo/ are exam opponents (family C) unless families.py says otherwise.

# Per-opponent bb/deal is capped at this value when scoring. Past ~3 bb/deal a win is
# already certain, so extra margin vs weak bots earns no points and must not drown out
# the close matchups where points are actually won or lost.
BB_CAP = 3.0


def split_opponents(bot="mybot"):
    """Train = families A + B, held out = C + D + E (families.py, STRATEGY.md)."""
    import families
    return families.split(bot)


def _git_info():
    """Get current git state."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
        diff = subprocess.check_output(
            ["git", "diff", "--stat"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
        dirty = bool(diff)
        full_diff = subprocess.check_output(
            ["git", "diff", "HEAD", "--", "mybot.py", "botlib/equity.py"], cwd=ROOT,
            stderr=subprocess.DEVNULL
        ).decode()
        return {"sha": sha, "dirty": dirty, "diff_stat": diff, "mybot_diff": full_diff[:5000]}
    except Exception:
        return {"sha": "unknown", "dirty": True, "diff_stat": "", "mybot_diff": ""}


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:12]


def _bot_path(bot="mybot"):
    return os.path.join(ROOT, f"{bot}.py")


def _snapshot_code(bot="mybot"):
    """Save a copy of the bot with its hash as filename."""
    path = _bot_path(bot)
    h = _file_hash(path)
    dst = os.path.join(SNAPSHOTS_DIR, f"{bot}_{h}.py")
    if not os.path.exists(dst):
        with open(path, "r") as f:
            src = f.read()
        with open(dst, "w") as f:
            f.write(src)
    return h, dst


def _read_bot_source(bot="mybot"):
    with open(_bot_path(bot), "r") as f:
        return f.read()


def _extract_params(source):
    """Try to extract DEFAULT_PARAMS or PARAMS dict from mybot.py source."""
    # Look for PARAMS = { ... } or DEFAULT_PARAMS = { ... }
    import ast
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("PARAMS", "DEFAULT_PARAMS"):
                        return ast.literal_eval(node.value)
    except Exception:
        pass
    return {}


def run_field(bot="mybot", seeds=2, deals=1000, workers=None, params=None):
    """Run a field evaluation and return parsed results."""
    import families
    opponents = families.all_opponents(bot)
    return run_field_subset(bot=bot, opponents=opponents, seeds=seeds, deals=deals,
                            workers=workers, params=params)


def _family_summary(opponent_results):
    """Points per opponent family, and STRATEGY.md's weighted share of the available points."""
    import families
    out, num, den = {}, 0.0, 0.0
    for fam, members in families.families().items():
        rs = [opponent_results[o] for o in members if o in opponent_results]
        if not rs:
            continue
        pts = sum(r["points"] for r in rs)
        out[fam] = {"points": round(pts, 2), "max": len(rs),
                    "worst_bb": round(min(r["mean_bb_deal"] for r in rs), 3)}
        w = families.FIELD_WEIGHT.get(fam, 0.0)
        num += w * pts / len(rs)
        den += w
    out["weighted_share"] = round(num / den, 4) if den else None
    return out


def _public(field):
    """Field results without the in-memory per-deal arrays (what goes into the log)."""
    return {k: v for k, v in field.items() if k != "_paired"}


def run_field_subset(bot="mybot", opponents=None, seeds=2, deals=1000, workers=None,
                     params=None, seed_prefix="tune"):
    """Run field evaluation against a specific list of opponents.

    `params` overrides the bot's PARAMS inside every match process (fastarena.apply_params),
    so the bot that plays is exactly the one being measured. Same seed_prefix = same deals,
    so two runs with the same prefix are a paired comparison.
    """
    if workers is None:
        workers = os.cpu_count() or 1

    import fastarena

    if opponents is None:
        opponents = [o for o in fastarena.zoo_specs() if o != bot]

    jobs = [(bot, opp, f"{seed_prefix}-{opp}-{s}", deals, params) for opp in opponents for s in range(seeds)]
    results = fastarena.run_many(jobs, min(workers, len(jobs)))

    # Parse into structured output
    opponent_results = {}
    total_pts = 0.0
    score_bb, score_var = 0.0, 0.0
    paired = {}
    for opp in opponents:
        rs = [r for r in results if r["b"] == opp]
        if not rs:
            continue
        w = sum(r["points"]["agent1"] == 1.0 for r in rs)
        l = sum(r["points"]["agent1"] == 0.0 for r in rs)
        c = len(rs) - w - l
        pts = sum(r["points"]["agent1"] for r in rs) / len(rs)
        total_pts += pts
        mean_bb = sum(r["mean"] for r in rs) / len(rs)
        ci_hw = sum((r["ci"][1] - r["ci"][0]) / 2 for r in rs) / len(rs)
        max_time = max(r["agent1"]["max_time"] for r in rs)
        errors = sum(r["agent1"]["errors"] for r in rs)
        forfeit = any(r["agent1"]["forfeit"] for r in rs)
        bank_empty = any(r["agent1"]["bank_empty"] for r in rs)
        # standard error of this opponent's mean over seeds (each match: CI half-width = 1.96 se)
        se = sum(((r["ci"][1] - r["ci"][0]) / 2 / 1.96) ** 2 for r in rs) ** 0.5 / len(rs)
        score_bb += min(mean_bb, BB_CAP)
        score_var += se ** 2

        paired[opp] = [r["paired"] for r in sorted(rs, key=lambda r: r["seed"])]
        opponent_results[opp] = {
            "wins": w, "chops": c, "losses": l,
            "points": pts, "mean_bb_deal": round(mean_bb, 4),
            "ci_half_width": round(ci_hw, 4),
            "max_time_s": round(max_time, 3),
            "errors": errors, "forfeit": forfeit, "bank_empty": bank_empty,
        }

    return {
        "total_points": round(total_pts, 2),
        "max_points": len(opponents),
        "score_bb": round(score_bb, 4),             # sum of per-opponent bb/deal, capped at BB_CAP
        "score_bb_se": round(score_var ** 0.5, 4),  # its standard error
        "families": _family_summary(opponent_results),
        "opponents": opponent_results,
        "_paired": paired,                           # per-deal results; stripped before logging
        "seeds": seeds,
        "deals": deals,
    }


def run_experiment(tag="", bot="mybot", seeds=2, deals=1000, workers=None, params_override=None):
    """Run one full experiment: snapshot code, run field, log everything."""
    experiment_id = uuid.uuid4().hex[:10]
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Snapshot
    code_hash, snapshot_path = _snapshot_code(bot)
    source = _read_bot_source(bot)
    params = _extract_params(source)
    git = _git_info()

    if params_override:
        params.update(params_override)

    # Run field
    t0 = time.time()
    field_results = run_field(bot=bot, seeds=seeds, deals=deals, workers=workers,
                              params=params_override or None)
    elapsed = round(time.time() - t0, 2)

    # Build record
    record = {
        "id": experiment_id,
        "timestamp": timestamp,
        "tag": tag,
        "bot": bot,
        "code_hash": code_hash,
        "snapshot": os.path.basename(snapshot_path),
        "params": params,
        "params_overridden": dict(params_override or {}),   # values that differ from mybot.py on disk
        "git": git,
        "source_lines": len(source.splitlines()),
        "field": _public(field_results),
        "elapsed_seconds": elapsed,
        "status": "ok",
    }

    # Safety checks
    any_error = any(o["errors"] > 0 for o in field_results["opponents"].values())
    any_forfeit = any(o["forfeit"] for o in field_results["opponents"].values())
    any_timeout = any(o["bank_empty"] for o in field_results["opponents"].values())
    max_time = max((o["max_time_s"] for o in field_results["opponents"].values()), default=0)

    record["health"] = {
        "errors": any_error,
        "forfeits": any_forfeit,
        "timeouts": any_timeout,
        "max_thinking_s": round(max_time, 3),
        "safe": not any_error and not any_forfeit and not any_timeout and max_time < 4.0,
    }

    # Append to log
    with open(EXPERIMENT_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

    return record


def load_history():
    """Load all experiments from the log."""
    if not os.path.exists(EXPERIMENT_LOG):
        return []
    records = []
    with open(EXPERIMENT_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def diff_snapshots(hash1, hash2):
    """Generate a unified diff between two code snapshots."""
    f1 = os.path.join(SNAPSHOTS_DIR, f"mybot_{hash1}.py")
    f2 = os.path.join(SNAPSHOTS_DIR, f"mybot_{hash2}.py")
    if not os.path.exists(f1) or not os.path.exists(f2):
        return "Snapshot not found"
    with open(f1) as a, open(f2) as b:
        lines_a = a.readlines()
        lines_b = b.readlines()
    diff = difflib.unified_diff(lines_a, lines_b, fromfile=f"mybot_{hash1}.py",
                                 tofile=f"mybot_{hash2}.py", lineterm="")
    return "\n".join(diff)


def _build_experiment_record(tag, field_results, params, bot="mybot", extra=None):
    """Build a structured experiment record without running a new evaluation."""
    experiment_id = uuid.uuid4().hex[:10]
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    code_hash, snapshot_path = _snapshot_code(bot)
    source = _read_bot_source(bot)
    git = _git_info()

    record = {
        "id": experiment_id,
        "timestamp": timestamp,
        "tag": tag,
        "bot": bot,
        "code_hash": code_hash,
        "snapshot": os.path.basename(snapshot_path),
        "params": params,
        "git": git,
        "source_lines": len(source.splitlines()),
        "field": _public(field_results),
        "elapsed_seconds": 0,
        "status": "ok",
    }
    if extra:
        record.update(extra)

    any_error = any(o["errors"] > 0 for o in field_results["opponents"].values())
    any_forfeit = any(o["forfeit"] for o in field_results["opponents"].values())
    any_timeout = any(o["bank_empty"] for o in field_results["opponents"].values())
    max_time = max((o["max_time_s"] for o in field_results["opponents"].values()), default=0)
    record["health"] = {
        "errors": any_error,
        "forfeits": any_forfeit,
        "timeouts": any_timeout,
        "max_thinking_s": round(max_time, 3),
        "safe": not any_error and not any_forfeit and not any_timeout and max_time < 4.0,
    }
    return record


def print_summary(record):
    """Pretty-print one experiment."""
    f = record["field"]
    h = record["health"]
    safe_icon = "✅" if h["safe"] else "❌"
    print(f"\n{'='*70}")
    print(f"  Experiment {record['id']}  |  {record['timestamp']}")
    print(f"  Tag: {record.get('tag', '(none)')}")
    print(f"  Code: {record['code_hash']}  |  Git: {record['git']['sha']}"
          f"{'*' if record['git']['dirty'] else ''}")
    print(f"  Score: {f['total_points']:.2f} / {f['max_points']}  |  "
          f"Time: {h['max_thinking_s']:.2f}s  |  Safe: {safe_icon}")
    print(f"{'='*70}")
    print(f"  {'Opponent':20} {'W':>3} {'C':>3} {'L':>3}  {'pts':>5}  {'bb/deal':>9}  {'CI hw':>8}  {'time':>6}")
    for opp, r in sorted(f["opponents"].items(), key=lambda x: -x[1]["points"]):
        flags = []
        if r["errors"]: flags.append("ERR")
        if r["forfeit"]: flags.append("FORFEIT")
        if r["bank_empty"]: flags.append("TIMEOUT")
        flag_str = " " + ",".join(flags) if flags else ""
        print(f"  {opp:20} {r['wins']:3d} {r['chops']:3d} {r['losses']:3d}  "
              f"{r['points']:5.2f}  {r['mean_bb_deal']:+9.3f}  {r['ci_half_width']:8.3f}  "
              f"{r['max_time_s']:6.2f}{flag_str}")
    print()


def cmd_run(args):
    bot = getattr(args, "bot", "mybot")
    print(f"Running field evaluation for {bot} (seeds={args.seeds}, deals={args.deals})...")
    override = json.loads(args.params) if args.params else None
    if override:
        print(f"Parameter overrides ({bot}.py on disk is NOT changed): {override}")
    record = run_experiment(
        tag=args.tag, bot=bot, seeds=args.seeds, deals=args.deals, workers=args.workers,
        params_override=override,
    )
    print_summary(record)
    print(f"Logged to {EXPERIMENT_LOG}")


def cmd_history(args):
    records = load_history()
    if not records:
        print("No experiments yet. Run: python tune.py run")
        return
    print(f"\n{'ID':>12}  {'Timestamp':>25}  {'Hash':>8}  {'Score':>8}  {'Safe':>5}  Tag")
    print("-" * 85)
    for r in records:
        safe = "✅" if r["health"]["safe"] else "❌"
        pts = f"{r['field']['total_points']:.2f}/{r['field']['max_points']}"
        print(f"  {r['id']:>10}  {r['timestamp'][:19]:>25}  {r['code_hash']:>8}  "
              f"{pts:>8}  {safe:>5}  {r.get('tag', '')}")
    print(f"\nTotal: {len(records)} experiments")


def _paired_se(cand, base, opponents=None):
    """Noise of (candidate - baseline) from deal-by-deal differences on the same deals.
    Card luck cancels, so this is far smaller than treating the runs as independent."""
    var = 0.0
    for opp in (opponents or cand["opponents"].keys()):
        a, b = cand.get("_paired", {}).get(opp), base.get("_paired", {}).get(opp)
        if not a or not b or len(a) != len(b):
            return None
        per_seed = []
        for xa, xb in zip(a, b):
            diffs = [p - q for p, q in zip(xa, xb)]
            n = len(diffs)
            m = sum(diffs) / n
            per_seed.append(sum((x - m) ** 2 for x in diffs) / (n - 1) / n)
        var += sum(per_seed) / len(per_seed) ** 2      # variance of the mean over seeds
    return var ** 0.5


def _delta(cand, base, opponents=None):
    """Candidate minus baseline score (capped bb/deal) and its noise (paired when possible)."""
    opps = opponents or list(cand["opponents"].keys())
    d = sum(min(cand["opponents"][o]["mean_bb_deal"], BB_CAP) - min(base["opponents"][o]["mean_bb_deal"], BB_CAP)
            for o in opps)
    se = _paired_se(cand, base, opps)
    if se is None:                                          # fall back to the independent estimate
        se = (cand["score_bb_se"] ** 2 + base["score_bb_se"] ** 2) ** 0.5
    return d, se


def _family_deltas(cand, base):
    """Per held-out family: (delta, noise). Used for the worst-family rule."""
    import families
    out = {}
    for fam, members in families.families().items():
        opps = [o for o in members if o in cand["opponents"] and o in base["opponents"]]
        if opps:
            out[fam] = _delta(cand, base, opps)
    return out


def _healthy(field_results):
    return not any(o["errors"] or o["forfeit"] or o["bank_empty"] or o["max_time_s"] >= 4.0
                   for o in field_results["opponents"].values())


def _log(record):
    with open(EXPERIMENT_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def cmd_sweep(args):
    """Random search around the current params, judged against the current params.

    1. Play the current params on the TRAIN opponents (baseline).
    2. Play each perturbed candidate on the same deals; score = difference vs baseline.
    3. Candidates that beat the baseline by more than 2 standard errors are finalists.
    4. Finalists and the baseline replay the HELD-OUT opponents on fresh deals.
       A candidate is recommended only if it also beats the baseline there, with no
       errors, forfeits or slow decisions. Nothing is written to the bot file.
    """
    bot = getattr(args, "bot", "mybot")
    base_params = _extract_params(_read_bot_source(bot))
    if not base_params:
        print(f"No PARAMS or DEFAULT_PARAMS dict found in {bot}.py.")
        return
    train_opps, holdout_opps = split_opponents(bot=bot)
    if not holdout_opps:
        print("Nothing is held out. Check the families in families.py.")
        return

    rng = random.Random(args.sweep_seed)
    kw = dict(seeds=args.seeds, deals=args.deals, workers=args.workers)
    print(f"Sweep for {bot}: {args.trials} trials, {args.seeds} seed(s) x {args.deals} deals per opponent")
    print(f"Train opponents:    {train_opps}")
    print(f"Held-out opponents: {holdout_opps}")
    print(f"Score = sum of per-opponent bb/deal, each capped at {BB_CAP}")

    base_train = run_field_subset(bot, train_opps, **kw)
    _log(_build_experiment_record(f"sweep-baseline-train-{bot}", base_train, base_params, bot=bot,
                                  extra={"opponent_set": "train"}))
    print(f"\nBaseline (current params) train score: {base_train['score_bb']:+.3f} "
          f"(points {base_train['total_points']:.2f}/{base_train['max_points']})")

    trials = []
    for trial in range(args.trials):
        cand = {}
        for k, v in base_params.items():
            if isinstance(v, (int, float)):
                if v <= 1.0:
                    cand[k] = round(max(0.0, min(1.0, v + v * 0.35 * (rng.random() * 2 - 1))), 4)
                else:
                    cand[k] = round(max(1.0, v * (1.0 + 0.40 * (rng.random() * 2 - 1))), 2)
            else:
                cand[k] = v
        res = run_field_subset(bot, train_opps, params=cand, **kw)
        d, se = _delta(res, base_train)
        _log(_build_experiment_record(f"sweep-{trial}-{bot}", res, cand, bot=bot, extra={
            "sweep_trial": trial, "opponent_set": "train", "delta_vs_baseline": round(d, 4),
            "delta_se": round(se, 4)}))
        verdict = "finalist" if d > 2 * se and _healthy(res) else ""   # must beat noise (STRATEGY.md ship rule)
        print(f"  trial {trial + 1:2d}/{args.trials}: {d:+.3f} vs current (noise +/-{2 * se:.3f})  {verdict}")
        trials.append((d, se, cand, res))

    finalists = [t for t in sorted(trials, key=lambda t: -t[0])
                 if t[0] > 2 * t[1] and _healthy(t[3])][:args.finalists]
    if not finalists:
        print("\nNo candidate beat the current params by more than the noise (2 standard errors). Keep the current params.")
        print("(Try more --seeds / --deals for a finer measurement, or change the strategy instead.)")
        return

    hkw = dict(kw, seeds=max(2, args.seeds), seed_prefix="holdout")
    print(f"\nHeld-out check on fresh deals ({hkw['seeds']} seed(s) per opponent)...")
    base_hold = run_field_subset(bot, holdout_opps, **hkw)
    _log(_build_experiment_record(f"sweep-baseline-holdout-{bot}", base_hold, base_params, bot=bot,
                                  extra={"opponent_set": "holdout"}))
    accepted = None
    for i, (d, se, cand, _) in enumerate(finalists):
        res = run_field_subset(bot, holdout_opps, params=cand, **hkw)
        hd, hse = _delta(res, base_hold)
        fams = _family_deltas(res, base_hold)
        worst = min(fams.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1e-9)) if fams else None
        no_family_worse = all(fd > -2 * fse for fd, fse in fams.values())
        ok = hd > 0 and no_family_worse and _healthy(res)
        for fam, (fd, fse) in sorted(fams.items()):
            print(f"      family {fam}: {fd:+.3f} (noise +/-{2 * fse:.3f})")
        _log(_build_experiment_record(f"sweep-finalist-{i}-holdout-{bot}", res, cand, bot=bot, extra={
            "opponent_set": "holdout", "train_delta": round(d, 4),
            "delta_vs_baseline": round(hd, 4), "delta_se": round(hse, 4)}))
        print(f"  finalist {i + 1}: train {d:+.3f}, held-out {hd:+.3f} (noise +/-{2 * hse:.3f})  "
              f"{'PASSES' if ok else 'fails'}")
        if ok and accepted is None:
            accepted = (d, hd, cand)

    print("\n" + "=" * 60)
    if not accepted:
        print("No finalist also improved on held-out opponents: the train gains were likely")
        print("overfitting or luck. Keep the current params.")
        return
    d, hd, cand = accepted
    changed = {k: (base_params[k], v) for k, v in cand.items() if v != base_params.get(k)}
    print(f"RECOMMENDED: train {d:+.3f}, held-out {hd:+.3f} bb/deal (capped score).")
    print(f"Tuned values only ship once they are in {bot}.py and committed. Change PARAMS in {bot}.py:")
    for k, (old, new) in changed.items():
        print(f"    {k:22} {old} -> {new}")
    print(f"Then re-run `python fastarena.py field {bot} --seeds 4` before committing.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run one field evaluation and log it")
    run_p.add_argument("--bot", default="mybot", help="which bot to evaluate (e.g. mybot or mybot_v2)")
    run_p.add_argument("--tag", default="", help="human label for this run")
    run_p.add_argument("--seeds", type=int, default=2)
    run_p.add_argument("--deals", type=int, default=1000)
    run_p.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    run_p.add_argument("--params", help='JSON overrides for bot PARAMS, e.g. \'{"prior_strength": 15.0}\'')
    run_p.set_defaults(fn=cmd_run)

    hist_p = sub.add_parser("history", help="show experiment log")
    hist_p.set_defaults(fn=cmd_history)

    sweep_p = sub.add_parser("sweep", help="random parameter sweep")
    sweep_p.add_argument("--bot", default="mybot", help="which bot to sweep (e.g. mybot or mybot_v2)")
    sweep_p.add_argument("--trials", type=int, default=10)
    sweep_p.add_argument("--seeds", type=int, default=2)
    sweep_p.add_argument("--deals", type=int, default=1000)
    sweep_p.add_argument("--finalists", type=int, default=3,
                         help="how many top train candidates get the held-out check")
    sweep_p.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    sweep_p.add_argument("--sweep-seed", type=int, default=42)
    sweep_p.set_defaults(fn=cmd_sweep)

    args = p.parse_args()
    if args.cmd in ("run", "sweep"):
        # One measurement at a time across every tool and agent (see tools/runlock.py).
        from tools.runlock import run_lock
        owner = os.environ.get("RUN_OWNER", "tune.py (terminal)")
        with run_lock(owner=owner, tag=f"{args.cmd} {getattr(args, 'bot', 'mybot')} {getattr(args, 'tag', '')}".strip()):
            args.fn(args)
    else:
        args.fn(args)


if __name__ == "__main__":
    main()

