"""Run the rangeaware_mc test opponent as a real HTTP bot, for three-terminal judge matches.

    python opponents/rangeaware_agent.py          # listens on port 5009 (our bot uses 5008)
    python judge_engine.py --deals 1000           # agent1 = our agent.py, agent2 = this bot

The strategy is zoo/rangeaware_mc.py; the server is the official template's plumbing, reused
from baseline-bots/baseline_random.py so it behaves exactly like a competitor's container.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PORT", "5009")

import importlib.util  # noqa: E402

from zoo import rangeaware_mc  # noqa: E402

# Load the official plumbing from the random baseline, then swap in this strategy.
spec = importlib.util.spec_from_file_location("_plumbing", os.path.join(ROOT, "baseline-bots", "baseline_random.py"))
plumbing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plumbing)
plumbing.new_half = rangeaware_mc.new_half
plumbing.choose_action = rangeaware_mc.choose_action
plumbing.on_hand_end = rangeaware_mc.on_hand_end
plumbing.PARTICIPANT, plumbing.AGENT_NAME = "Test opponent", "RangeAwareMC"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(plumbing.app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
