"""tools/stress_v1.py - can the shipped bot forfeit? Real judge, real HTTP, under CPU load.

Plays agent.py (the shipped bot) against a baseline server through judge_engine.py, exactly as
the tournament does, while busy-loop processes compete for the CPU. With K burners per core,
our server gets roughly 1/(K+1) of a core: a stand-in for a slower tournament machine.

Reports, per half: thinking time charged to the bank (of 8 s), errors (forfeit at 5),
whether the bank ran out, and the slowest single request (limit 1 s).

    .venv/bin/python tools/stress_v1.py                       # loads 0, 1, 2 burners per core
    .venv/bin/python tools/stress_v1.py --load 3 --opp baseline_calling_station --seeds 2

Takes the shared run queue. Exit code 1 if any half breaks the safety bar.
"""
import argparse
import json
import multiprocessing as mp
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.runlock import run_lock  # noqa: E402

PY = sys.executable
BANK = 8.0
SAFE_SHARE = 0.6          # a half must use under 60% of the bank, so a slower box still fits


def _burn():
    while True:
        pass


def _wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def _serve(script, port):
    env = dict(os.environ, PORT=str(port))
    p = subprocess.Popen([PY, script], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_port(port):
        p.kill()
        raise RuntimeError(f"{script} did not start on {port}")
    return p


def play(opp, seed, deals, out):
    me = _serve("agent.py", 5108)
    them = _serve(os.path.join("baseline-bots", opp + ".py"), 5109)
    try:
        subprocess.run([PY, "judge_engine.py", "--agent1", "5108", "--agent2", "5109",
                        "--seed", str(seed), "--deals", str(deals), "--json", out],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL, timeout=900)
    finally:
        me.kill()
        them.kill()
    with open(out) as f:
        return json.load(f)


def agent1_halves(result):
    """[(time_seconds, errors, timed_out, forfeited, wall_seconds)] for our bot, per half."""
    return [(h["time_seconds"], h["errors"], h["timed_out"], h["forfeited"], h["wall_seconds"])
            for h in result["agent_stats"]["agent1"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", type=int, nargs="*", default=[0, 1, 2], help="burners per core")
    ap.add_argument("--opp", default="baseline_calling_station")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--deals", type=int, default=1000)
    a = ap.parse_args()

    tmp = os.path.join(ROOT, "data", "stress")
    os.makedirs(tmp, exist_ok=True)
    ok = True
    rows = []
    with run_lock(owner="stress_v1.py", tag=f"forfeit check vs {a.opp}"):
        for load in a.load:
            burners = [mp.Process(target=_burn, daemon=True) for _ in range(load * os.cpu_count())]
            for b in burners:
                b.start()
            try:
                for seed in range(1, a.seeds + 1):
                    out = os.path.join(tmp, f"load{load}_seed{seed}.json")
                    res = play(a.opp, seed, a.deals, out)
                    halves = agent1_halves(res)
                    ok &= len(halves) == 2
                    for i, (t, err, to, ff, wall) in enumerate(halves, 1):
                        safe = err == 0 and not to and not ff and t < SAFE_SHARE * BANK
                        ok &= safe
                        rows.append({"load": load, "seed": seed, "half": i, "bank_used": round(t, 3),
                                     "errors": err, "bank_empty": to, "forfeit": ff, "safe": safe})
                        print(f"load {load}/core  seed {seed}  half {i}: bank used {t:5.2f}s of {BANK:g} "
                              f"({t / BANK:4.0%})  errors {err}  bank empty {to}  forfeit {ff}"
                              f"  (wall {wall:.1f}s)  {'OK' if safe else 'UNSAFE'}",
                              flush=True)
            finally:
                for b in burners:
                    b.terminate()
    with open(os.path.join(tmp, "latest.json"), "w") as f:
        json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "opponent": a.opp,
                   "deals": a.deals, "bank": BANK, "safe_share": SAFE_SHARE, "passed": ok, "rows": rows}, f)
    from tools.runlock import log_run
    worst = max((r["bank_used"] for r in rows), default=0)
    log_run({"kind": "stress", "bot": "agent.py", "passed": ok, "worst_bank": worst, "bank": BANK,
             "loads": a.load, "opponent": a.opp})
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
