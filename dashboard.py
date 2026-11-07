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


@app.get("/api/status")
def api_status():
    global CURRENT_JOB
    if CURRENT_JOB["running"] and CURRENT_JOB["proc"]:
        ret = CURRENT_JOB["proc"].poll()
        if ret is not None:
            CURRENT_JOB["running"] = False
            CURRENT_JOB["proc"] = None
    elapsed = round(time.time() - CURRENT_JOB["start_time"], 1) if CURRENT_JOB["running"] and CURRENT_JOB["start_time"] else 0
    return {
        "running": CURRENT_JOB["running"],
        "tag": CURRENT_JOB["tag"],
        "elapsed_seconds": elapsed,
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
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=run_log_file, stderr=subprocess.STDOUT)
    
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

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🃏 PokerBot Tuning Dashboard</title>
<style>
:root {
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d; --border: #30363d;
  --text: #e6edf3; --text2: #8b949e; --accent: #58a6ff; --green: #3fb950;
  --red: #f85149; --yellow: #d29922; --orange: #db6d28; --purple: #bc8cff;
  --font: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Layout */
.header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 18px; font-weight: 600; }
.header h1 span { font-size: 22px; margin-right: 8px; }
.header-right { display: flex; gap: 12px; align-items: center; }
.status-pill { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.status-safe { background: rgba(63,185,80,0.15); color: var(--green); }
.status-unsafe { background: rgba(248,81,73,0.15); color: var(--red); }

.container { max-width: 1400px; margin: 0 auto; padding: 20px 24px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.full-width { grid-column: 1 / -1; }

/* Cards */
.card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.card-header { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.card-header h2 { font-size: 14px; font-weight: 600; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; }
.card-body { padding: 16px; }

/* Metrics */
.metric { text-align: center; padding: 16px; }
.metric-value { font-size: 32px; font-weight: 700; font-family: var(--font); }
.metric-label { font-size: 11px; color: var(--text2); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.metric-sub { font-size: 12px; color: var(--text2); margin-top: 2px; }

/* Table */
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th { text-align: left; padding: 8px 12px; color: var(--text2); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
.data-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
.data-table tr:hover { background: var(--bg3); }
.data-table .num { font-family: var(--font); text-align: right; }
.win { color: var(--green); } .loss { color: var(--red); } .chop { color: var(--yellow); }

/* Code viewer */
.code-viewer { background: #0d1117; border: 1px solid var(--border); border-radius: 6px; overflow: auto; max-height: 500px; font-family: var(--font); font-size: 12px; line-height: 1.6; }
.code-line { display: flex; white-space: pre; }
.code-line:hover { background: rgba(88,166,255,0.1); }
.line-num { color: var(--text2); min-width: 50px; text-align: right; padding-right: 16px; user-select: none; border-right: 1px solid var(--border); margin-right: 12px; }
.diff-add { background: rgba(63,185,80,0.15); }
.diff-add .line-num { color: var(--green); }
.diff-del { background: rgba(248,81,73,0.15); }
.diff-del .line-num { color: var(--red); }
.diff-hunk { background: rgba(88,166,255,0.1); color: var(--accent); }

/* Chart */
.chart-container { position: relative; height: 200px; padding: 8px 0; }
.chart-bar-row { display: flex; align-items: center; margin: 4px 0; gap: 8px; }
.chart-label { width: 120px; text-align: right; font-size: 12px; color: var(--text2); flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chart-bar-wrap { flex: 1; display: flex; height: 28px; border-radius: 4px; overflow: hidden; background: var(--bg3); }
.chart-bar { height: 100%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; font-family: var(--font); transition: width 0.3s; }
.bar-win { background: var(--green); color: #0d1117; }
.bar-chop { background: var(--yellow); color: #0d1117; }
.bar-loss { background: var(--red); color: #0d1117; }
.chart-value { width: 80px; text-align: right; font-size: 12px; font-family: var(--font); flex-shrink: 0; }

/* Score timeline (SVG) */
.timeline-svg { width: 100%; height: 160px; }

/* Controls */
.btn { padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg3); color: var(--text); font-size: 13px; cursor: pointer; font-family: var(--sans); transition: all 0.15s; }
.btn:hover { background: var(--border); }
.btn-primary { background: rgba(88,166,255,0.15); border-color: var(--accent); color: var(--accent); }
.btn-primary:hover { background: rgba(88,166,255,0.3); }
.btn-green { background: rgba(63,185,80,0.15); border-color: var(--green); color: var(--green); }
input[type="text"], input[type="number"], select { padding: 6px 12px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg3); color: var(--text); font-size: 13px; font-family: var(--sans); }
input:focus, select:focus { outline: none; border-color: var(--accent); }
.form-row { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
.form-label { font-size: 12px; color: var(--text2); min-width: 60px; }

/* Params table */
.params-table { width: 100%; }
.params-table td { padding: 4px 8px; font-size: 13px; }
.params-table .param-name { font-family: var(--font); color: var(--accent); }
.params-table .param-val { font-family: var(--font); }
.params-table input { width: 80px; text-align: right; }

/* Tooltip */
[data-tip] { position: relative; cursor: help; border-bottom: 1px dashed var(--text2); }
[data-tip]:hover::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: var(--bg3); border: 1px solid var(--border); padding: 6px 10px; border-radius: 6px; font-size: 12px; color: var(--text); white-space: nowrap; max-width: 300px; white-space: normal; z-index: 200; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }

/* Tabs */
.tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); }
.tab { padding: 8px 16px; font-size: 13px; color: var(--text2); cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.15s; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Empty state */
.empty { text-align: center; padding: 48px 24px; color: var(--text2); }
.empty-icon { font-size: 48px; margin-bottom: 12px; }

/* Responsive */
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } .grid-3 { grid-template-columns: 1fr; } }

/* Sparkline */
.sparkline { display: inline-flex; align-items: flex-end; gap: 2px; height: 30px; vertical-align: middle; }
.spark-bar { width: 6px; border-radius: 2px; background: var(--accent); transition: height 0.3s; }

/* Loading */
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
.loading { animation: pulse 1.5s ease-in-out infinite; }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text2); }
</style>
</head>
<body>

<div class="header">
  <h1><span>🃏</span> PokerBot Tuning Dashboard</h1>
  <div class="header-right">
    <span id="last-refresh" style="font-size:12px;color:var(--text2)"></span>
    <button class="btn" onclick="refresh()">↻ Refresh</button>
    <button class="btn btn-primary" onclick="showRunDialog()">▶ New Run</button>
  </div>
</div>

<div class="container">
  <!-- Live Status Banner -->
  <div id="live-status-banner" style="display:none;background:rgba(88,166,255,0.12);border:1px solid var(--accent);border-radius:8px;padding:12px 18px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;">
    <div style="display:flex;align-items:center;gap:12px">
      <div class="loading" style="font-size:20px">🔄</div>
      <div>
        <div id="live-status-title" style="font-weight:600;color:var(--accent);font-size:14px">Evaluating Field Matches...</div>
        <div id="live-status-sub" style="font-size:12px;color:var(--text2)">Tag: dashboard-run | Elapsed: 0s</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--text2);background:var(--bg3);padding:4px 10px;border-radius:12px">Auto-refreshing live</div>
  </div>

  <!-- Top metrics -->
  <div class="grid-3" id="metrics-row">
    <div class="card"><div class="card-body metric">
      <div class="metric-value" id="m-score">—</div>
      <div class="metric-label" data-tip="Percentage of possible tournament points won across tested opponents. 100% means winning all matches.">Win Rate (% Points)</div>
      <div class="metric-sub" id="m-score-sub"></div>
    </div></div>
    <div class="card"><div class="card-body metric">
      <div class="metric-value" id="m-time">—</div>
      <div class="metric-label" data-tip="Peak thinking time in any half. Budget is 8.0s total; above 4.0s risks timeout on the slower tournament machine.">Max Think Time</div>
      <div class="metric-sub" id="m-time-sub"></div>
    </div></div>
    <div class="card"><div class="card-body metric">
      <div class="metric-value" id="m-runs">—</div>
      <div class="metric-label" data-tip="Total experiment runs logged. More data = higher confidence in improvements.">Total Runs</div>
      <div class="metric-sub" id="m-runs-sub"></div>
    </div></div>
  </div>

  <!-- Score Timeline + Opponent Breakdown -->
  <div class="grid">
    <div class="card">
      <div class="card-header"><h2>📈 Win Rate Over Time (% Points)</h2></div>
      <div class="card-body">
        <canvas id="timeline-canvas" style="width:100%;height:180px;"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><h2>🎯 Opponent Breakdown</h2><span id="opponent-card-subtitle" style="font-size:11px;color:var(--accent);font-weight:600">(latest run)</span></div>
      <div class="card-body" id="opponent-bars"></div>
    </div>
  </div>

  <!-- Tabs: Experiments | Code | Parameters | Agent Log | Help -->
  <div class="card full-width">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('experiments')">📋 Experiments</div>
      <div class="tab" onclick="switchTab('agent')">🤖 Agent Log</div>
      <div class="tab" onclick="switchTab('code')">💻 Code Viewer</div>
      <div class="tab" onclick="switchTab('params')">⚙️ Parameters</div>
      <div class="tab" onclick="switchTab('help')">❓ Glossary</div>
    </div>

    <!-- Experiments Tab -->
    <div class="tab-content active" id="tab-experiments">
      <div class="card-body">
        <table class="data-table" id="exp-table">
          <thead><tr>
            <th>Run</th><th>Time</th><th>Tag</th><th>Code</th>
            <th data-tip="Expected tournament points — the single number the tournament ranks on.">Score</th>
            <th data-tip="True if no errors, no forfeits, no timeouts, and peak time under 4s.">Safe</th>
            <th data-tip="Peak thinking time across all opponents.">Time</th>
            <th>Sparkline</th>
            <th>Actions</th>
          </tr></thead>
          <tbody id="exp-tbody"></tbody>
        </table>
        <div class="empty" id="exp-empty" style="display:none">
          <div class="empty-icon">🔬</div>
          <p>No experiments yet. Click <b>▶ New Run</b> to start!</p>
        </div>
      </div>
    </div>

    <!-- Agent Activity Tab -->
    <div class="tab-content" id="tab-agent">
      <div class="card-body">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div>
            <h3 style="color:var(--accent);font-size:14px">🤖 Agent Activity & Reasoning Log</h3>
            <p style="color:var(--text2);font-size:12px">Live record of what the AI assistant is modifying, why, and what was diagnosed.</p>
          </div>
          <button class="btn" onclick="renderAgentActivity()">↻ Refresh Log</button>
        </div>
        <div id="agent-activity-list"></div>
      </div>
    </div>

    <!-- Code Tab -->
    <div class="tab-content" id="tab-code">
      <div class="card-body">
        <div class="form-row">
          <span class="form-label">Compare:</span>
          <select id="diff-from" onchange="loadDiff()"><option value="">Select version...</option></select>
          <span style="color:var(--text2)">→</span>
          <select id="diff-to" onchange="loadDiff()"><option value="">Select version...</option></select>
          <button class="btn" onclick="loadCurrentSource()">View Current</button>
        </div>
        <div id="code-display" class="code-viewer" style="margin-top:12px">
          <div style="padding:24px;color:var(--text2);text-align:center">Select two versions to compare, or click "View Current" to see mybot.py</div>
        </div>
      </div>
    </div>

    <!-- Parameters Tab -->
    <div class="tab-content" id="tab-params">
      <div class="card-body">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div>
            <h3 style="color:var(--accent);font-size:14px">⚙️ Human-in-the-Loop Threshold Controls</h3>
            <p style="color:var(--text2);font-size:12px">Test runs the field with these values and leaves mybot.py untouched. Save writes them into mybot.py; they only reach the tournament once committed.</p>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-green" onclick="testParams()">🧪 Test without saving</button>
            <button class="btn btn-primary" onclick="saveParams(false)">💾 Save to mybot.py</button>
          </div>
        </div>
        <div id="params-status" style="margin-bottom:12px;font-size:13px;display:none;padding:8px 12px;border-radius:6px"></div>
        <table class="params-table" id="params-table">
          <thead><tr><th>Parameter</th><th>Value</th><th>Description</th></tr></thead>
          <tbody id="params-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Help Tab -->
    <div class="tab-content" id="tab-help">
      <div class="card-body">
        <h3 style="margin-bottom:12px;color:var(--accent)">Glossary for Teammates</h3>
        <div id="glossary-content"></div>
      </div>
    </div>
  </div>
</div>

<!-- Run Dialog (modal) -->
<div id="run-dialog" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;display:none;align-items:center;justify-content:center;">
  <div class="card" style="width:420px;">
    <div class="card-header"><h2>▶ New Experiment Run</h2><button class="btn" onclick="hideRunDialog()" style="font-size:18px;padding:2px 8px">×</button></div>
    <div class="card-body">
      <div class="form-row"><span class="form-label">Tag:</span><input type="text" id="run-tag" value="" placeholder="e.g. 'added bluff logic'" style="flex:1"></div>
      <div class="form-row"><span class="form-label">Seeds:</span><input type="number" id="run-seeds" value="1" min="1" max="10" style="width:60px"></div>
      <div class="form-row"><span class="form-label">Deals:</span><input type="number" id="run-deals" value="500" min="100" max="2000" step="100" style="width:80px"></div>
      <div class="form-row" style="margin-top:16px;justify-content:flex-end">
        <button class="btn" onclick="hideRunDialog()">Cancel</button>
        <button class="btn btn-green" onclick="triggerRun()">🚀 Run Experiment</button>
      </div>
      <div id="run-status" style="margin-top:8px;font-size:12px;color:var(--text2)"></div>
    </div>
  </div>
</div>

<script>
let experiments = [];
let autoRefreshTimer = null;

const GLOSSARY = [
  {term: "Expected Points", def: "How many tournament points you'd earn in a round-robin. Each opponent match is worth 1 point (win), 0.5 (chop), or 0 (loss). This is THE number the tournament ranks on."},
  {term: "bb/deal", def: "Big blinds won per deal (1 deal = 2 hands, one from each seat). Positive means you're profiting. A top bot might average +2 to +5 bb/deal vs weak opponents."},
  {term: "CI Half-Width", def: "Half the width of the 95% confidence interval. If your mean minus this is above 0, you win. A narrower CI makes wins easier to prove statistically."},
  {term: "Chop", def: "NOT a tie you earned. It means the result was too close to call with 95% statistical confidence. You get 0.5 points instead of 1.0. Reducing variance (tighter play) makes chops less likely."},
  {term: "Time Bank", def: "You get 8 seconds of thinking time for the entire half (1,000 hands). If you run out, every remaining decision auto-folds or auto-checks. Max think time should stay under 4s for safety margin."},
  {term: "Forfeit", def: "If your bot crashes 5 times in one half, you immediately lose the entire match. This is the worst possible outcome. Zero errors is mandatory."},
  {term: "Equity", def: "Your probability of winning the hand if all remaining cards are dealt randomly and there's a showdown. Calculated by simulating random outcomes (Monte Carlo)."},
  {term: "Pot Odds", def: "The ratio of the bet you must call to the total pot. If pot odds < equity, calling is profitable in the long run."},
  {term: "Safe ✅", def: "Means: zero errors, no forfeits, no timeouts, and peak thinking time under 4.0 seconds. You should NEVER submit an unsafe bot."},
  {term: "Seed", def: "A random number that determines the card deals. Same seed = same cards. Using multiple seeds prevents you from overfitting to one particular sequence of hands."},
  {term: "Short Deck", def: "A 36-card deck (2-5 removed). Flush beats Full House, Three-of-a-Kind beats Straight. Aces play high or low (A-6-7-8-9 is a straight)."},
  {term: "PLO5 (Pot-Limit Omaha 5-card)", def: "Each player gets 5 hole cards. You MUST use exactly 2 hole cards + 3 board cards to make your hand. Pot-limit means the max raise equals the current pot size."},
];

async function fetchExperiments() {
  const res = await fetch('/api/experiments');
  experiments = await res.json();
  return experiments;
}

async function refresh() {
  await fetchExperiments();
  render();
  document.getElementById('last-refresh').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function render() {
  renderMetrics();
  renderOpponentBars();
  renderTimeline();
  renderExperimentsTable();
  renderDiffDropdowns();
  renderParams();
  renderGlossary();
}

let selectedIdx = -1;
let chartPoints = [];

function selectExperiment(idx) {
  selectedIdx = idx;
  renderMetrics();
  renderOpponentBars();
  renderTimeline();
  renderExperimentsTable();
  const exp = experiments[idx];
  if (exp) {
    const el = document.getElementById('last-refresh');
    if (el) el.textContent = `Viewing Run #${idx+1} (${exp.tag || exp.id})`;
  }
}

function renderMetrics() {
  const n = experiments.length;
  document.getElementById('m-runs').textContent = n;

  if (n === 0) return;
  const activeIdx = (selectedIdx >= 0 && selectedIdx < n) ? selectedIdx : n - 1;
  const targetExp = experiments[activeIdx];
  const isLatest = activeIdx === n - 1;

  document.getElementById('m-runs-sub').textContent = isLatest
    ? `Latest Run #${n}: ${targetExp.tag || targetExp.id}`
    : `Viewing Run #${activeIdx+1} of ${n} (Click chart to change)`;

  const f = targetExp.field;
  const h = targetExp.health;
  const maxPts = Math.max(1, f.max_points);
  const pct = (f.total_points / maxPts) * 100;

  document.getElementById('m-score').textContent = pct.toFixed(1) + '%';
  document.getElementById('m-score').style.color = pct >= 80 ? 'var(--green)' : pct >= 60 ? 'var(--yellow)' : 'var(--red)';
  document.getElementById('m-score-sub').textContent = `${f.total_points.toFixed(2)} of ${f.max_points} pts ${isLatest ? '' : '[Run #' + (activeIdx+1) + ']'}`;

  document.getElementById('m-time').textContent = h.max_thinking_s.toFixed(2) + 's';
  document.getElementById('m-time').style.color = h.max_thinking_s < 3.0 ? 'var(--green)' : h.max_thinking_s < 4.0 ? 'var(--yellow)' : 'var(--red)';
  document.getElementById('m-time-sub').textContent = `of 8.0s bank`;
}

function renderOpponentBars() {
  const el = document.getElementById('opponent-bars');
  const subEl = document.getElementById('opponent-card-subtitle');
  if (experiments.length === 0) { el.innerHTML = '<div class="empty"><div class="empty-icon">🎯</div><p>Run an experiment first</p></div>'; return; }
  
  const activeIdx = (selectedIdx >= 0 && selectedIdx < experiments.length) ? selectedIdx : experiments.length - 1;
  const targetExp = experiments[activeIdx];
  const isLatest = activeIdx === experiments.length - 1;

  if (subEl) {
    subEl.textContent = isLatest 
      ? `(Latest Run #${activeIdx+1}: ${targetExp.tag || targetExp.id})`
      : `(Selected Run #${activeIdx+1}: ${targetExp.tag || targetExp.id})`;
  }

  const opps = targetExp.field.opponents;
  const sorted = Object.entries(opps).sort((a,b) => b[1].points - a[1].points);

  let html = '';
  for (const [name, d] of sorted) {
    const total = d.wins + d.chops + d.losses;
    const wPct = (d.wins / total * 100).toFixed(0);
    const cPct = (d.chops / total * 100).toFixed(0);
    const lPct = (d.losses / total * 100).toFixed(0);
    const bbStr = (d.mean_bb_deal >= 0 ? '+' : '') + d.mean_bb_deal.toFixed(2);
    const bbColor = d.mean_bb_deal > 0 ? 'var(--green)' : d.mean_bb_deal < 0 ? 'var(--red)' : 'var(--text2)';
    html += `<div class="chart-bar-row">
      <div class="chart-label">${name}</div>
      <div class="chart-bar-wrap">
        ${d.wins ? `<div class="chart-bar bar-win" style="width:${wPct}%">${d.wins}W</div>` : ''}
        ${d.chops ? `<div class="chart-bar bar-chop" style="width:${cPct}%">${d.chops}C</div>` : ''}
        ${d.losses ? `<div class="chart-bar bar-loss" style="width:${lPct}%">${d.losses}L</div>` : ''}
      </div>
      <div class="chart-value" style="color:${bbColor}">${bbStr} bb</div>
    </div>`;
  }
  el.innerHTML = html;
}

function renderTimeline() {
  const canvas = document.getElementById('timeline-canvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  chartPoints = [];

  if (experiments.length === 0) {
    ctx.fillStyle = '#8b949e';
    ctx.textAlign = 'center';
    ctx.font = '13px sans-serif';
    ctx.fillText('Score timeline will appear after experiments run', W/2, H/2);
    return;
  }

  const activeIdx = (selectedIdx >= 0 && selectedIdx < experiments.length) ? selectedIdx : experiments.length - 1;
  const pcts = experiments.map(e => {
    const max = Math.max(1, e.field.max_points || 1);
    return (e.field.total_points / max) * 100;
  });
  const pad = {t: 25, b: 35, l: 48, r: 25};
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;

  // Percentage Grid (0%, 25%, 50%, 75%, 100%)
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 1;
  for (let pctVal = 0; pctVal <= 100; pctVal += 25) {
    const py = pad.t + plotH - (pctVal / 100 * plotH);
    ctx.beginPath(); ctx.moveTo(pad.l, py); ctx.lineTo(W - pad.r, py); ctx.stroke();
    ctx.fillStyle = '#8b949e'; ctx.textAlign = 'right'; ctx.font = '11px monospace';
    ctx.fillText(pctVal + '%', pad.l - 6, py + 4);
  }

  if (pcts.length === 1) {
    const x = pad.l + plotW / 2, y = pad.t + plotH - (pcts[0] / 100 * plotH);
    const exp = experiments[0];
    chartPoints.push({
      x, y, index: 0, 
      pct: pcts[0], 
      score: exp.field.total_points, 
      max: exp.field.max_points, 
      tag: exp.tag || '#1'
    });
    ctx.fillStyle = '#58a6ff'; ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#e6edf3'; ctx.textAlign = 'center'; ctx.font = '12px monospace';
    ctx.fillText(`${pcts[0].toFixed(1)}% (${exp.field.total_points.toFixed(1)}/${exp.field.max_points})`, x, y - 10);
    return;
  }

  // Connecting Line
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2.5; ctx.lineJoin = 'round';
  ctx.beginPath();
  for (let i = 0; i < pcts.length; i++) {
    const x = pad.l + (i / (pcts.length - 1)) * plotW;
    const y = pad.t + plotH - (pcts[i] / 100 * plotH);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Gradient fill under line
  ctx.fillStyle = 'rgba(88,166,255,0.08)';
  ctx.lineTo(pad.l + plotW, pad.t + plotH);
  ctx.lineTo(pad.l, pad.t + plotH);
  ctx.closePath(); ctx.fill();

  // Draw interactive nodes
  for (let i = 0; i < pcts.length; i++) {
    const x = pad.l + (i / (pcts.length - 1)) * plotW;
    const y = pad.t + plotH - (pcts[i] / 100 * plotH);
    const exp = experiments[i];
    const safe = exp.health.safe;
    const isSelected = (i === activeIdx);
    
    chartPoints.push({
      x, y, index: i, 
      pct: pcts[i], 
      score: exp.field.total_points, 
      max: exp.field.max_points, 
      tag: exp.tag || `#${i+1}`
    });

    // If selected, draw glowing ring
    if (isSelected) {
      ctx.strokeStyle = '#58a6ff';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(x, y, 10, 0, Math.PI * 2);
      ctx.stroke();

      ctx.fillStyle = 'rgba(88,166,255,0.3)';
      ctx.beginPath();
      ctx.arc(x, y, 10, 0, Math.PI * 2);
      ctx.fill();

      // Tooltip label above selected node
      ctx.fillStyle = '#e6edf3';
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(`${pcts[i].toFixed(1)}% (${exp.field.total_points.toFixed(1)}/${exp.field.max_points})`, x, y - 14);
    }

    ctx.fillStyle = safe ? '#3fb950' : '#f85149';
    ctx.beginPath();
    ctx.arc(x, y, isSelected ? 6 : 4.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // X axis labels
  ctx.fillStyle = '#8b949e'; ctx.textAlign = 'center'; ctx.font = '10px sans-serif';
  const step = Math.max(1, Math.floor(pcts.length / 8));
  for (let i = 0; i < pcts.length; i += step) {
    const x = pad.l + (i / (pcts.length - 1)) * plotW;
    const tag = (experiments[i].tag || `#${i+1}`).slice(0, 10);
    ctx.fillText(tag, x, H - 8);
  }

  // Attach canvas interactive events once
  if (!canvas.dataset.hasInteractiveEvents) {
    canvas.dataset.hasInteractiveEvents = 'true';
    canvas.addEventListener('click', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      for (const pt of chartPoints) {
        if (Math.hypot(pt.x - mx, pt.y - my) <= 16) {
          selectExperiment(pt.index);
          return;
        }
      }
    });

    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      let hit = false;
      for (const pt of chartPoints) {
        if (Math.hypot(pt.x - mx, pt.y - my) <= 16) {
          canvas.style.cursor = 'pointer';
          canvas.title = `Click to inspect Run #${pt.index+1}: ${pt.tag} (${pt.pct.toFixed(1)}% — ${pt.score.toFixed(2)}/${pt.max} pts)`;
          hit = true;
          break;
        }
      }
      if (!hit) {
        canvas.style.cursor = 'default';
        canvas.title = 'Click any point to inspect run breakdown';
      }
    });
  }
}

function renderExperimentsTable() {
  const tbody = document.getElementById('exp-tbody');
  const empty = document.getElementById('exp-empty');
  if (experiments.length === 0) { tbody.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';

  const activeIdx = (selectedIdx >= 0 && selectedIdx < experiments.length) ? selectedIdx : experiments.length - 1;

  let html = '';
  for (let i = experiments.length - 1; i >= 0; i--) {
    const e = experiments[i];
    const f = e.field, h = e.health;
    const safe = h.safe ? '<span class="win">✅</span>' : '<span class="loss">❌</span>';
    const ts = new Date(e.timestamp).toLocaleTimeString();
    const scoreColor = f.total_points >= f.max_points * 0.8 ? 'win' : f.total_points >= f.max_points * 0.6 ? 'chop' : 'loss';
    const isSelected = (i === activeIdx);
    const rowBg = isSelected ? 'background:rgba(88,166,255,0.18);border-left:4px solid var(--accent);' : '';

    // Sparkline: per-opponent points
    const vals = Object.values(f.opponents).map(o => o.points);
    const sparkHtml = vals.map(v => {
      const h = Math.max(4, v * 28);
      const c = v >= 0.8 ? 'var(--green)' : v >= 0.4 ? 'var(--yellow)' : 'var(--red)';
      return `<div class="spark-bar" style="height:${h}px;background:${c}"></div>`;
    }).join('');

    const expPct = ((f.total_points / Math.max(1, f.max_points)) * 100).toFixed(0);
    html += `<tr onclick="selectExperiment(${i})" style="cursor:pointer;${rowBg}" title="Click to view breakdown on dashboard">
      <td class="num">${i + 1}${isSelected ? ' ◀' : ''}</td>
      <td>${ts}</td>
      <td>${e.tag || '<span style="color:var(--text2)">—</span>'}</td>
      <td><code style="color:var(--accent);font-size:11px">${e.code_hash}</code></td>
      <td class="num ${scoreColor}">${expPct}% <span style="font-size:11px;color:var(--text2)">(${f.total_points.toFixed(1)}/${f.max_points})</span></td>
      <td>${safe}</td>
      <td class="num" style="color:${h.max_thinking_s < 3 ? 'var(--green)' : h.max_thinking_s < 4 ? 'var(--yellow)' : 'var(--red)'}">${h.max_thinking_s.toFixed(2)}s</td>
      <td><div class="sparkline">${sparkHtml}</div></td>
      <td><button class="btn" style="font-size:11px;padding:2px 8px" onclick="event.stopPropagation();showDetail(${i})">Detail</button></td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

function showDetail(idx) {
  selectExperiment(idx);
  const e = experiments[idx];
  const f = e.field;
  let msg = `Experiment #${idx+1}: ${e.tag || e.id}\n`;
  msg += `Code: ${e.code_hash} | Git: ${e.git.sha}${e.git.dirty ? '*' : ''}\n`;
  msg += `Score: ${f.total_points.toFixed(2)}/${f.max_points}\n\n`;
  msg += `Params: ${JSON.stringify(e.params, null, 2)}\n\n`;
  msg += `Opponents:\n`;
  for (const [name, d] of Object.entries(f.opponents)) {
    msg += `  ${name}: ${d.wins}W/${d.chops}C/${d.losses}L  ${d.mean_bb_deal >= 0 ? '+' : ''}${d.mean_bb_deal.toFixed(3)} bb/deal\n`;
  }
  if (e.git.mybot_diff) {
    msg += `\nGit diff:\n${e.git.mybot_diff}`;
  }
  alert(msg);
}

function renderDiffDropdowns() {
  const hashes = [...new Set(experiments.map(e => e.code_hash))];
  for (const id of ['diff-from', 'diff-to']) {
    const sel = document.getElementById(id);
    const cur = sel.value;
    sel.innerHTML = '<option value="">Select version...</option>';
    for (const h of hashes) {
      const exp = experiments.find(e => e.code_hash === h);
      const label = `${h} (${exp?.tag || exp?.id || '?'})`;
      sel.innerHTML += `<option value="${h}">${label}</option>`;
    }
    sel.value = cur;
  }
}

async function loadDiff() {
  const h1 = document.getElementById('diff-from').value;
  const h2 = document.getElementById('diff-to').value;
  const el = document.getElementById('code-display');
  if (!h1 || !h2) return;
  if (h1 === h2) { el.innerHTML = '<div style="padding:24px;color:var(--text2);text-align:center">Same version selected — no differences</div>'; return; }
  const res = await fetch(`/api/diff/${h1}/${h2}`);
  const data = await res.json();
  if (data.error) { el.innerHTML = `<div style="padding:24px;color:var(--red)">${data.error}</div>`; return; }
  renderDiff(el, data.diff);
}

async function loadCurrentSource() {
  const el = document.getElementById('code-display');
  const res = await fetch('/api/current-source');
  const data = await res.json();
  renderSource(el, data.source);
}

function renderSource(el, source) {
  const lines = source.split('\n');
  let html = '';
  for (let i = 0; i < lines.length; i++) {
    const escaped = lines[i].replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    html += `<div class="code-line"><span class="line-num">${i + 1}</span><span>${escaped}</span></div>`;
  }
  el.innerHTML = html;
}

function renderDiff(el, diff) {
  if (!diff) { el.innerHTML = '<div style="padding:24px;color:var(--text2);text-align:center">No differences</div>'; return; }
  const lines = diff.split('\n');
  let html = '';
  let lineNum = 0;
  for (const line of lines) {
    const escaped = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    let cls = '';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    lineNum++;
    html += `<div class="code-line ${cls}"><span class="line-num">${lineNum}</span><span>${escaped}</span></div>`;
  }
  el.innerHTML = html;
}

async function renderParams() {
  const res = await fetch('/api/current-params');
  const data = await res.json();
  const tbody = document.getElementById('params-tbody');
  const descriptions = {
    pf_btn_raise: "On the button: preflop hand strength (0-1) needed to raise pot. Higher = fewer raises.",
    pf_btn_limp: "On the button: strength needed to complete the 1-chip small blind instead of folding.",
    pf_btn_call_raise: "On the button: strength needed to call when the big blind re-raises.",
    pf_bb_raise: "In the big blind: strength needed to raise pot preflop.",
    pf_bb_call: "In the big blind: strength needed to call a button raise.",
    eq_raise_pot: "Postflop equity threshold for a pot-size raise. The 'value bet' cutoff.",
    eq_raise_half: "Postflop equity to bet half-pot when checked to. The 'probe bet' cutoff.",
    eq_bluff_fold_rate: "Opponent fold rate that triggers bluff attempts.",
    eq_bluff_min: "Minimum equity to attempt a bluff (protects against calling stations).",
    aggro_margin: "Extra equity discount when facing an aggressive opponent (they bluff more, so call wider).",
    aggro_threshold: "Opponent aggression frequency above which the margin kicks in.",
    tight_threshold: "Opponent aggression below this triggers tight opponent exploitation.",
    tight_call_penalty: "Extra equity required to call a bet from a tight opponent.",
    tight_probe_min_eq: "Minimum equity to probe bet when checked to by a tight opponent.",
  };
  let html = '';
  for (const [k, v] of Object.entries(data.params || {})) {
    html += `<tr>
      <td class="param-name">${k}</td>
      <td class="param-val"><input type="number" step="0.01" value="${v}" id="param-${k}" style="width:80px;text-align:right"></td>
      <td style="color:var(--text2);font-size:12px">${descriptions[k] || ''}</td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

function paramStatus(kind, text) {
  const el = document.getElementById('params-status');
  const look = {info: ['rgba(88,166,255,0.15)', 'var(--accent)'],
                ok: ['rgba(63,185,80,0.15)', 'var(--green)'],
                err: ['rgba(248,81,73,0.15)', 'var(--red)']}[kind];
  el.style.display = 'block';
  el.style.background = look[0];
  el.style.color = look[1];
  el.textContent = text;
}

// Read every box; returns null (and shows why) if any value is blank or out of range.
function collectParams() {
  const out = {};
  for (const inp of document.querySelectorAll('#params-tbody input')) {
    const key = inp.id.replace('param-', '');
    const v = Number(inp.value);
    if (inp.value.trim() === '' || !Number.isFinite(v) || v < 0 || v > 1) {
      paramStatus('err', `${key} needs a number between 0 and 1. Nothing was saved or run.`);
      inp.focus();
      return null;
    }
    out[key] = v;
  }
  return out;
}

async function testParams() {
  const params = collectParams();
  if (!params) return;
  const res = await fetch('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tag: 'param-test', seeds: 1, deals: 500, params})
  });
  const data = await res.json();
  if (!res.ok) {
    paramStatus('err', data.error === undefined && data.status === 'already_running'
      ? 'A run is already in progress. Wait for it to finish, then test again.'
      : 'Test not started: ' + (data.error || data.status));
    return;
  }
  paramStatus('info', '🧪 Testing these values (mybot.py unchanged). Results appear in Experiments when done.');
  checkLiveStatus();
}

async function saveParams(runTest) {
  const newParams = collectParams();
  if (!newParams) return;
  const statusEl = document.getElementById('params-status');
  paramStatus('info', '💾 Saving parameters to mybot.py...');

  try {
    const res = await fetch('/api/save-params', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({params: newParams})
    });
    const data = await res.json();
    if (data.status === 'saved') {
      statusEl.style.background = 'rgba(63,185,80,0.15)';
      statusEl.style.color = 'var(--green)';
      statusEl.textContent = '✅ Parameters saved to mybot.py successfully!';
      setTimeout(() => { statusEl.style.display = 'none'; }, 4000);
      if (runTest) {
        triggerRunQuick('param-update');
      }
    } else {
      throw new Error(data.error || 'Failed to save');
    }
  } catch (err) {
    statusEl.style.background = 'rgba(248,81,73,0.15)';
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = '❌ Error saving: ' + err.message;
  }
}

async function renderAgentActivity() {
  const el = document.getElementById('agent-activity-list');
  try {
    const res = await fetch('/api/agent-activity');
    const items = await res.json();
    if (!items || items.length === 0) {
      el.innerHTML = '<div style="color:var(--text2);padding:24px;text-align:center">No agent activity logged yet.</div>';
      return;
    }
    let html = '';
    for (const item of items) {
      html += `<div style="background:var(--bg);border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:6px;padding:14px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div style="font-weight:600;color:var(--text);font-size:14px">${item.action}</div>
          <div style="font-size:11px;color:var(--text2)">${item.timestamp}</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:6px"><b style="color:var(--accent)">Reasoning:</b> ${item.why}</div>
        <div style="font-size:12px;color:var(--green);font-family:var(--font);background:var(--bg2);padding:8px;border-radius:4px;overflow-x:auto"><b>Changes:</b> ${item.changes}</div>
      </div>`;
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:var(--red)">Failed to load activity</div>';
  }
}

function renderGlossary() {
  const el = document.getElementById('glossary-content');
  let html = '';
  for (const g of GLOSSARY) {
    html += `<div style="margin-bottom:16px;padding:12px;background:var(--bg);border-radius:6px;border:1px solid var(--border)">
      <div style="font-weight:600;color:var(--accent);margin-bottom:4px">${g.term}</div>
      <div style="color:var(--text);font-size:13px;line-height:1.5">${g.def}</div>
    </div>`;
  }
  el.innerHTML = html;
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    t.classList.toggle('active', t.textContent.toLowerCase().includes(name.slice(0,4)));
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'agent') renderAgentActivity();
}

function showRunDialog() { document.getElementById('run-dialog').style.display = 'flex'; }
function hideRunDialog() { document.getElementById('run-dialog').style.display = 'none'; }

async function checkLiveStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    const banner = document.getElementById('live-status-banner');
    if (data.running) {
      banner.style.display = 'flex';
      document.getElementById('live-status-title').textContent = `Running Evaluation: ${data.tag}`;
      document.getElementById('live-status-sub').textContent = `Active | Elapsed: ${data.elapsed_seconds}s`;
    } else {
      if (banner.style.display === 'flex') {
        // Just finished
        banner.style.display = 'none';
        refresh();
      }
    }
  } catch (e) {}
}

async function triggerRunQuick(tag) {
  await fetch('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tag: tag || 'quick-test', seeds: 1, deals: 500})
  });
  checkLiveStatus();
}

async function triggerRun() {
  const tag = document.getElementById('run-tag').value;
  const seeds = parseInt(document.getElementById('run-seeds').value);
  const deals = parseInt(document.getElementById('run-deals').value);
  const statusEl = document.getElementById('run-status');
  statusEl.textContent = '🔄 Starting experiment...';

  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tag, seeds, deals})
    });
    const data = await res.json();
    statusEl.innerHTML = `✅ Experiment started (PID ${data.pid}). Results will appear after ~${seeds * 10}s.`;
    hideRunDialog();
    checkLiveStatus();
  } catch (e) {
    statusEl.textContent = '❌ Error: ' + e.message;
  }
}

// Init
window.addEventListener('resize', () => renderTimeline());
refresh();
renderAgentActivity();
// Check live status every 2 seconds, refresh data every 4 seconds
setInterval(checkLiveStatus, 2000);
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    return DASHBOARD_HTML


if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("DASH_PORT", 8050))
    print(f"\n  🃏 PokerBot Dashboard → http://localhost:{port}\n")
    webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

