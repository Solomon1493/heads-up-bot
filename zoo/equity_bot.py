"""Replica of the typical AI-written bot: Monte Carlo equity + pot odds (25 sims/decision)."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from game import Action, Street
from botlib.equity import mc_equity, preflop_features, equity_policy


def new_half(config, memory):
    memory["rng"] = random.Random(1)


def choose_action(obs, memory):
    return equity_policy(obs, memory["rng"], 25)


def on_hand_end(result, memory):
    pass
