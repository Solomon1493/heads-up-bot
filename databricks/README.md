# Databricks: leak finder for the poker bot

Local arena matches → Delta tables → a notebook that grades every decision the bot made
against the opponent's real cards and ranks what to fix next.

```
fastarena (local)  ──export.py──▶  data/databricks/<run>/*.jsonl
                                         │ sync.py
                                         ▼
                   Unity Catalog volume  <catalog>.poker_bot.raw
                                         │ read_files → Delta
                                         ▼
          tables: matches · hands · decisions · experiments · experiment_opponents
                                         │
                                         ▼
                          poker_analysis.py notebook (leak finder)
```

Nothing here runs in the tournament: the bot has no internet there. Databricks is the
offline lab that tells us what to change in `mybot.py`.

## One-time setup
1. Install the SDK into the project venv:
   `.venv/bin/pip install -r requirements-databricks.txt`
2. Install the Databricks CLI and log in (opens a browser; no token is stored in this repo):
   ```
   brew tap databricks/tap && brew install databricks
   databricks auth login --host https://<your-workspace>.cloud.databricks.com
   ```
   Accept the default profile name. Teammates do the same with their own login.
3. The workspace needs Unity Catalog and one SQL warehouse (the smallest serverless one is
   plenty). `sync.py` starts the warehouse if it is stopped.

## Each time you want fresh data
```
.venv/bin/python databricks/export.py --seeds 2 --deals 1000   # ~1-2 min on a laptop
.venv/bin/python databricks/sync.py                              # newest run + tuning log
```
`sync.py --all` uploads every run on disk; `--dry-run` shows what it would do.
Each run is tagged with the git commit and a hash of the bot, so bot versions can be compared.

## The notebook
Import `databricks/poker_analysis.py` into the workspace (Workspace → Import, or put the
repo in a Git folder). Run all cells. Sections: results per bot version, where chips are
won and lost, the hindsight leak finder, whether our bets get folds, opponent profiles, and
which tuning parameters matter.

## Never commit
Tokens or `~/.databrickscfg`. The repo is public at submission.
