"""Plain-language explanations for everything the arena reports.

The goal: nobody on the team should have to ask what a number means or whether it
is good. Every metric here carries a one-line definition and a "what good looks like".
Imported by fastarena.py; also runnable on its own as a glossary:  python -m botlib.explain
"""

# metric -> (what it is, what good looks like)
GLOSSARY = {
    "points": (
        "Tournament score for a match: win = 1, chop (draw) = 0.5, loss = 0.",
        "Higher. This is literally what the tournament ranks you on.",
    ),
    "bb/deal": (
        "Average big blinds won per deal over the match (both seats combined).",
        "Positive and large. +1 bb/deal is a comfortable edge; near 0 is a toss-up.",
    ),
    "CI half-width": (
        "Half the width of the judge's 95% confidence interval on bb/deal. "
        "The result is 'mean plus or minus this'.",
        "Smaller. A win counts only if mean minus this stays above 0, so a narrow "
        "interval turns a real edge into a scored win instead of a chop.",
    ),
    "W / C / L": (
        "How many of the matches against this opponent were wins, chops, losses.",
        "More W, fewer L. A chop still beats a loss (0.5 vs 0).",
    ),
    "verdict": (
        "Who the judge would score the match to: a win needs the whole confidence "
        "interval above 0, not just a positive average.",
        "AGENT1 WIN for your bot. CHOP means your edge was not proven, not that you tied.",
    ),
    "my max s": (
        "The most thinking time your bot used in any half, out of an 8-second bank.",
        "Comfortably under 8, ideally under 4. The tournament machine may be slower "
        "than your laptop, so leave a wide margin.",
    ),
    "errors": (
        "Illegal actions, crashes, or decisions over 1 second. The judge plays a "
        "default move for each.",
        "Zero. Five in one half forfeits the match, which is an automatic loss.",
    ),
    "FORFEIT": (
        "Your bot hit 5 errors in a half, or failed to start, and lost the whole match.",
        "Never. A forfeit loses a match you might have won on chips. Fix the cause first.",
    ),
    "BANK EMPTY": (
        "Your bot used all 8 seconds of thinking in a half and then auto-folded the rest.",
        "Never. It silently throws away chips. Use fewer simulations per decision.",
    ),
    "SLOW": (
        "Your bot used more than half its time bank. A warning, not yet a failure.",
        "Clear it. On a slower machine 'slow' can become 'bank empty'.",
    ),
    "equity": (
        "Estimated probability you win this hand at showdown, from Monte Carlo "
        "simulation against random opponent cards.",
        "Context-dependent. Compare it to pot odds: call when equity beats the price.",
    ),
    "pot odds": (
        "The price of a call: chips to call divided by the pot after you call.",
        "Call when your equity is above this. Example: pay 10 into a 40 pot means "
        "you need to win more than 25 percent of the time.",
    ),
    "fold-to-bet rate": (
        "How often an opponent folds when you bet. Tracked per opponent, within a half.",
        "High means you can bluff and steal more against them. Low means never bluff.",
    ),
    "aggression": (
        "How often an opponent raises versus calls. Tracked within a half.",
        "High means they bluff a lot, so call wider. Low means their bets are real.",
    ),
    "expected points": (
        "Points you would score across this whole field, if it were the tournament.",
        "Higher, out of the number of opponents. This is the single headline number.",
    ),
    "held-out": (
        "Opponents the tuner never optimizes against, used only to check that gains "
        "are real and not overfitting.",
        "Gains here should track gains on the training field. If training improves but "
        "held-out does not, you are overfitting: stop.",
    ),
}

FIELD_LEGEND = [
    ("points", "points"),
    ("bb/deal", "bb/deal"),
    ("CI half-width", "CI half-width"),
    ("my max s", "my max s"),
    ("flags (FORFEIT/BANK EMPTY/SLOW)", "FORFEIT"),
]

MATCH_LEGEND = [
    ("verdict", "verdict"),
    ("bb/deal", "bb/deal"),
    ("confidence interval width", "CI half-width"),
    ("thinking / errors (Faults)", "errors"),
]


def legend_block(title, items):
    lines = [f"How to read the {title}:"]
    for label, key in items:
        what, good = GLOSSARY[key]
        lines.append(f"  - {label}: {what}")
        lines.append(f"      good: {good}")
    return "\n".join(lines)


def glossary_text():
    out = ["GLOSSARY: every number the arena reports, in plain language.\n"]
    for key, (what, good) in GLOSSARY.items():
        out.append(f"{key}")
        out.append(f"  what: {what}")
        out.append(f"  good: {good}\n")
    return "\n".join(out)


if __name__ == "__main__":
    print(glossary_text())
