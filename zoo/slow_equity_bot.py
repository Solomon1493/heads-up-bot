"""Replica of a careless AI-written bot: same logic, 250 sims/decision.
Expect it to empty its time bank, then auto check/fold for the rest of the half."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action, Street
from botlib.equity import mc_equity, preflop_features, equity_policy


def new_half(config, memory):
    memory["rng"] = random.Random(1)


def choose_action(obs, memory):
    return equity_policy(obs, memory["rng"], 250)


def on_hand_end(result, memory):
    pass
