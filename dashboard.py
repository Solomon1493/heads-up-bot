"""dashboard.py — Interactive visual dashboard for the poker bot tuning harness.

Launch:
    .venv/bin/python dashboard.py          # opens http://localhost:8050

Features:
    - Live experiment history with score progression chart
    - Per-opponent win/chop/loss breakdown
    - Code diff viewer (IDE-style) between any two snapshots
    - Active parameter display with inline editing
    - Health indicators (errors, time bank, forfeits)
    - Human-in-the-loop: trigger new runs, edit params, add tags
    - Teammate-friendly: every metric has a plain-language tooltip
"""

import json
import os
import sys
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

DATA_DIR = os.path.join(ROOT, "data")
EXPERIMENT_LOG = os.path.join(DATA_DIR, "experiments.jsonl")
SNAPSHOTS_DIR = os.path.join(DATA_DIR, "snapshots")
MYBOT_PATH = os.path.join(ROOT, "mybot.py")
AGENT_LOG_PATH = os.path.join(DATA_DIR, "agent_activity.json")

# Process tracking for live status
CURRENT_JOB = {
    "running": False,
    "pid": None,
    "tag": "",
    "start_time": None,
    "proc": None,
}

app = FastAPI(title="PokerBot Tuning Dashboard")


def log_agent_activity(action: str, why: str, changes: str):
    """Record an agent activity entry with timestamp and reasoning."""
    entries = []
    if os.path.exists(AGENT_LOG_PATH):
        try:
            with open(AGENT_LOG_PATH, "r") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    import datetime
    entries.insert(0, {
        "timestamp": datetime.datetime.now().strftime("%I:%M:%S %p"),
        "action": action,
        "why": why,
        "changes": changes,
    })
    # Keep last 50 entries
    entries = entries[:50]
    with open(AGENT_LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def get_agent_activities():
    if os.path.exists(AGENT_LOG_PATH):
        try:
            with open(AGENT_LOG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def load_experiments():
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


def read_snapshot(code_hash):
    path = os.path.join(SNAPSHOTS_DIR, f"mybot_{code_hash}.py")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return None


def read_current_mybot():
    with open(MYBOT_PATH) as f:
        return f.read()


# ---- API Endpoints ----

@app.get("/api/experiments")
def api_experiments():
    return load_experiments()


def _job_env():
    env = dict(os.environ)
    env["RUN_OWNER"] = "dashboard"
    return env


@app.get("/api/status")
def api_status():
    """The dashboard's own job, plus whoever holds the shared run queue (any tool or agent)."""
    global CURRENT_JOB
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is not None:
        CURRENT_JOB["running"] = False
        CURRENT_JOB["proc"] = None
    from tools.runlock import holder
    h = holder()
    mine = CURRENT_JOB["running"]
    return {
        "running": mine,
        "tag": CURRENT_JOB["tag"],
        "elapsed_seconds": round(time.time() - CURRENT_JOB["start_time"], 1) if mine and CURRENT_JOB["start_time"] else 0,
        "queue_holder": h,                                   # e.g. {"owner": "tune.py (terminal)", ...}
        "queued": bool(mine and h and h.get("owner") != "dashboard"),
    }


@app.get("/api/agent-activity")
def api_agent_activity():
    return get_agent_activities()


def _disk_params():
    import ast
    tree = ast.parse(read_current_mybot())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_PARAMS":
                    return ast.literal_eval(node.value)
    return {}


def _validate_params(new_params, require_all):
    """Return an error string, or None if the params are safe to use.

    A blank box arrives as null; writing that into mybot.py would make every comparison
    raise, and the bot would silently check/fold for the whole tournament.
    """
    import math
    if not isinstance(new_params, dict) or not new_params:
        return "No params provided"
    known = _disk_params()
    unknown = sorted(set(new_params) - set(known))
    if unknown:
        return f"Unknown parameter(s): {unknown}"
    if require_all and set(new_params) != set(known):
        return f"Missing parameter(s): {sorted(set(known) - set(new_params))}"
    for k, v in new_params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return f"{k} must be a number (got {v!r})"
        if not 0.0 <= v <= 1.0:
            return f"{k} must be between 0 and 1 (got {v})"
    return None


@app.post("/api/save-params")
async def api_save_params(request: Request):
    """Update the numbers in DEFAULT_PARAMS in mybot.py, in place (comments kept).

    Strict: every key must exist, every value must be a finite number in [0, 1], and the
    rewritten file must parse back to exactly these values, or nothing is written.
    """
    import re
    data = await request.json()
    new_params = data.get("params", {})
    err = _validate_params(new_params, require_all=True)
    if err:
        return JSONResponse({"error": err}, 400)

    try:
        source = read_current_mybot()
        m = re.search(r"DEFAULT_PARAMS\s*=\s*\{[^}]*\}", source)
        if not m:
            return JSONResponse({"error": "DEFAULT_PARAMS block not found in mybot.py"}, 500)
        block = m.group(0)
        for k, v in new_params.items():
            block, n = re.subn(rf'("{re.escape(k)}"\s*:\s*)[-+0-9.eE]+', rf"\g<1>{float(v)!r}", block)
            if n != 1:
                return JSONResponse({"error": f"could not locate {k} in DEFAULT_PARAMS"}, 500)
        new_source = source[:m.start()] + block + source[m.end():]
        import ast
        compile(new_source, MYBOT_PATH, "exec")      # never write a file that will not import
        tmp = MYBOT_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(new_source)
        os.replace(tmp, MYBOT_PATH)
        if _disk_params() != {k: float(v) for k, v in new_params.items()}:
            with open(MYBOT_PATH, "w") as f:
                f.write(source)                        # roll back
            return JSONResponse({"error": "verification failed; mybot.py left unchanged"}, 500)

        log_agent_activity(
            action="User Edited Parameters via Dashboard",
            why="Manual human-in-the-loop parameter adjustment",
            changes=json.dumps(new_params, indent=2)
        )
        return {"status": "saved", "params": new_params}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@app.get("/api/snapshot/{code_hash}")
def api_snapshot(code_hash: str):
    src = read_snapshot(code_hash)
    if src is None:
        return JSONResponse({"error": "not found"}, 404)
    return {"code_hash": code_hash, "source": src}


@app.get("/api/current-source")
def api_current_source():
    return {"source": read_current_mybot()}


@app.get("/api/current-params")
def api_current_params():
    try:
        import ast
        source = read_current_mybot()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEFAULT_PARAMS":
                        return {"params": ast.literal_eval(node.value)}
    except Exception as e:
        return {"params": {}, "error": str(e)}
    return {"params": {}}


@app.post("/api/run")
async def api_run(request: Request):
    global CURRENT_JOB
    body = await request.json()
    tag = str(body.get("tag", "dashboard-run"))[:80]
    try:
        seeds = max(1, min(10, int(body.get("seeds", 1))))
        deals = max(100, min(2000, int(body.get("deals", 500))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "seeds and deals must be whole numbers"}, 400)
    params = body.get("params")            # optional: test these values WITHOUT saving them
    if params:
        err = _validate_params(params, require_all=False)
        if err:
            return JSONResponse({"error": err}, 400)

    # Check if already running
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is None:
        return JSONResponse({"status": "already_running", "tag": CURRENT_JOB["tag"]}, 400)

    cmd = [
        sys.executable, os.path.join(ROOT, "tune.py"), "run",
        "--tag", tag, "--seeds", str(seeds), "--deals", str(deals),
    ]
    if params:
        cmd += ["--params", json.dumps(params)]
    # Direct output to a log file instead of keeping pipe buffer open
    run_log_file = open(os.path.join(DATA_DIR, "last_run.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=run_log_file, stderr=subprocess.STDOUT, env=_job_env())
    
    CURRENT_JOB = {
        "running": True,
        "pid": proc.pid,
        "tag": tag,
        "start_time": time.time(),
        "proc": proc,
    }
    return {"status": "started", "pid": proc.pid}


@app.get("/api/diff/{hash1}/{hash2}")
def api_diff(hash1: str, hash2: str):
    import difflib
    src1 = read_snapshot(hash1)
    src2 = read_snapshot(hash2)
    if src1 is None or src2 is None:
        return JSONResponse({"error": "snapshot not found"}, 404)
    diff = list(difflib.unified_diff(
        src1.splitlines(keepends=True), src2.splitlines(keepends=True),
        fromfile=f"mybot_{hash1}.py", tofile=f"mybot_{hash2}.py"
    ))
    return {"diff": "".join(diff), "from": hash1, "to": hash2}


# ---- Serve the dashboard ----

UI_PATH = os.path.join(ROOT, "dashboard_ui", "index.html")
TRACE_PATH = os.path.join(DATA_DIR, "trace", "latest.json")


@app.get("/api/trace")
def api_trace():
    """Latest tools/trace_v2.py output: every v2 decision with its beliefs and EVs."""
    if not os.path.exists(TRACE_PATH):
        return JSONResponse({"error": "No trace yet. Run a traced match to watch the model think."}, 404)
    with open(TRACE_PATH) as f:
        return JSONResponse(json.load(f))


@app.post("/api/trace/run")
async def api_trace_run(request: Request):
    """Start tools/trace_v2.py in the background (one job at a time)."""
    global CURRENT_JOB
    body = await request.json()
    import fastarena
    known = set(fastarena.zoo_specs())
    opps = [o for o in str(body.get("opponents", "")).split(",") if o in known]
    if not opps:
        return JSONResponse({"error": f"Pick at least one opponent from: {sorted(known)}"}, 400)
    try:
        deals = max(50, min(1000, int(body.get("deals", 300))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "deals must be a whole number"}, 400)
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is None:
        return JSONResponse({"status": "already_running", "tag": CURRENT_JOB["tag"]}, 400)
    cmd = [sys.executable, os.path.join(ROOT, "tools", "trace_v2.py"),
           "--opponents", ",".join(opps), "--deals", str(deals)]
    log = open(os.path.join(DATA_DIR, "last_run.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=_job_env())
    CURRENT_JOB = {"running": True, "pid": proc.pid, "tag": "trace", "start_time": time.time(), "proc": proc}
    return {"status": "started", "pid": proc.pid}


SCOREBOARD_BOTS = ("mybot", "mybot_v2")


@app.get("/api/scoreboard")
def api_scoreboard():
    """Latest full-field run (every opponent, tune.py run) per bot, newest first."""
    best = {}
    for r in load_experiments():
        if r.get("opponent_set", "all") != "all" or r.get("sweep_trial") is not None:
            continue
        if r.get("params_overridden"):
            continue                                        # a what-if test, not the bot as it is
        bot = r.get("bot", "mybot")
        best[bot] = r                                       # log is chronological: last one wins
    out = []
    for bot in SCOREBOARD_BOTS:
        r = best.get(bot)
        if r:
            out.append({"bot": bot, "timestamp": r["timestamp"], "code_hash": r.get("code_hash"),
                        "field": r["field"], "health": r.get("health", {})})
        else:
            out.append({"bot": bot, "timestamp": None})
    import families
    fam = families.families()
    return {"bots": out, "family_of": {o: k for k, v in fam.items() for o in v},
            "family_names": families.FAMILY_NAMES, "weights": families.FIELD_WEIGHT}


@app.post("/api/scoreboard/run")
async def api_scoreboard_run(request: Request):
    """Re-measure every scoreboard bot on the full field, one after another (queued)."""
    global CURRENT_JOB
    body = await request.json()
    try:
        seeds = max(1, min(6, int(body.get("seeds", 2))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "seeds must be a whole number"}, 400)
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is None:
        return JSONResponse({"status": "already_running", "tag": CURRENT_JOB["tag"]}, 400)
    import shlex
    steps = [" ".join(map(shlex.quote, [sys.executable, os.path.join(ROOT, "tune.py"), "run", "--bot", b,
                                        "--seeds", str(seeds), "--tag", "scoreboard"])) for b in SCOREBOARD_BOTS]
    log = open(os.path.join(DATA_DIR, "last_run.log"), "w")
    proc = subprocess.Popen(["/bin/sh", "-c", " && ".join(steps)], cwd=ROOT, stdout=log,
                            stderr=subprocess.STDOUT, env=_job_env())
    CURRENT_JOB = {"running": True, "pid": proc.pid, "tag": "scoreboard", "start_time": time.time(), "proc": proc}
    return {"status": "started", "pid": proc.pid}


@app.post("/api/replay")
async def api_replay(request: Request):
    """Replay one exact match of a run (tools/replay.py) and record every decision. Queued."""
    global CURRENT_JOB
    body = await request.json()
    import fastarena
    bot, opp, seed = str(body.get("bot", "")), str(body.get("opponent", "")), str(body.get("seed", ""))
    try:
        fastarena.resolve(bot), fastarena.resolve(opp)
    except SystemExit as exc:
        return JSONResponse({"error": str(exc)}, 400)
    if not seed or len(seed) > 120:
        return JSONResponse({"error": "This run did not record match seeds, so it cannot be replayed exactly."}, 400)
    try:
        deals = max(50, min(1000, int(body.get("deals", 1000))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "deals must be a whole number"}, 400)
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is None:
        return JSONResponse({"status": "already_running", "tag": CURRENT_JOB["tag"]}, 400)
    cmd = [sys.executable, os.path.join(ROOT, "tools", "replay.py"), "--bot", bot, "--opponent", opp,
           "--seed", seed, "--deals", str(deals), "--run", str(body.get("run", ""))]
    log = open(os.path.join(DATA_DIR, "last_run.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=_job_env())
    CURRENT_JOB = {"running": True, "pid": proc.pid, "tag": "replay", "start_time": time.time(), "proc": proc}
    return {"status": "started", "pid": proc.pid}


STRESS_PATH = os.path.join(DATA_DIR, "stress", "latest.json")


@app.get("/api/stress")
def api_stress():
    """Latest tools/stress_v1.py result: the shipped bot's time-bank use under CPU load."""
    if not os.path.exists(STRESS_PATH):
        return JSONResponse({"error": "No stress test yet."}, 404)
    with open(STRESS_PATH) as f:
        return JSONResponse(json.load(f))


@app.post("/api/stress/run")
async def api_stress_run():
    """Run tools/stress_v1.py (real judge over HTTP, 0/1/2/4 busy processes per core). Queued."""
    global CURRENT_JOB
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"] and CURRENT_JOB["proc"].poll() is None:
        return JSONResponse({"status": "already_running", "tag": CURRENT_JOB["tag"]}, 400)
    cmd = [sys.executable, os.path.join(ROOT, "tools", "stress_v1.py"), "--load", "0", "1", "2", "4"]
    log = open(os.path.join(DATA_DIR, "last_run.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=_job_env())
    CURRENT_JOB = {"running": True, "pid": proc.pid, "tag": "stress", "start_time": time.time(), "proc": proc}
    return {"status": "started", "pid": proc.pid}


def _families_of(opps):
    """{family: {points, max}} and the weighted share, from {opponent: {points}}."""
    import families
    out, num, den = {}, 0.0, 0.0
    for fam, members in families.families().items():
        rs = [opps[o]["points"] for o in members if o in opps]
        if not rs:
            continue
        out[fam] = {"points": round(sum(rs), 2), "max": len(rs)}
        w = families.FIELD_WEIGHT.get(fam, 0.0)
        num += w * sum(rs) / len(rs)
        den += w
    return out, (round(num / den, 4) if den else None)


def _all_runs():
    """Every finished run from every tool, normalized to one shape, oldest first.

    Sources: data/experiments.jsonl (tune.py run/sweep, dashboard buttons) and data/runs.jsonl
    (fastarena field/ab, stress). Field-style runs carry per-opponent results."""
    runs = []
    for r in load_experiments():
        f = r.get("field") or {}
        opps = {o: {"w": x.get("wins", 0), "c": x.get("chops", 0), "l": x.get("losses", 0),
                    "points": x.get("points", 0.0), "bb": x.get("mean_bb_deal", 0.0),
                    "ci": x.get("ci_half_width"), "max_s": x.get("max_time_s", 0.0),
                    "flags": ",".join(k for k in ("forfeit", "bank_empty") if x.get(k)) or
                             (f"{x['errors']} err" if x.get("errors") else "")}
                for o, x in (f.get("opponents") or {}).items()}
        sweep = r.get("sweep_trial") is not None or str(r.get("tag", "")).startswith("sweep")
        runs.append({"id": "t-" + str(r.get("id")), "timestamp": str(r.get("timestamp", ""))[:19],
                     "owner": r.get("owner") or "tune.py", "kind": "sweep" if sweep else "field",
                     "tool": "tune.py", "bot": r.get("bot", "mybot"), "label": r.get("tag") or "",
                     "seeds": f.get("seeds"), "deals": f.get("deals"),
                     "points": f.get("total_points"), "max_points": f.get("max_points"),
                     "overridden": bool(r.get("params_overridden")), "opponents": opps,
                     "healthy": (r.get("health") or {}).get("safe", True)})
    path = os.path.join(DATA_DIR, "runs.jsonl")
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    for n, line in enumerate(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        base = {"id": f"r-{n}", "timestamp": r.get("timestamp", ""), "owner": r.get("owner", ""),
                "kind": r.get("kind", "field"), "tool": "fastarena.py" if r.get("kind") != "stress" else "stress_v1.py",
                "bot": str(r.get("bot", "")).removesuffix(".py"), "seeds": r.get("seeds"), "deals": r.get("deals"),
                "points": r.get("points"), "max_points": r.get("max_points"), "label": "", "opponents": {},
                "healthy": True}
        if r.get("kind") == "stress":
            base.update(label="time bank stress test", passed=r.get("passed"), worst_bank=r.get("worst_bank"),
                        bank=r.get("bank"), healthy=bool(r.get("passed")))
        elif r.get("kind") == "ab":
            base.update(label=f"vs {r.get('vs', '')}", vs=r.get("vs"), points_vs=r.get("points_vs"),
                        head_to_head=r.get("head_to_head"))
        for x in r.get("rows") or []:
            base["opponents"][x["opponent"]] = {k: x.get(k) for k in ("w", "c", "l", "points", "bb", "max_s", "flags",
                                                                       "matches", "buckets", "worst")}
            base["opponents"][x["opponent"]]["ci"] = None
            if x.get("flags"):
                base["healthy"] = False
        runs.append(base)
    for r in runs:
        if r["opponents"]:
            r["families"], r["weighted"] = _families_of(r["opponents"])
    runs.sort(key=lambda r: r["timestamp"])
    return runs


@app.get("/api/runs")
def api_runs():
    """Every finished run (tune.py, fastarena, stress), newest first, in one shape."""
    import families
    fam = families.families()
    return JSONResponse({"runs": _all_runs()[::-1][:200],
                         "family_of": {o: k for k, v in fam.items() for o in v},
                         "family_names": families.FAMILY_NAMES, "weights": families.FIELD_WEIGHT})


@app.get("/api/changelog")
def api_changelog():
    """Every recorded iteration (changelog.json, written by tools/changelog.py), newest first."""
    path = os.path.join(ROOT, "changelog.json")
    try:
        with open(path) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    return list(reversed(entries))


@app.get("/api/last-log")
def api_last_log():
    path = os.path.join(DATA_DIR, "last_run.log")
    if not os.path.exists(path):
        return {"log": ""}
    with open(path) as f:
        return {"log": f.read()[-4000:]}


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    # Read on every request, so the page can change without restarting the server.
    with open(UI_PATH) as f:
        return f.read()


if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("DASH_PORT", 8050))
    print(f"\n  🃏 PokerBot Dashboard → http://localhost:{port}\n")
    webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

