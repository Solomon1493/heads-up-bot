"""tools/runlock.py - one measurement run at a time, shared by every tool and agent.

Every match-playing job (tune.py run/sweep, tools/trace_v2.py, dashboard buttons) takes this
lock first. Two jobs at once would both use every CPU core; both bots size their Monte Carlo
by measured time, so they would play worse and both results would be skewed. Instead, the
second job waits its turn and says who it is waiting for.

The lock is data/run.lock (JSON: owner, tag, pid, started). A lock whose process has died is
stale and is taken over automatically.

    from tools.runlock import run_lock
    with run_lock(owner="tune.py", tag="sweep mybot_v2"):
        ...play matches...
"""
import contextlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "data", "run.lock")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def holder():
    """The current lock holder as a dict, or None if free (stale locks count as free)."""
    try:
        with open(LOCK_PATH) as f:
            info = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not _alive(int(info.get("pid", 0))):
        return None
    info["elapsed_seconds"] = round(time.time() - info.get("started", time.time()), 1)
    return info


def _try_take(owner, tag):
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    info = {"owner": owner, "tag": tag, "pid": os.getpid(), "started": time.time()}
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if holder() is None:                       # stale: its process is gone
            try:
                os.remove(LOCK_PATH)
            except FileNotFoundError:
                pass
            return _try_take(owner, tag)
        return False
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    return True


@contextlib.contextmanager
def run_lock(owner, tag="", poll=3.0):
    announced = False
    while not _try_take(owner, tag):
        if not announced:
            h = holder() or {}
            print(f"[queue] waiting for {h.get('owner', '?')} ({h.get('tag', '')}) to finish...",
                  file=sys.stderr, flush=True)
            announced = True
        time.sleep(poll)
    try:
        yield
    finally:
        try:
            with open(LOCK_PATH) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(LOCK_PATH)
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def set_progress(done, total, what=""):
    """Record progress on the lock this process holds, so the dashboard can show it."""
    try:
        with open(LOCK_PATH) as f:
            info = json.load(f)
        if info.get("pid") != os.getpid():
            return
        info.update(done=done, total=total, what=what)
        tmp = LOCK_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(info, f)
        os.replace(tmp, LOCK_PATH)
    except (OSError, ValueError):
        pass


RUNS_PATH = os.path.join(ROOT, "data", "runs.jsonl")


def log_run(record):
    """Append one finished run (any tool, any agent) to data/runs.jsonl for the dashboard."""
    try:
        record = dict(record, timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                      owner=record.get("owner") or os.environ.get("RUN_OWNER", "terminal"))
        os.makedirs(os.path.dirname(RUNS_PATH), exist_ok=True)
        with open(RUNS_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def code_hash(spec_path):
    """Short fingerprint of a bot file, so a replay can tell whether the code changed since a run."""
    import hashlib
    try:
        with open(spec_path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return ""


def trace_cache_path(bot, opponent, seed):
    safe = lambda x: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(x))
    return os.path.join(ROOT, "data", "trace", "cache", f"{safe(bot)}__{safe(opponent)}__{safe(seed)}.json")
