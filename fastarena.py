"""fastarena.py - run the OFFICIAL judge logic in-process, with no HTTP servers.

Place this file in the root of the heads-up-bundle repository. It imports the official
game.py and judge_engine.py and replaces only the network layer, so dealing, rules,
duplicate halves, the time bank, the 5-error forfeit rule and the 95% CI verdict are the
judge's own code. Each half reloads the bot file, like the tournament's fresh process.

Bot specs: a file path (agent.py, mybot.py, zoo/aggro_bot.py) or a short name:
    random, half, pot, station          the official baseline bots
    any file name in zoo/               e.g. equity_bot, tight_bot

Commands
    python fastarena.py match mybot random --deals 1000 --seed 7
    python fastarena.py field mybot --seeds 5            # vs every zoo + baseline bot
    python fastarena.py tournament mybot agent.py equity_bot tight_bot --seeds 3
    python fastarena.py ab mybot equity_bot --seeds 5    # which version scores more points?

Verified: on identical seeds, results match the official HTTP judge exactly.
Caveat: real bots also pay JSON/HTTP overhead per request. Locally this stayed inside the
judge's 1 ms grace, but the tournament machine may be slower: keep a wide time margin.
"""
import argparse
import glob
import importlib.util
import json
import os
import sys
import time
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import judge_engine as J  # noqa: E402
from botlib import explain  # noqa: E402
from game import (Action, AgentConfig, HandResult, Observation, ACTION_TIMEOUT_SECONDS,  # noqa: E402
                  NETWORK_GRACE_SECONDS, NUM_DEALS, TIME_BANK_SECONDS, default_action)

BASELINES = {
    "random": "baseline-bots/baseline_random.py",
    "half": "baseline-bots/baseline_always_half.py",
    "pot": "baseline-bots/baseline_always_pot.py",
    "station": "baseline-bots/baseline_calling_station.py",
}
TIME_WARN_SECONDS = 4.0  # flag bots using more than half the bank (HTTP overhead + slower machine)


def resolve(spec):
    if spec in BASELINES:
        return os.path.join(ROOT, BASELINES[spec])
    for cand in (spec, spec + ".py", os.path.join("zoo", spec + ".py")):
        path = cand if os.path.isabs(cand) else os.path.join(ROOT, cand)
        if os.path.isfile(path):
            return path
    raise SystemExit(f"Unknown bot '{spec}'. Use a path, a zoo/ name, or one of {list(BASELINES)}.")


def zoo_specs():
    return list(BASELINES) + sorted(os.path.splitext(os.path.basename(p))[0]
                                    for p in glob.glob(os.path.join(ROOT, "zoo", "*.py"))
                                    if not os.path.basename(p).startswith("_"))


def load_module(path):
    name = f"_arena_bot_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def apply_params(mod, params):
    """Override a bot's live PARAMS dict (used by tune.py and --params).

    Strict on purpose: a typo or a bot with no PARAMS must fail loudly, otherwise a
    sweep would silently play the default bot and report meaningless numbers.
    """
    live = getattr(mod, "PARAMS", None)
    if not isinstance(live, dict):
        raise ValueError(f"{getattr(mod, '__file__', mod)} has no PARAMS dict, cannot apply overrides")
    unknown = sorted(set(params) - set(live))
    if unknown:
        raise ValueError(f"unknown parameter(s) {unknown}; valid: {sorted(live)}")
    live.update(params)


def _wire_answer(raw):
    """Mirror agent.py's plumbing: how a returned value is sent to the judge."""
    if isinstance(raw, Action):
        return raw.name
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    return repr(raw)


