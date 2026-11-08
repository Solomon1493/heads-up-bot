"""databricks/leak_sheet.py - run the fixed leak queries in Databricks and write one ranked sheet.

Reads the Delta tables that sync.py builds (decisions, hands, matches), restricted to the
current version of the bot (by the code fingerprint in run_id), and writes
data/leaks/latest.json: every place the bot gives away chips, in plain words, with the
settings that move it, or "structural" when no setting can (those go to a human).
tools/loop.py leaks prints this sheet when it matches the current code.

    .venv/bin/python databricks/export.py --hero mybot --seeds 2      # fresh hands (local)
    .venv/bin/python databricks/sync.py                               # load into Delta
    .venv/bin/python databricks/leak_sheet.py                         # query, write the sheet
"""
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "leaks", "latest.json")

CALL_FOLD = ["tight_call_penalty", "aggro_margin", "aggro_threshold", "tight_threshold"]
BET = ["eq_raise_pot", "eq_raise_half", "eq_bluff_fold_rate", "eq_bluff_min", "tight_probe_min_eq"]
PREFLOP_BTN = ["pf_btn_raise", "pf_btn_limp", "pf_btn_call_raise"]
PREFLOP_BB = ["pf_bb_raise", "pf_bb_call"]

# chips given away by one call or fold, priced against the opponent's real cards
LOSS = """CASE WHEN action = 'CHECK_CALL' THEN to_call - hindsight_equity * (pot_before + to_call)
              ELSE hindsight_equity * (pot_before + to_call) - to_call END"""
SPOT = f"""actor = 'hero' AND to_call > 0 AND action IN ('CHECK_CALL', 'FOLD')
           AND street != 'PREFLOP' AND hindsight_equity IS NOT NULL"""


def code_version(bot):
    import fastarena
    return hashlib.sha256(open(fastarena.resolve(bot), "rb").read()).hexdigest()[:8]


def connect():
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list()))
    return w, wh


def runner(w, wh, fq):
    def sql(q):
        r = w.statement_execution.execute_statement(warehouse_id=wh.id, statement=q.replace("{T}", fq), wait_timeout="50s")
        while r.status.state.value in ("PENDING", "RUNNING"):
            time.sleep(2)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state.value != "SUCCEEDED":
            raise SystemExit(f"Query failed: {r.status.error.message if r.status.error else r.status.state}\n{q[:400]}")
        cols = [c.name for c in r.manifest.schema.columns]
        return [dict(zip(cols, row)) for row in (r.result.data_array or [])]
    return sql


