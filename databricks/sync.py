"""databricks/sync.py - load exported match data and the tuning log into Delta tables.

What it does, in order:
  1. connects with your Databricks login (a profile in ~/.databrickscfg; never a token
     in this repo, which goes public at submission)
  2. picks a SQL warehouse and creates the schema and a Unity Catalog volume if missing
  3. uploads data/databricks/<run>/*.jsonl and data/experiments.jsonl into the volume
  4. (re)builds Delta tables from everything in the volume, so repeated syncs accumulate:
         matches, hands, decisions             from databricks/export.py
         experiments, experiment_opponents     from tune.py's log

Usage (from the repo root, after `databricks auth login`, see databricks/README.md):
    .venv/bin/python databricks/sync.py                 # newest export run + tuning log
    .venv/bin/python databricks/sync.py --all           # every export run on disk
    .venv/bin/python databricks/sync.py --dry-run       # show what would happen, touch nothing
"""
import argparse
import glob
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT_DIR = os.path.join(ROOT, "data", "databricks")
EXPERIMENT_LOG = os.path.join(ROOT, "data", "experiments.jsonl")
EXPORT_TABLES = ("matches", "hands", "decisions")
LOG_TABLES = ("experiments", "experiment_opponents")


def flatten_experiments(path):
    """tune.py's nested log -> one row per run, and one row per run x opponent."""
    runs, per_opp = [], []
    if not os.path.exists(path):
        return runs, per_opp
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        field, health = r.get("field", {}), r.get("health", {})
        runs.append({
            "experiment_id": r.get("id"), "timestamp": r.get("timestamp"), "tag": r.get("tag"),
            "code_hash": r.get("code_hash"), "git_sha": r.get("git", {}).get("sha"),
            "opponent_set": r.get("opponent_set", "all"), "sweep_trial": r.get("sweep_trial"),
            "total_points": field.get("total_points"), "max_points": field.get("max_points"),
            "score_bb": field.get("score_bb"), "score_bb_se": field.get("score_bb_se"),
            "delta_vs_baseline": r.get("delta_vs_baseline"), "delta_se": r.get("delta_se"),
            "seeds": field.get("seeds"), "deals": field.get("deals"),
            "safe": health.get("safe"), "max_thinking_s": health.get("max_thinking_s"),
            "params_json": json.dumps(r.get("params", {}), sort_keys=True),
        })
        for opp, o in field.get("opponents", {}).items():
            per_opp.append({"experiment_id": r.get("id"), "opponent": opp, **o})
    return runs, per_opp


def _jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


class Sql:
    def __init__(self, w, warehouse_id):
        self.w, self.wid = w, warehouse_id

    def __call__(self, statement):
        from databricks.sdk.service.sql import StatementState
        resp = self.w.statement_execution.execute_statement(
            warehouse_id=self.wid, statement=statement, wait_timeout="50s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            err = resp.status.error.message if resp.status.error else resp.status.state
            raise SystemExit(f"SQL failed: {err}\n  statement: {statement[:300]}")
        return resp.result.data_array if resp.result and resp.result.data_array else []


def pick_warehouse(w, wanted):
    whs = list(w.warehouses.list())
    if not whs:
        raise SystemExit("No SQL warehouse in this workspace. Create one under SQL > SQL Warehouses "
                         "(the smallest serverless size is plenty), then re-run.")
    wh = next((x for x in whs if x.id == wanted), None) if wanted else \
        next((x for x in whs if getattr(x.state, "value", "") == "RUNNING"), whs[0])
    if wh is None:
        raise SystemExit(f"Warehouse {wanted} not found. Available: {[(x.name, x.id) for x in whs]}")
    if getattr(wh.state, "value", "") != "RUNNING":
        print(f"  starting warehouse '{wh.name}' (can take a minute)...")
        w.warehouses.start_and_wait(wh.id)
    return wh


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", help="export run id to upload (default: the newest)")
    p.add_argument("--all", action="store_true", help="upload every export run on disk")
    p.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE"),
                   help="profile in ~/.databrickscfg (default: DEFAULT)")
    p.add_argument("--catalog", help="Unity Catalog catalog (default: the workspace's current catalog)")
    p.add_argument("--schema", default="poker_bot")
    p.add_argument("--volume", default="raw")
    p.add_argument("--warehouse-id")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    runs = sorted(os.path.basename(d) for d in glob.glob(os.path.join(EXPORT_DIR, "*")) if os.path.isdir(d))
    if a.all:
        chosen = runs
    elif a.run:
        if a.run not in runs:
            raise SystemExit(f"No export run '{a.run}' in {EXPORT_DIR}")
        chosen = [a.run]
    else:
        chosen = runs[-1:]
    exp_runs, exp_opps = flatten_experiments(EXPERIMENT_LOG)
    print(f"export runs to upload: {chosen or 'none (run databricks/export.py first)'}")
    print(f"tuning log: {len(exp_runs)} experiments, {len(exp_opps)} opponent rows")
    if a.dry_run:
        print("dry run: nothing uploaded")
        return

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise SystemExit("Install the SDK first: .venv/bin/pip install -r requirements-databricks.txt")
    w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient()
    print(f"connected to {w.config.host} as {w.current_user.me().user_name}")
    wh = pick_warehouse(w, a.warehouse_id)
    sql = Sql(w, wh.id)

    catalog = a.catalog or sql("SELECT current_catalog()")[0][0]
    if catalog == "hive_metastore":
        raise SystemExit("This workspace has no Unity Catalog (current catalog is hive_metastore), which "
                         "volumes need. Pass --catalog <a UC catalog>, or enable Unity Catalog.")
    fq = f"`{catalog}`.`{a.schema}`"
    vol = f"/Volumes/{catalog}/{a.schema}/{a.volume}"
    print(f"target: {catalog}.{a.schema}  (files in {vol}, warehouse '{wh.name}')")
    sql(f"CREATE SCHEMA IF NOT EXISTS {fq} COMMENT 'TAMU Datathon poker bot: arena matches and tuning runs'")
    sql(f"CREATE VOLUME IF NOT EXISTS {fq}.`{a.volume}` COMMENT 'Raw JSON-lines uploads from the local arena'")

    for run in chosen:
        for t in EXPORT_TABLES:
            src = os.path.join(EXPORT_DIR, run, f"{t}.jsonl")
            with open(src, "rb") as f:
                w.files.upload(f"{vol}/{t}/{run}.jsonl", f, overwrite=True)
        print(f"  uploaded run {run}")
    for t, rows in (("experiments", exp_runs), ("experiment_opponents", exp_opps)):
        if rows:
            w.files.upload(f"{vol}/{t}/experiments.jsonl", io.BytesIO(_jsonl(rows)), overwrite=True)

    have = {t for t in EXPORT_TABLES + LOG_TABLES
            if any(e.name for e in _list(w, f"{vol}/{t}"))}
    for t in EXPORT_TABLES + LOG_TABLES:
        if t not in have:
            continue
        sql(f"CREATE OR REPLACE TABLE {fq}.`{t}` AS "
            f"SELECT * FROM read_files('{vol}/{t}/', format => 'json')")
        n = sql(f"SELECT count(*) FROM {fq}.`{t}`")[0][0]
        print(f"  table {catalog}.{a.schema}.{t}: {n} rows")
    print("done. Open databricks/poker_analysis.py as a notebook in the workspace to explore.")


def _list(w, path):
    try:
        return list(w.files.list_directory_contents(path))
    except Exception:
        return []


if __name__ == "__main__":
    sys.exit(main())