class InProcessAgent:
    """Drop-in replacement for judge_engine.HttpAgentHandle that calls a module directly."""

    def __init__(self, path, label, config=None, params=None):
        self.path, self.label = path, label
        self.params = params
        self.config = config or AgentConfig()
        self.stats = J.AgentStats()
        self._pending = []
        self.mod = None
        self.memory = {}
        self.load_seconds = 0.0

    def describe(self):
        return self.label

    # same bookkeeping as the judge
    def _record_error(self, message):
        self.stats.errors += 1
        if len(self.stats.error_messages) < J.MAX_STORED_ERRORS:
            self.stats.error_messages.append(message)
        if self.stats.errors >= J.ERROR_LIMIT:
            self.stats.forfeited = True

    def _remaining_bank(self):
        return self.config.time_bank_seconds - self.stats.time_seconds

    def _usable(self):
        if self.stats.forfeited:
            return False
        if self._remaining_bank() <= 0:
            self.stats.timed_out = True
            self.stats.skipped_calls += 1
            return False
        return True

    def _timed(self, label, fn):
        """Run fn like one HTTP request: charge time, enforce the 1 s and bank limits."""
        bank_left = self._remaining_bank() + NETWORK_GRACE_SECONDS
        limited_by_bank = bank_left < ACTION_TIMEOUT_SECONDS
        limit = max(0.001, min(ACTION_TIMEOUT_SECONDS, bank_left))
        start = time.perf_counter()
        exc_msg = None
        value = None
        try:
            value = fn()
        except Exception as exc:
            exc_msg = f"{label} raised {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        elapsed = time.perf_counter() - start
        charged = min(elapsed, limit)
        self.stats.wall_seconds += charged
        self.stats.time_seconds += max(0.0, charged - NETWORK_GRACE_SECONDS)
        self.stats.calls += 1
        if elapsed > limit:
            if limited_by_bank:
                self.stats.timed_out = True
            else:
                self._record_error(f"{label} took longer than {ACTION_TIMEOUT_SECONDS:g}s")
            return False, None, True
        if exc_msg:
            self._record_error(exc_msg)
            return False, None, False
        return True, value, False

    def _deliver(self, results, errors):
        for r in results:
            try:
                self.mod.on_hand_end(HandResult.from_dict(r.to_dict()), self.memory)
            except Exception as exc:
                errors.append(f"on_hand_end raised {type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    # what judge_engine.play_half calls
    def start_half(self):
        self.stats = J.AgentStats()
        self._pending = []
        t0 = time.perf_counter()
        try:
            self.mod = load_module(self.path)   # fresh process in the tournament
        except Exception as exc:
            self.stats.forfeited = True
            self._record_error(f"import failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            return
        self.load_seconds = max(self.load_seconds, time.perf_counter() - t0)
        if self.params:
            apply_params(self.mod, self.params)   # fresh module each half, so re-apply each half
        self.memory = {}
        cfg = AgentConfig.from_dict(self.config.to_dict())
        ok, _, _ = self._timed("new_half", lambda: self.mod.new_half(cfg, self.memory))
        if not ok:
            self.stats.forfeited = True

    def act(self, obs):
        fallback = default_action(obs.to_call)
        if not self._usable():
            return fallback
        results, self._pending = self._pending, []
        hand_errors = []

        def request():
            self._deliver(results, hand_errors)
            return self.mod.choose_action(Observation.from_dict(obs.to_dict()), self.memory)

        ok, raw, timed_out = self._timed("choose_action", request)
        if not timed_out:
            for m in hand_errors:
                self._record_error(m)
        if not ok:
            return fallback
        action = J.coerce_action(_wire_answer(raw))
        if action is None:
            self._record_error(f"choose_action returned {raw!r}, which is not an Action")
            return fallback
        if action not in obs.legal_actions:
            legal = ", ".join(a.name for a in obs.legal_actions)
            self._record_error(f"choose_action returned illegal action {action.name} (legal: {legal})")
            return fallback
        return action

    def hand_end(self, result):
        if not self.stats.forfeited:
            self._pending.append(result)

    def flush_hand_results(self):
        results, self._pending = self._pending, []
        if results and self._usable():
            errs = []
            _, _, timed_out = self._timed("on_hand_end", lambda: self._deliver(results, errs))
            if not timed_out:
                for m in errs:
                    self._record_error(m)


# ----------------------------------------------------------------------------- matches
def play(spec1, spec2, seed, deals, keep_logs=False, params=None):
    """`params` overrides spec1's PARAMS (spec1 only)."""
    a1 = InProcessAgent(resolve(spec1), spec1, params=params)
    a2 = InProcessAgent(resolve(spec2), spec2)
    h1 = J.play_half(a1, a2, seed, 1, deals, keep_logs)
    h2 = J.play_half(a1, a2, seed, 2, deals, keep_logs)
    res = J.combine_halves(h1, h2)
    return res, (a1.load_seconds, a2.load_seconds)


def _summary(spec1, spec2, seed, deals, params=None):
    res, loads = play(spec1, spec2, seed, deals, params=params)
    out = {"a": spec1, "b": spec2, "seed": seed, "mean": res.mean_bb, "ci": res.ci,
           "verdict": res.verdict, "points": res.points, "load": loads}
    for key, spec in (("agent1", spec1), ("agent2", spec2)):
        halves = res.agent_stats[key]
        out[key] = {
            "max_time": max(s.time_seconds for s in halves),
            "errors": sum(s.errors for s in halves),
            "forfeit": any(s.forfeited for s in halves),
            "bank_empty": any(s.timed_out for s in halves),
            "first_error": next((m for s in halves for m in s.error_messages), None),
        }
    return out


def _job(args):
    return _summary(*args)


def run_many(jobs, workers):
    if workers <= 1 or len(jobs) == 1:
        return [_job(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_job, jobs))


def _flags(st):
    f = []
    if st["forfeit"]:
        f.append("FORFEIT")
    if st["bank_empty"]:
        f.append("BANK EMPTY")
    if st["max_time"] > TIME_WARN_SECONDS:
        f.append("SLOW")
    if st["errors"]:
        f.append(f"{st['errors']} err")
    return ",".join(f)


def _health_lines(results):
    lines, seen = [], set()
    for r in results:
        for key in ("agent1", "agent2"):
            spec = r["a"] if key == "agent1" else r["b"]
            st = r[key]
            if st["first_error"] and spec not in seen:
                seen.add(spec)
                lines.append(f"\nFirst error from {spec}:\n{st['first_error'][:1500]}")
            if spec not in seen and r["load"][0 if key == 'agent1' else 1] > 10:
                lines.append(f"WARNING: {spec} takes {r['load'][0 if key == 'agent1' else 1]:.1f}s to import "
                             f"(the judge allows 15 s to start)")
    return lines


def cmd_match(a):
    params = json.loads(a.params) if a.params else None
    res, loads = play(a.bot1, a.bot2, a.seed, a.deals, keep_logs=bool(a.json), params=params)
    print(J.format_report(res))
    for spec, s in zip((a.bot1, a.bot2), loads):
        if s > 10:
            print(f"WARNING: {spec} takes {s:.1f}s to import (limit 15 s)")
    if a.json:
        with open(a.json, "w") as f:
            f.write(res.to_json())
    if a.explain:
        print("\n" + explain.legend_block("match report", explain.MATCH_LEGEND))


def field_table(bot, opponents, seeds, deals, workers):
    jobs = [(bot, opp, f"field-{opp}-{s}", deals) for opp in opponents for s in range(seeds)]
    results = run_many(jobs, workers)
    lines = [f"{bot} vs field: {seeds} match(es) per opponent, {deals} deals per half",
             f"{'opponent':22} {'W':>3} {'C':>3} {'L':>3} {'points':>7} {'bb/deal':>9} "
             f"{'CI half-width':>13} {'my max s':>8}  flags"]
    total_pts = 0.0
    for opp in opponents:
        rs = [r for r in results if r["b"] == opp]
        w = sum(r["points"]["agent1"] == 1.0 for r in rs)
        l = sum(r["points"]["agent1"] == 0.0 for r in rs)
        c = len(rs) - w - l
        pts = sum(r["points"]["agent1"] for r in rs) / len(rs)
        total_pts += pts
        mean = sum(r["mean"] for r in rs) / len(rs)
        hw = sum((r["ci"][1] - r["ci"][0]) / 2 for r in rs) / len(rs)
        mt = max(r["agent1"]["max_time"] for r in rs)
        flags = ";".join(sorted({_flags(r["agent1"]) for r in rs} - {""}))
        lines.append(f"{opp:22} {w:3d} {c:3d} {l:3d} {pts:7.2f} {mean:+9.3f} {hw:13.3f} {mt:8.2f}  {flags}")
    lines.append(f"Expected points vs this field: {total_pts:.2f} of {len(opponents)}")
    lines += _health_lines(results)
    return total_pts, "\n".join(lines)


def cmd_field(a):
    opps = a.opponents.split(",") if a.opponents else [z for z in zoo_specs() if z != a.bot]
    print(field_table(a.bot, opps, a.seeds, a.deals, a.workers)[1])
    if a.explain:
        print("\n" + explain.legend_block("field table", explain.FIELD_LEGEND))


def cmd_ab(a):
    opps = a.opponents.split(",") if a.opponents else [z for z in zoo_specs() if z not in (a.bot1, a.bot2)]
    p1, t1 = field_table(a.bot1, opps, a.seeds, a.deals, a.workers)
    p2, t2 = field_table(a.bot2, opps, a.seeds, a.deals, a.workers)
    print(t1 + "\n\n" + t2)
    res = run_many([(a.bot1, a.bot2, f"ab-{s}", a.deals) for s in range(a.seeds)], a.workers)
    hh = sum(r["points"]["agent1"] for r in res) / len(res)
    print(f"\nHead to head: {a.bot1} scores {hh:.2f} points per match vs {a.bot2}")
    print(f"Field points: {a.bot1} {p1:.2f} vs {a.bot2} {p2:.2f}  "
          f"(difference {p1 - p2:+.2f}; with few seeds, treat <0.5 as noise)")


def cmd_tournament(a):
    bots = a.bots
    pairs = [(x, y) for i, x in enumerate(bots) for y in bots[i + 1:]]
    jobs = [(x, y, f"tour-{x}-{y}-{s}", a.deals) for x, y in pairs for s in range(a.seeds)]
    results = run_many(jobs, a.workers)
    pts = {b: 0.0 for b in bots}
    bb = {b: [] for b in bots}
    for r in results:
        pts[r["a"]] += r["points"]["agent1"] / a.seeds
        pts[r["b"]] += r["points"]["agent2"] / a.seeds
        bb[r["a"]].append(r["mean"])
        bb[r["b"]].append(-r["mean"])
    print(f"Round robin: {len(bots)} bots, {a.seeds} seed(s) per pairing, {a.deals} deals per half")
    print(f"{'bot':22} {'points':>7} {'avg bb/deal':>12}")
    for b in sorted(bots, key=lambda b: (-pts[b], -sum(bb[b]) / max(1, len(bb[b])))):
        print(f"{b:22} {pts[b]:7.2f} {sum(bb[b]) / max(1, len(bb[b])):+12.3f}")
    for line in _health_lines(results):
        print(line)
    if a.explain:
        print("\n" + explain.legend_block("standings", [("points","points"),("avg bb/deal","bb/deal")]))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--deals", type=int, default=NUM_DEALS)
    common.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    common.add_argument("--explain", action="store_true", help="print a plain-language legend with the report")

    g = sub.add_parser("glossary", help="print plain-language definitions of every metric")
    g.set_defaults(fn=lambda a: print(explain.glossary_text()))

    m = sub.add_parser("match", parents=[common], help="one full match with the judge's report")
    m.add_argument("bot1")
    m.add_argument("bot2")
    m.add_argument("--seed", default="7")
    m.add_argument("--json", help="save the full result, including every hand")
    m.add_argument("--params", help='JSON overrides for bot1\'s PARAMS, e.g. \'{"pf_call": 0.5}\'')
    m.set_defaults(fn=cmd_match)

    f = sub.add_parser("field", parents=[common], help="one bot vs every zoo and baseline bot")
    f.add_argument("bot")
    f.add_argument("--opponents")
    f.add_argument("--seeds", type=int, default=3)
    f.set_defaults(fn=cmd_field)

    ab = sub.add_parser("ab", parents=[common], help="compare two versions on points")
    ab.add_argument("bot1")
    ab.add_argument("bot2")
    ab.add_argument("--opponents")
    ab.add_argument("--seeds", type=int, default=3)
    ab.set_defaults(fn=cmd_ab)

    t = sub.add_parser("tournament", parents=[common], help="round robin with tournament scoring")
    t.add_argument("bots", nargs="+")
    t.add_argument("--seeds", type=int, default=1)
    t.set_defaults(fn=cmd_tournament)

    a = p.parse_args(argv)
    t0 = time.time()
    a.fn(a)
    print(f"\nElapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
