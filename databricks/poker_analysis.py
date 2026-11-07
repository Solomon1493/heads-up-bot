# Databricks notebook source
# MAGIC %md
# MAGIC # Poker bot leak finder
# MAGIC Every hand our bot played in the local arena, loaded by `databricks/sync.py`.
# MAGIC The judge logs **both players' cards** for every hand, so each decision can be graded
# MAGIC in hindsight: what was our real chance of winning against the opponent's actual hand?
# MAGIC That turns millions of rows into a ranked list of what to fix in the bot next.
# MAGIC
# MAGIC Tables: `matches` (one per match), `hands` (one per hand), `decisions` (one per action),
# MAGIC `experiments` and `experiment_opponents` (parameter-tuning runs from `tune.py`).
# MAGIC 1 bb (big blind) = 2 chips. Each player starts every hand with 40 bb.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catalog (blank = current)")
dbutils.widgets.text("schema", "poker_bot", "Schema")
catalog = dbutils.widgets.get("catalog") or spark.sql("SELECT current_catalog()").first()[0]
spark.sql(f"USE CATALOG `{catalog}`")
spark.sql(f"USE SCHEMA `{dbutils.widgets.get('schema')}`")
latest_run = spark.sql("SELECT max(run_id) FROM matches").first()[0]
# Views of the newest export run, used by the SQL cells below.
for t in ("hands", "decisions"):
    spark.sql(f"CREATE OR REPLACE TEMP VIEW latest_{t} AS SELECT * FROM {t} WHERE run_id = '{latest_run}'")