def f(x):
    return None if x is None else float(x)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bot", default="mybot")
    p.add_argument("--catalog", default="workspace")
    p.add_argument("--schema", default="poker_bot")
    p.add_argument("--min-hands", type=int, default=30, help="ignore spots seen fewer times than this")
    a = p.parse_args(argv)
    sys.path.insert(0, ROOT)
    ver = code_version(a.bot)
    w, wh = connect()
    sql = runner(w, wh, f"`{a.catalog}`.`{a.schema}`")
    mine = f"hero = '{a.bot}' AND element_at(split(run_id, '_'), 3) = '{ver}'"

    size = sql(f"SELECT count(*) AS hands, count(DISTINCT opponent) AS opps, sum(hero_net_chips) AS net "
               f"FROM {{T}}.hands WHERE {mine}")[0]
    if not int(size["hands"] or 0):
        raise SystemExit(f"No hands in Databricks for {a.bot} version {ver}. Export and sync first:\n"
                         f"  .venv/bin/python databricks/export.py --hero {a.bot} --seeds 2\n"
                         f"  .venv/bin/python databricks/sync.py")
    leaks = []

    # 1. calls and folds by street and bet size
    for r in sql(f"""SELECT street, action, bet_faced, count(*) n, sum({LOSS}) lost,
                            avg(hindsight_equity) eq, avg(pot_odds) need
                     FROM {{T}}.decisions WHERE {mine} AND {SPOT} GROUP BY 1, 2, 3"""):
        n, lost = int(r["n"]), f(r["lost"])
        if n < a.min_hands or lost <= 0:
            continue
        verb = "calls" if r["action"] == "CHECK_CALL" else "folds to"
        leaks.append({"area": "call/fold", "chips": lost, "n": n,
                      "title": f"On the {r['street'].lower()}, {verb} a {r['bet_faced'] or ''}-pot bet {n} times with "
                               f"{f(r['eq']):.0%} real equity (price needs {f(r['need']):.0%})",
                      "fix": "call less" if r["action"] == "CHECK_CALL" else "fold less", "levers": CALL_FOLD})

    # 2. the same, only on one board texture: a texture-specific leak needs new logic (structural)
    for col, label in (("board_paired", "paired boards"), ("board_suited >= 3", "flush-heavy boards"),
                       ("board_connected", "connected boards")):
        for r in sql(f"""SELECT street, action, count(*) n, sum({LOSS}) lost, avg(hindsight_equity) eq, avg(pot_odds) need
                         FROM {{T}}.decisions WHERE {mine} AND {SPOT} AND {col} GROUP BY 1, 2"""):
            n, lost = int(r["n"]), f(r["lost"])
            if n < a.min_hands or lost <= 0:
                continue
            verb = "calls" if r["action"] == "CHECK_CALL" else "folds"
            leaks.append({"area": "board texture", "chips": lost, "n": n, "structural": True,
                          "title": f"On {label}, {r['street'].lower()}: {verb} with {f(r['eq']):.0%} real equity "
                                   f"(price needs {f(r['need']):.0%}), {n} times"})

    # 3. by opponent style (swarm opponents): which kinds of player cost chips
    for col, label in (("m.opp_k > 1", "range-aware opponents"), ("m.opp_bluff >= 0.3", "heavy bluffers"),
                       ("m.opp_trap > 0", "slow-players"), ("m.opp_size_tell", "opponents with a bet-size tell")):
        r = sql(f"""SELECT count(*) n, sum({LOSS.replace('to_call', 'd.to_call').replace('pot_before', 'd.pot_before')
                                              .replace('hindsight_equity', 'd.hindsight_equity').replace('action', 'd.action')}) lost
                    FROM {{T}}.decisions d JOIN {{T}}.matches m ON d.match_id = m.match_id
                    WHERE d.hero = '{a.bot}' AND element_at(split(d.run_id, '_'), 3) = '{ver}'
                      AND d.actor = 'hero' AND d.to_call > 0 AND d.action IN ('CHECK_CALL', 'FOLD')
                      AND d.street != 'PREFLOP' AND d.hindsight_equity IS NOT NULL AND {col}""")
        r = r[0] if r else {}
        n, lost = int(r.get("n") or 0), f(r.get("lost")) or 0.0
        if n >= a.min_hands and lost > 0:
            leaks.append({"area": "opponent style", "chips": lost, "n": n, "structural": True,
                          "title": f"Against {label}: call/fold mistakes cost {lost:.0f} chips over {n} decisions"})

    # 4. preflop: chips per hand by position and first action (preflop has no hindsight equity)
    for r in sql(f"""WITH first AS (
                       SELECT hand_id, actor_is_button, action,
                              row_number() OVER (PARTITION BY hand_id ORDER BY seq) rk
                       FROM {{T}}.decisions WHERE {mine} AND actor = 'hero' AND street = 'PREFLOP')
                     SELECT f.actor_is_button btn, f.action, count(*) n, sum(h.hero_net_chips) net
                     FROM first f JOIN {{T}}.hands h ON f.hand_id = h.hand_id WHERE f.rk = 1 GROUP BY 1, 2"""):
        n, net = int(r["n"]), f(r["net"])
        if n >= a.min_hands and net < 0 and r["action"] != "FOLD":
            pos = "on the button" if r["btn"] in (True, "true") else "in the big blind"
            leaks.append({"area": "preflop", "chips": -net, "n": n,
                          "title": f"Preflop {pos}, hands it opened with '{r['action'].lower().replace('_', ' ')}' "
                                   f"lose {-net / n:.2f} chips each ({n} hands)",
                          "fix": "play fewer of these hands, or raise them differently",
                          "levers": PREFLOP_BTN if pos == "on the button" else PREFLOP_BB})

    # 5. our own bets when checked to: what actually happened after betting vs after checking,
    #    in spots with the same real equity (tenths), street and position. "Result from here" is the
    #    hand result plus what we had already put in, divided by the pot (so pot sizes compare).
    #    It includes later streets, raises and calls, which a one-street estimate misses.
    STACK = 80
    rows = sql(f"""WITH d AS (
                     SELECT d.street, d.actor_is_button btn, floor(d.hindsight_equity * 10) eqb, d.pot_before,
                            CASE WHEN d.action IN ('RAISE_HALF','RAISE_POT') THEN 'bet' ELSE 'check' END choice,
                            (h.hero_net_chips + ({STACK} - d.stack_before)) / d.pot_before rel
                     FROM {{T}}.decisions d JOIN {{T}}.hands h ON d.hand_id = h.hand_id
                     WHERE d.hero = '{a.bot}' AND element_at(split(d.run_id, '_'), 3) = '{ver}' AND d.actor = 'hero'
                       AND d.to_call = 0 AND d.street != 'PREFLOP' AND d.hindsight_equity IS NOT NULL AND d.pot_before > 0)
                   SELECT street, btn, eqb, choice, count(*) n, avg(rel) rel, avg(pot_before) pot
                   FROM d GROUP BY 1, 2, 3, 4""")
    cells = {}
    for r in rows:
        cells.setdefault((r["street"], r["btn"], r["eqb"]), {})[r["choice"]] = r
    agg = {}
    for (street, btn, eqb), c in cells.items():
        if "bet" not in c or "check" not in c:
            continue
        nb, nc = int(c["bet"]["n"]), int(c["check"]["n"])
        if nb < a.min_hands or nc < a.min_hands:
            continue
        diff = f(c["bet"]["rel"]) - f(c["check"]["rel"])          # pots won per decision, bet minus check
        band = "weak" if float(eqb) < 4 else "medium" if float(eqb) < 6 else "strong"
        key = (street, band)
        g = agg.setdefault(key, [0, 0.0])
        g[0] += nb
        g[1] += diff * f(c["bet"]["pot"]) * nb                      # chips: per-pot difference x pot x bets
    tradeoff = {}
    for (street, band), (n, chips) in agg.items():
        tradeoff.setdefault(street.lower(), {})[band] = {"bets": n, "chips_vs_check": round(chips)}
    for (street, band), (n, chips) in agg.items():
        if chips < 0:
            leaks.append({"area": "betting", "chips": -chips, "n": n,
                          "title": f"On the {street.lower()}, betting {band} hands ({'under 40%' if band == 'weak' else '40-60%' if band == 'medium' else 'over 60%'} "
                                   f"real equity) did worse than checking the same kind of hand: {-chips / n:.2f} chips per bet, {n} bets",
                          "fix": "bet these less" if band != "strong" else "check these more (slow down)", "levers": BET,
                          "caveat": "v1 cannot see real equity when it bets: betting fewer weak hands also means betting "
                                    "fewer strong ones. Weigh against the strong-hand gain in 'bet_tradeoff'."})

    # 6. version trend: chips per hand for every version of the bot in the tables
    trend = [{"version": r["v"], "hands": int(r["n"]), "chips_per_hand": round(f(r["net"]) / int(r["n"]), 3)}
             for r in sql(f"""SELECT element_at(split(run_id, '_'), 3) v, count(*) n, sum(hero_net_chips) net
                              FROM {{T}}.hands WHERE hero = '{a.bot}' GROUP BY 1 ORDER BY min(run_id)""")]

    leaks.sort(key=lambda x: -x["chips"])
    for x in leaks:
        x["chips"] = round(x["chips"])
        x.setdefault("structural", False)
    doc = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "bot": a.bot, "version": ver,
           "hands": int(size["hands"]), "opponents": int(size["opps"]), "net_chips": f(size["net"]),
           "leaks": leaks, "trend": trend, "bet_tradeoff": tradeoff}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(doc, fh, indent=1)
    print(f"{a.bot} version {ver}: {doc['hands']} hands vs {doc['opponents']} opponents in Databricks")
    for i, x in enumerate(leaks[:10], 1):
        tag = "structural" if x["structural"] else f"settings: {', '.join(x['levers'])}"
        print(f"  {i:2}. [{x['area']}] {x['title']}. Cost {x['chips']} chips. ({tag})")
    print("  betting trade-off (chips vs checking the same kind of hand):")
    for st_, bands in tradeoff.items():
        print(f"    {st_:5} " + ", ".join(f"{b} {v['chips_vs_check']:+} ({v['bets']} bets)" for b, v in bands.items()))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
