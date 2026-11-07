# Rules for AI agents working in this repo

Two agents work here: **Claude Code** (development) and **Antigravity** (running and
logging experiments). They share one working tree, so they must not edit the same file.

## Who edits what
| Files | Owner |
|---|---|
| `mybot.py` strategy logic, `botlib/`, `fastarena.py` | Claude Code |
| `tune.py`, `dashboard.py`, `data/` | Antigravity |
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

## Reporting results
- Report results on deals the change was not tuned on: `python fastarena.py field mybot --seeds 4`.
- Give the win / chop / loss counts. Say "win" only when every seed won.
- A change is kept only if it beats the current bot on the training opponents AND the
  held-out opponents (see `TRAIN_OPPONENTS` in `tune.py`). One lucky run is not evidence.

## Before any commit
1. `.venv/bin/python -m pytest test_agent.py` passes.
2. `fastarena.py field mybot --seeds 4`: 0 errors, no FORFEIT / BANK EMPTY / SLOW flags.