print(f"using {catalog}.{dbutils.widgets.get('schema')}, latest export run: {latest_run}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. How each bot version did
# MAGIC Each export run is one version of the bot (run id = time, git commit, code hash).
# MAGIC A **win** means the 95% confidence interval is entirely above zero, exactly as the
# MAGIC tournament judges it; anything else that is not a loss is a chop (draw).

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT run_id, opponent, count(*) AS matches,
# MAGIC        round(avg(mean_bb_per_deal), 3) AS avg_bb_per_deal,
# MAGIC        sum(CASE WHEN ci_low > 0 THEN 1 ELSE 0 END) AS wins,
# MAGIC        sum(CASE WHEN ci_high < 0 THEN 1 ELSE 0 END) AS losses,
# MAGIC        round(max(hero_think_seconds_max), 2) AS max_think_seconds_of_8
# MAGIC FROM matches
# MAGIC GROUP BY run_id, opponent
# MAGIC ORDER BY run_id DESC, avg_bb_per_deal

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Where the chips are won and lost
# MAGIC Net result by the street a hand ended on. Big losses on the river usually mean paying
# MAGIC off strong hands; big losses preflop mean folding too often to raises.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT opponent, final_street, went_to_showdown, count(*) AS hands,
# MAGIC        round(sum(hero_net_bb), 1) AS total_bb, round(avg(hero_net_bb), 3) AS avg_bb_per_hand
# MAGIC FROM latest_hands
# MAGIC GROUP BY opponent, final_street, went_to_showdown
# MAGIC ORDER BY opponent, total_bb

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Leak finder: calls and folds graded in hindsight
# MAGIC For every flop/turn/river spot where we faced a bet, the **hindsight value** of calling is
# MAGIC `equity x (pot + call) - call`, using our exact equity against the opponent's real cards.
# MAGIC - *bb lost on bad calls*: calls whose hindsight value was negative.
# MAGIC - *bb given up by folding*: folds whose hindsight value was positive.
# MAGIC
# MAGIC Hindsight knows the opponent's cards, so these totals overstate what any bot could save.
# MAGIC Read them as a **ranking**: the biggest number is the first thing to fix.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT opponent, street, action, count(*) AS spots,
# MAGIC        round(sum(CASE WHEN action = 'CHECK_CALL' AND hindsight_equity * (pot_before + to_call) - to_call < 0
# MAGIC                       THEN hindsight_equity * (pot_before + to_call) - to_call ELSE 0 END) / 2.0, 1) AS bb_lost_on_bad_calls,
# MAGIC        round(sum(CASE WHEN action = 'FOLD' AND hindsight_equity * (pot_before + to_call) - to_call > 0
# MAGIC                       THEN hindsight_equity * (pot_before + to_call) - to_call ELSE 0 END) / 2.0, 1) AS bb_given_up_by_folding,
# MAGIC        round(avg(hindsight_equity), 3) AS avg_equity, round(avg(pot_odds), 3) AS avg_pot_odds
# MAGIC FROM latest_decisions
# MAGIC WHERE actor = 'hero' AND to_call > 0
# MAGIC   AND hindsight_equity IS NOT NULL AND action IN ('CHECK_CALL', 'FOLD')
# MAGIC GROUP BY opponent, street, action
# MAGIC ORDER BY bb_lost_on_bad_calls, bb_given_up_by_folding DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Do our flop bets get folds?
# MAGIC When checked to, the bot often bets half the pot. A bet with weak equity only makes money
# MAGIC if the opponent folds. This shows the opponent's fold rate to our flop bets, by how strong
# MAGIC we really were. A low fold rate in the weak buckets means those bluffs are burning chips
# MAGIC against that opponent.

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH replies AS (
# MAGIC   SELECT *, LEAD(action) OVER (PARTITION BY hand_id ORDER BY seq) AS reply,
# MAGIC             LEAD(actor)  OVER (PARTITION BY hand_id ORDER BY seq) AS reply_by
# MAGIC   FROM latest_decisions)
# MAGIC SELECT opponent,
# MAGIC        CASE WHEN hindsight_equity < 0.3 THEN '1: under 30%' WHEN hindsight_equity < 0.5 THEN '2: 30-50%'
# MAGIC             WHEN hindsight_equity < 0.7 THEN '3: 50-70%' ELSE '4: 70% and up' END AS our_equity,
# MAGIC        count(*) AS bets,
# MAGIC        round(avg(CASE WHEN reply = 'FOLD' THEN 1.0 ELSE 0 END), 2) AS opponent_fold_rate
# MAGIC FROM replies
# MAGIC WHERE actor = 'hero' AND street = 'FLOP' AND action = 'RAISE_HALF' AND to_call = 0 AND reply_by = 'opponent'
# MAGIC GROUP BY opponent, our_equity
# MAGIC ORDER BY opponent, our_equity

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Opponent profiles
# MAGIC How each opponent plays, from its own actions. These are the patterns the bot's
# MAGIC opponent model tries to detect live, so a style it misreads shows up here first.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT opponent, street, count(*) AS actions,
# MAGIC        round(avg(CASE WHEN action IN ('RAISE_HALF', 'RAISE_POT') THEN 1.0 ELSE 0 END), 2) AS raise_rate,
# MAGIC        round(avg(CASE WHEN action = 'FOLD' THEN 1.0 ELSE 0 END) * count(*)
# MAGIC              / greatest(sum(CASE WHEN to_call > 0 THEN 1 ELSE 0 END), 1), 2) AS fold_when_facing_bet,
# MAGIC        round(avg(CASE WHEN action = 'CHECK_CALL' AND to_call > 0 THEN hindsight_equity END), 3) AS avg_equity_when_calling
# MAGIC FROM latest_decisions
# MAGIC WHERE actor = 'opponent'
# MAGIC GROUP BY opponent, street
# MAGIC ORDER BY opponent, CASE street WHEN 'PREFLOP' THEN 0 WHEN 'FLOP' THEN 1 WHEN 'TURN' THEN 2 ELSE 3 END

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Which tuning parameters matter
# MAGIC Each `tune.py sweep` trial nudges every parameter at random and measures the gain over
# MAGIC the current bot on the same deals. The correlation between a parameter's value and the
# MAGIC gain shows which knobs actually move results (needs a few dozen trials to mean much).

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH trials AS (
# MAGIC   SELECT delta_vs_baseline AS gain, from_json(params_json, 'map<string,double>') AS p
# MAGIC   FROM experiments
# MAGIC   WHERE sweep_trial IS NOT NULL AND opponent_set = 'train' AND delta_vs_baseline IS NOT NULL)
# MAGIC SELECT parameter, count(*) AS trials, round(corr(value, gain), 2) AS correlation_with_gain
# MAGIC FROM trials LATERAL VIEW explode(p) AS parameter, value
# MAGIC GROUP BY parameter
# MAGIC ORDER BY abs(corr(value, gain)) DESC
