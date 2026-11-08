"""families.py - the opponent families of STRATEGY.md, in one place.

Every tool that picks opponents (tune.py, the dashboard scoreboard) reads this file.

  A  Generated     zoo/gen_*.py: one parameterized bot, many styles (incl. stack-off players)
  B  Basic/broken  the official baselines, a time-bank-blowing bot, two all-in bots
  C  Exam          bots written like typical competition entries: NEVER tuned on
  D  League        frozen earlier versions of our own bot (regression gate)
  E  Exploiter     a bot that adapts to our habits (exploitability gate)

Tuning uses A + B only. A change ships only if it also holds up on C, D and E.
A zoo bot not listed here is treated as exam (family C): unseen opponents are the test.
"""
import glob
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

FAMILY_NAMES = {"A": "Generated", "B": "Basic and broken", "C": "Exam (held out)",
                "D": "Our past versions", "E": "Exploiter", "S": "Swarm (random styles)"}

_EXPLICIT = {
    "B": ["random", "half", "pot", "station", "slow_equity_bot", "allin_random", "allin_selective"],
    "D": ["mybot"],
    "E": ["exploiter"],
}
TRAIN_FAMILIES = ("A", "B")
HELDOUT_FAMILIES = ("C", "D", "E")

# Weight of each family in "expected tournament points" (STRATEGY.md: the field we expect is
# mostly typical competition bots). D and E are gates, so they share the last 10%.
FIELD_WEIGHT = {"C": 0.5, "B": 0.2, "A": 0.2, "D": 0.05, "E": 0.05}


def _zoo_names():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(ROOT, "zoo", "*.py"))
                  if not os.path.basename(p).startswith("_"))


def families(hero=None):
    """{family letter: [opponent specs]}, without the hero itself."""
    zoo = _zoo_names()
    fam = {"A": [z for z in zoo if z.startswith("gen_")]}
    fam.update({k: list(v) for k, v in _EXPLICIT.items()})
    listed = {o for v in fam.values() for o in v}
    fam["C"] = [z for z in zoo if z not in listed]
    return {k: [o for o in v if o != hero] for k, v in sorted(fam.items())}


def family_of(opponent):
    if str(opponent).startswith("swarm:"):
        return "S"
    for k, v in families().items():
        if opponent in v:
            return k
    return "C"


def split(hero="mybot"):
    fam = families(hero)
    train = [o for k in TRAIN_FAMILIES for o in fam.get(k, [])]
    heldout = [o for k in HELDOUT_FAMILIES for o in fam.get(k, [])]
    return train, heldout


def all_opponents(hero="mybot"):
    fam = families(hero)
    return [o for k in sorted(fam) for o in fam[k]]
