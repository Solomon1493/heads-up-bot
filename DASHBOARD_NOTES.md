# Dashboard notes — the running spec for the Claude Code hand-off

Purpose: capture, during today's real matches, what we actually want to SEE. At the end of
the day this file is handed to Claude Code to build the dashboard in one shot. Do not design
the dashboard from imagination now — fill this in from things that actually surprised or
confused you while running the arena. A spec grounded in real matches beats a guessed one.

How to use this file
- Keep it open while you run `fastarena.py`. Every time you think "I wish I could see X",
  add a line under "Wishlist" with the date/time and who wrote it.
- When a number confuses you, add it under "Confusing numbers". Those become tooltips.
- Do not delete ideas; strike through ones you decide against, with a reason.

---

## Decisions still open (answer these from today's experience, not now)
- [ ] Live or post-run? Does the dashboard watch matches as they happen, or analyze finished
      runs? (Different builds. Today's matches will make the answer obvious.)
- [ ] Who is the primary user — Solomon (learning) or the whole team (deciding what to merge)?
- [ ] Does it need to run on one laptop only, or be shared? (A single self-contained HTML file
      that reads a results JSON is the simplest thing that could work.)

## What we already know is worth a panel (seeded from planning)
Each earns a panel only because it changes a decision. Confirm or cut each one today.
- Win / chop / loss and CI half-width per opponent — see not just if we win but if the win is
  statistically real (a chop is not a tie; it is an unproven edge).
- Expected points across the field — the single headline number the tournament ranks on.
- Training vs held-out comparison — the overfitting guard. If training improves but held-out
  does not, stop. This panel is the whole reason we trust our tuning.
- Chips won/lost by street (preflop/flop/turn/river) — where our edge or leak actually is.
- Time-bank usage per decision — running out auto-folds silently; a hidden match-loser.
- Parameter vs score — how each tunable number moves expected points (once the tuner exists).

## Confusing numbers (these become tooltips / explanations in the dashboard)
Seed (from the built-in glossary — run `python fastarena.py glossary`):
- "CI half-width": a win needs mean minus this above 0, so narrow = more wins, fewer chops.
- "chop": a draw, not a tie we earned — our edge was not proven at 95%.
- Add your own as they come up:
- [ ] ...

## Wishlist (append freely, with time + initials)
- [ ] (example) 2:10pm SM — a bar of chips won by street, so I can see we leak on the river.
- [ ] ...

## Explicitly out of scope (so Claude Code does not gold-plate)
- No login, no database, no server unless "live" is chosen above.
- No real-time streaming unless we decide the dashboard is live.
- Nothing that the plain-text report or a static HTML report already answers well enough.

## Data the dashboard will read (keep this accurate as the arena evolves)
- `fastarena.py match ... --json out.json` writes the full match result, including every hand
  (`logs`), the confidence interval, per-half stats (time, errors, forfeits).
- A field or tournament run can be dumped to JSON the same way (add `--json` there if needed).
- So the dashboard's input is one or more of these JSON files. Note the exact shape here once
  we have a file in hand:
- [ ] fields present in out.json that we care about: ...

## Draft hand-off prompt for Claude Code (refine at end of day)
> Build a single self-contained HTML dashboard (no server, no external calls) that reads a
> results JSON produced by fastarena.py. It must be explanatory for a teammate who does not
> know poker or statistics: every metric shows a one-line definition and what a good value is.
> Panels (final list from this file): ... . Input JSON shape: ... . Light and dark mode.
> Do not add features beyond this list.
