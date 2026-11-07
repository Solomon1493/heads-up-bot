# fastarena: in-process testing for the Short Deck PLO challenge

Copy everything here into the root of your copy of `heads-up-bundle`, next to `game.py`.
Python 3.10+, plus `pip install fastapi uvicorn` (needed to import agent.py and baselines).

## What it is
`fastarena.py` runs the official `judge_engine.play_half` and `combine_halves` with bots
called directly instead of over HTTP. Rules, dealing, duplicate halves, time bank,
5-error forfeit and the 95% CI verdict are the organizers' own code. On identical seeds it
reproduces the official judge's results exactly. It is about 5-10x faster and needs no servers.

## Commands
    python fastarena.py match mybot random --deals 1000 --seed 7   # judge-style report
    python fastarena.py field mybot --seeds 3                      # vs every zoo + baseline bot
    python fastarena.py ab mybot equity_bot --seeds 5              # compare two versions on points
    python fastarena.py tournament mybot agent.py tight_bot aggro_bot --seeds 2
Bots: a path (agent.py, mybot.py), a zoo/ name, or random / half / pot / station.
Add --workers N on a multi-core laptop. Use --deals 300 for quick checks.

Understanding the numbers (you do not need to know poker or stats):
    python fastarena.py glossary            # plain-language definition of every metric
    python fastarena.py field mybot --explain   # same report, with a legend under it
Add --explain to match / field / tournament to get the legend inline.

## Files
- fastarena.py      the arena
- mybot.py          the team's strategy (agent.py should only delegate to it; see its docstring)
- botlib/equity.py  fast Monte Carlo equity (same results as omaha_score), preflop heuristic
- zoo/              replica opponents: equity_bot (typical AI bot), slow_equity_bot (blows its
                    time bank), tight_bot, aggro_bot. Add more; any zoo/*.py is picked up.
- botlib/explain.py the glossary behind `glossary` and `--explain`
- DASHBOARD_NOTES.md running spec for the end-of-day Claude Code dashboard hand-off. Keep it
                    open while testing; jot down anything you wish you could see.

## Reading results
- The `field` table shows W / C / L per opponent and expected points: what the tournament scores.
- "CI half-width" is how wide the judge's interval was. Narrower = easier to win significantly.
- Flags: FORFEIT, BANK EMPTY, SLOW (>4 s of the 8 s bank), errors. Any of these needs fixing first.
- Merge a change only if expected points rise across several seeds, not on one lucky match.

## Before every submission
1. `python fastarena.py field agent.py --seeds 3` shows 0 errors and no flags.
2. `python test_agent.py` (or pytest) passes and `./tools/check_build.sh` builds.
3. One real-judge match (three terminals, see the participant guide) as a final check.
