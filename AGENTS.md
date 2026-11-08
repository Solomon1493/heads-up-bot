# Rules for AI agents working in this repo

**Read [STRATEGY.md](STRATEGY.md) first.** It is the settled design. Build toward it; do not
invent new tuning knobs or thresholds. Propose changes to it only with a measurement.

Two agents work here: **Claude Code** (development) and **Antigravity** (running and
logging experiments). They share one working tree, so they must not edit the same file.

## Who edits what
| Files | Owner |
|---|---|
| `mybot.py`, `mybot_v2.py`, `botlib/`, `fastarena.py`, `dashboard.py`, `dashboard_ui/`, `tools/`, `databricks/`, `families.py`, `zoo/`, `tune.py` | Claude Code |
| Running experiments, exports, syncs (`tune.py run/sweep`, `databricks/*.py`), writing to `data/` | Antigravity |

Antigravity runs the tools; it does not edit them. If a tool needs a change, describe it to
the human. The training loop is in STRATEGY.md, "How to train".
| `design/PROJECT.design.md` | the human; agents follow it for any UI |
| `DEFAULT_PARAMS` values in `mybot.py` | Change only from a `tune.py sweep` RECOMMENDED result, or when a human asks |
| New opponents in `zoo/` | Either; say so in your report |

Need a change in a file you don't own? Ask the human instead of editing it.

## Never edit
`game.py`, `evaluator.py`, `judge_engine.py`, `test_agent.py`, and anything in `agent.py`
outside the `YOUR CODE HERE` block. The tournament uses its own copies of the first three.

## The shipped bot
- `mybot.py` must not read environment variables or files to change its parameters.
  Tests override parameters in-process through `fastarena.apply_params`.
- Every value in `DEFAULT_PARAMS` must be read by the code (`P["name"]`). No dead parameters.
- Never let an exception escape `choose_action`, `new_half` or `on_hand_end`.

## Running matches
- One measurement at a time. `tune.py run/sweep`, `tools/trace_v2.py` and dashboard buttons all
  take the shared queue (`tools/runlock.py`) and wait their turn. Do not bypass it: two runs at
  once make both bots simulate less, so both results come out wrong.
- The sweep's noise guard stays: a candidate must beat the current params by more than
  2 standard errors. Do not loosen it to `> 0`; with noise near +/-2.6 that recommends luck.
- Automated improvement loops (any agent, any bot) go through `tools/loop.py` and its gate
  (see GEMINI.md), or an equivalent with the same rules: 8 seeds x 1000 deals, a never-used swarm
  per decision, and a margin above noise. Never keep a change because one short run beat the
  best so far, and never keep ties. (2026-11-08: an overnight hill-climber did both for 355
  cycles; its "best" bot scored 29.8 of 37 against v1's 34.3 of 38 when re-measured properly.)

## Log every iteration
Any change to a bot, its tuned values, or the evaluation (new opponents, scoring) gets one
changelog entry, shown on the dashboard: what changed, why (the evidence that prompted it),
and the measured result before and after.
`python tools/changelog.py add --author <you> --bot <mybot|mybot_v2|harness> --title ... --what ... --why ... --before ... --after ... --evidence ...`

## Reporting results
- Report results on deals the change was not tuned on: `python fastarena.py field mybot --seeds 4`.
- Give the win / chop / loss counts. Say "win" only when every seed won.
- A change is kept only if it beats the current bot on the training opponents AND the
  held-out opponents (see `TRAIN_OPPONENTS` in `tune.py`). One lucky run is not evidence.

## Before any commit
1. `.venv/bin/python -m pytest test_agent.py` passes.
2. `fastarena.py field mybot --seeds 4`: 0 errors, no FORFEIT / BANK EMPTY / SLOW flags.
