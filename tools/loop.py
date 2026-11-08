"""tools/loop.py - a guided improvement loop for v1 that a smaller model can run safely.

The agent never edits code. It proposes new values for v1's existing settings (DEFAULT_PARAMS
in mybot.py, within the allowed ranges below); this tool applies them in memory, measures, and
an automatic gate decides keep or reject. Only a human can write a kept result into mybot.py.

    .venv/bin/python tools/loop.py status                 # settings, allowed ranges, last results
    .venv/bin/python tools/loop.py leaks --worst 5        # where v1 loses chips, ranked, in plain words
    .venv/bin/python tools/loop.py try eq_raise_half=0.62 tight_call_penalty=0.15   # quick screen
    .venv/bin/python tools/loop.py gate                   # full verdict on the last try: KEEP or REJECT
    .venv/bin/python tools/loop.py promote                # human only: write a KEEP into mybot.py

Every measurement takes the shared run queue and appears on the dashboard. See GEMINI.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
STATE = os.path.join(ROOT, "data", "loop", "state.json")
PY = sys.executable
RANGE = 0.12            # each setting may move at most this far from its shipped value

# The gate's rules. Field points: AGENTS.md treats < 0.5 as noise. Swarm average: about +/-0.06
# run to run at 2 x 1000 deals per style (measured 2026-11-08), so 0.08 is a real move.
GATE = {"field_gain": 0.5, "swarm_gain": 0.08, "family_drop": 0.5, "field_drop": 0.25,
        "swarm_drop": 0.05, "worst10_drop": 0.15}

# Which settings touch which leak, so the leak report can point at levers.
LEVERS = {
    "preflop": ["pf_btn_raise", "pf_btn_limp", "pf_btn_call_raise", "pf_bb_raise", "pf_bb_call"],
    "call": ["tight_call_penalty", "aggro_margin", "aggro_threshold", "tight_threshold"],
    "fold": ["tight_call_penalty", "aggro_margin", "aggro_threshold", "tight_threshold"],
    "bet": ["eq_raise_pot", "eq_raise_half", "eq_bluff_fold_rate", "eq_bluff_min", "tight_probe_min_eq"],
}


# ---------- state and settings
def _state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"next_swarm_base": 5000, "candidate": None, "last_gate": None}


def _save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)


def _defaults():
    import ast
    src = open(os.path.join(ROOT, "mybot.py")).read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_PARAMS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("DEFAULT_PARAMS not found in mybot.py")


def _allowed():
    return {k: (round(max(0.0, v - RANGE), 3), round(min(1.0, v + RANGE), 3)) for k, v in _defaults().items()}


def _owner():
    return os.environ.get("RUN_OWNER", "loop.py (terminal)")


# ---------- measurement (paired: same deals for candidate and baseline)
def _field(params, opponents, seeds, deals, prefix):
    import fastarena
    import tune
    spec = tune._frozen("mybot")
    jobs = [(spec, o, f"{prefix}-{o}-{s}", deals, params) for o in opponents for s in range(seeds)]
    res = fastarena.run_many(jobs, os.cpu_count() or 1, "loop")
    out = {}
    for o in opponents:
        rs = [r for r in res if r["b"] == o]
        out[o] = {"points": sum(r["points"]["agent1"] for r in rs) / len(rs),
                  "bb": sum(r["mean"] for r in rs) / len(rs),
                  "flags": any(r["agent1"]["errors"] or r["agent1"]["forfeit"] or r["agent1"]["bank_empty"] for r in rs)}
    return out


def _families(results):
    import families
    fam = families.families("mybot")
    return {k: sum(results[o]["points"] for o in v if o in results) for k, v in fam.items()}


def _swarm_summary(results):
    bbs = sorted(x["bb"] for x in results.values())
    w = bbs[:max(1, len(bbs) // 10)]
    return {"mean": sum(bbs) / len(bbs), "worst10": sum(w) / len(w),
            "beaten": sum(1 for x in results.values() if x["points"] == 1.0) / len(bbs)}


def _measure(params, label, field_opps, field_seeds, swarm_base, swarm_n, swarm_seeds, deals):
    from tools.runlock import run_lock
    with run_lock(owner=_owner(), tag=f"loop {label}"):
        f = _field(params, field_opps, field_seeds, deals, "loop-field") if field_opps else {}
        s = _field(params, [f"swarm:{swarm_base + i}" for i in range(swarm_n)], swarm_seeds, deals,
                   "loop-swarm") if swarm_n else {}
    return f, s


def _parse_kv(pairs):
    allowed, out = _allowed(), {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"'{p}' is not name=value. Example: eq_raise_half=0.62")
        k, v = p.split("=", 1)
        if k not in allowed:
            raise SystemExit(f"Unknown setting '{k}'. Settings: {', '.join(sorted(allowed))}")
        try:
            v = round(float(v), 3)
        except ValueError:
            raise SystemExit(f"{k} needs a number, got '{v}'.")
        lo, hi = allowed[k]
        if not lo <= v <= hi:
            raise SystemExit(f"{k}={v} is outside its allowed range {lo} to {hi} (shipped {_defaults()[k]}).")
        out[k] = v
    if not out:
        raise SystemExit("Give at least one setting, e.g. try eq_raise_half=0.62")
    return out


# ---------- commands
def cmd_status(a):
    st, d, al = _state(), _defaults(), _allowed()
    print("v1 settings you may change (shipped value, allowed range):")
    for k in sorted(d):
        print(f"  {k:22} {d[k]:<6} {al[k][0]} to {al[k][1]}")
    if st.get("candidate"):
        print(f"\nCurrent candidate: {st['candidate']['params']}  (screened {st['candidate'].get('when', '')})")
    if st.get("last_gate"):
        g = st["last_gate"]
        print(f"Last gate: {g['verdict']} for {g['params']}  ({g['when']})")
        for line in g.get("reasons", []):
            print("   ", line)
    print(f"\nNext gate uses never-seen swarm styles starting at {st['next_swarm_base']}.")


def _latest_swarm_worst(n):
    path = os.path.join(ROOT, "data", "runs.jsonl")
    try:
        runs = [json.loads(l) for l in open(path)]
    except FileNotFoundError:
        runs = []
    from tools.runlock import code_hash
    sw = [r for r in runs if r.get("kind") == "swarm" and r.get("bot") == "mybot" and r.get("base", 0) == 0]
    now = code_hash(os.path.join(ROOT, "mybot.py"))
    sw = [r for r in sw if r.get("code") == now] or sw       # prefer a run of the current code
    if not sw:
        raise SystemExit("No swarm run of mybot on styles 0-79 yet. Run: .venv/bin/python fastarena.py swarm mybot --n 80")
    return [r["opponent"] for r in sorted(sw[-1]["rows"], key=lambda r: r["bb"])[:n]]


def cmd_leaks(a):
    opps = a.opponents.split(",") if a.opponents else _latest_swarm_worst(a.worst)
    print(f"Exporting v1 against {', '.join(opps)} ({a.seeds} seeds)...", flush=True)
    env = dict(os.environ, RUN_OWNER=_owner())
    out = subprocess.run([PY, os.path.join(ROOT, "databricks", "export.py"), "--hero", "mybot", "--opponents",
                          ",".join(opps), "--seeds", str(a.seeds)], cwd=ROOT, env=env, capture_output=True, text=True)
    m = re.search(r"to (\S+databricks/\S+?) in", out.stdout)
    if not m:
        raise SystemExit("Export failed:\n" + out.stdout[-1500:] + out.stderr[-1500:])
    d = m.group(1)
    spots = {}
    for line in open(os.path.join(d, "decisions.jsonl")):
        x = json.loads(line)
        if x["actor"] != "hero":
            continue
        tc, pot, act, st = x["to_call"], x["pot_before"], x["action"], x["street"]
        if st == "PREFLOP":
            continue
        eq = x["hindsight_equity"]
        if eq is None or not tc or act not in ("CHECK_CALL", "FOLD"):
            continue
        size = "a pot-sized bet" if tc >= 0.75 * (pot - tc) else "a half-pot bet"
        lost = (tc - eq * (pot + tc)) if act == "CHECK_CALL" else (eq * (pot + tc) - tc)
        key = (act, st.lower(), size)
        s = spots.setdefault(key, [0, 0.0, 0.0, 0.0])
        s[0] += 1
        s[1] += lost
        s[2] += eq
        s[3] += tc / (pot + tc)
    ranked = sorted(spots.items(), key=lambda kv: -kv[1][1])
    print("\nWhere v1 gives away chips (priced against the opponents' real cards):")
    shown = 0
    for (act, st, size), (n, lost, eq, need) in ranked:
        if lost <= 0 or shown >= 6:
            continue
        shown += 1
        verb = "calls" if act == "CHECK_CALL" else "folds to"
        why = ("it calls with less than the price needs: call less" if act == "CHECK_CALL"
               else "it folds hands that had enough: fold less")
        print(f"  {shown}. On the {st}, v1 {verb} {size} {n} times with {eq / n:.0%} real equity "
              f"(the price needs {need / n:.0%}). Cost: {lost:.0f} chips. {why}.")
        print(f"     Settings that move this: {', '.join(LEVERS['call' if act == 'CHECK_CALL' else 'fold'])}")
    if not shown:
        print("  No call or fold spot loses chips here. Look at betting (settings: "
              + ", ".join(LEVERS["bet"]) + ") or preflop (" + ", ".join(LEVERS["preflop"]) + ").")
    print("\nNext: pick ONE lever and a small step (0.02 to 0.05), then: tools/loop.py try name=value")


def cmd_try(a):
    import families
    params = _parse_kv(a.settings)
    train = [o for k in families.TRAIN_FAMILIES for o in families.families("mybot").get(k, [])]
    print(f"Screening {params} on the training bots (2 seeds) and training swarm styles 0-39...", flush=True)
    t0 = time.time()
    cf, cs = _measure(params, "try", train, 2, 0, 40, 1, 500)
    bf, bs = _measure(None, "try baseline", train, 2, 0, 40, 1, 500)
    dpts = sum(x["points"] for x in cf.values()) - sum(x["points"] for x in bf.values())
    c, b = _swarm_summary(cs), _swarm_summary(bs)
    print(f"  training bots: {dpts:+.2f} points vs shipped")
    print(f"  training swarm: average {c['mean']:+.3f} vs {b['mean']:+.3f} bb/deal, worst 10% {c['worst10']:+.3f} vs {b['worst10']:+.3f}")
    promising = dpts >= 0 and c["mean"] - b["mean"] >= 0
    st = _state()
    st["candidate"] = {"params": params, "when": time.strftime("%Y-%m-%d %H:%M"), "screen_points": round(dpts, 2),
                       "screen_swarm": round(c["mean"] - b["mean"], 3)}
    _save(st)
    print(f"  ({time.time() - t0:.0f}s)")
    print("\nPromising: run tools/loop.py gate for the real verdict." if promising else
          "\nNot promising: worse on the screen. Try a different lever or the opposite direction.")


def cmd_gate(a):
    st = _state()
    if not st.get("candidate"):
        raise SystemExit("No candidate. Run tools/loop.py try name=value first.")
    import fastarena
    params, base = st["candidate"]["params"], st["next_swarm_base"]
    field_opps = [o for o in fastarena.zoo_specs() if o != "mybot"]
    if a.quick:                                       # tool test only: tiny, and can never be promoted
        field_opps, fs, sn, ss, dl = field_opps[:4], 1, 6, 1, 200
    else:
        fs, sn, ss, dl = 8, 80, 2, 1000
    print(f"Gate for {params}: full field 8 seeds + never-seen swarm styles {base}-{base + 79} (2 seeds each).",
          "This takes about 15 minutes.", flush=True)
    cf, cs = _measure(params, "gate", field_opps, fs, base, sn, ss, dl)
    bf, bs = _measure(None, "gate baseline", field_opps, fs, base, sn, ss, dl)
    dpts = sum(x["points"] for x in cf.values()) - sum(x["points"] for x in bf.values())
    fc, fb = _families(cf), _families(bf)
    c, b = _swarm_summary(cs), _swarm_summary(bs)
    dsw, dworst = c["mean"] - b["mean"], c["worst10"] - b["worst10"]
    worst_fam = min(((fc[k] - fb[k], k) for k in fc), default=(0, ""))
    flags = any(x["flags"] for x in list(cf.values()) + list(cs.values()))
    reasons, keep = [], True
    gain = dpts >= GATE["field_gain"] or dsw >= GATE["swarm_gain"]
    reasons.append(f"field {dpts:+.2f} points (needs +{GATE['field_gain']}) or swarm average {dsw:+.3f} (needs +{GATE['swarm_gain']}): "
                   + ("gain" if gain else "no real gain"))
    keep &= gain
    for ok, text in [(dpts > -GATE["field_drop"], f"field not worse than -{GATE['field_drop']}: {dpts:+.2f}"),
                     (worst_fam[0] > -GATE["family_drop"], f"no family worse than -{GATE['family_drop']}: worst {worst_fam[1]} {worst_fam[0]:+.2f}"),
                     (dsw > -GATE["swarm_drop"], f"swarm average not worse than -{GATE['swarm_drop']}: {dsw:+.3f}"),
                     (dworst > -GATE["worst10_drop"], f"swarm worst 10% not worse than -{GATE['worst10_drop']}: {dworst:+.3f}"),
                     (not flags, "no errors, forfeits or empty time banks")]:
        reasons.append(("ok    " if ok else "FAILED ") + text)
        keep &= ok
    verdict = ("TEST-" if a.quick else "") + ("KEEP" if keep else "REJECT")
    print(f"\n{verdict}")
    for r in reasons:
        print("  ", r)
    st["last_gate"] = {"verdict": verdict, "params": params, "when": time.strftime("%Y-%m-%d %H:%M"),
                       "reasons": reasons, "field": round(dpts, 2), "swarm": round(dsw, 3), "worst10": round(dworst, 3)}
    if not a.quick:
        st["next_swarm_base"] = base + 100
    _save(st)
    if a.quick:
        print("\n(quick test: not logged, not promotable)")
        return
    subprocess.run([PY, os.path.join(ROOT, "tools", "changelog.py"), "add", "--author", _owner(), "--bot", "mybot",
                    "--title", f"{'Gate KEEP' if keep else 'Rejected'}: {', '.join(f'{k}={v}' for k, v in params.items())}",
                    "--what", f"Settings tried in memory with tools/loop.py: {params} (shipped: "
                              + ", ".join(f"{k}={_defaults()[k]}" for k in params) + ").",
                    "--why", f"Leak report and screen (training bots {st['candidate'].get('screen_points')} points, "
                             f"training swarm {st['candidate'].get('screen_swarm')}).",
                    "--before", f"field {sum(x['points'] for x in bf.values()):.2f}, swarm {b['mean']:+.3f} (worst 10% {b['worst10']:+.3f})",
                    "--after", f"field {sum(x['points'] for x in cf.values()):.2f}, swarm {c['mean']:+.3f} (worst 10% {c['worst10']:+.3f}); {verdict}",
                    "--evidence", f"Gate: field 8 seeds paired, fresh swarm styles {base}-{base + 79} x 2 x 1000 deals. " + " | ".join(reasons)],
                   cwd=ROOT)
    print("\nKEEP: ask the human to run tools/loop.py promote." if keep else
          "\nREJECT: logged. Try another lever; do not retry the same values.")


def cmd_promote(a):
    st = _state()
    g = st.get("last_gate")
    if not g or g["verdict"] != "KEEP":
        raise SystemExit("Nothing to promote: the last gate was not KEEP.")
    if not sys.stdin.isatty():
        raise SystemExit("promote must be run by a human in a terminal (AGENTS.md: DEFAULT_PARAMS change only "
                         "from a sweep or when a human asks).")
    print(f"Write {g['params']} into mybot.py DEFAULT_PARAMS? ({g['when']}: field {g['field']:+}, swarm {g['swarm']:+})")
    if input("Type PROMOTE to confirm: ").strip() != "PROMOTE":
        raise SystemExit("Not promoted.")
    path = os.path.join(ROOT, "mybot.py")
    src = open(path).read()
    for k, v in g["params"].items():
        src, n = re.subn(rf'("{k}":\s*)[0-9.]+', rf"\g<1>{v}", src, count=1)
        if n != 1:
            raise SystemExit(f"Could not find {k} in mybot.py; nothing written.")
    with open(path, "w") as f:
        f.write(src)
    st["candidate"], g["verdict"] = None, "PROMOTED"
    _save(st)
    print("Written to mybot.py. Run the tests, then commit it.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    lk = sub.add_parser("leaks")
    lk.add_argument("--worst", type=int, default=5, help="the N worst swarm styles from the latest swarm run")
    lk.add_argument("--opponents", help="or name opponents, comma-separated")
    lk.add_argument("--seeds", type=int, default=2)
    lk.set_defaults(fn=cmd_leaks)
    t = sub.add_parser("try")
    t.add_argument("settings", nargs="+", help="name=value ...")
    t.set_defaults(fn=cmd_try)
    g = sub.add_parser("gate")
    g.add_argument("--quick", action="store_true", help="tool self-test only; the verdict cannot be promoted")
    g.set_defaults(fn=cmd_gate)
    sub.add_parser("promote").set_defaults(fn=cmd_promote)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
