# Project design identity: poker bot lab dashboard

Fills every slot of the canon's slot contract (`~/.claude/design-system/DESIGN.md` §3).
Chosen 2026-11-07: "Oscilloscope lab" direction.

## Personality
A lab instrument reading a live opponent model: you watch the bot's beliefs move and
see exactly why it acted. Adjectives: **calibrated, signal-first, unhurried**.

## Palette
| Token | Light | Dark | Meaning |
|---|---|---|---|
| base | `#E8EDF1` | `#0E1824` | page ground: cool blue-grey / deep navy |
| surface | `#F6F8FA` | `#152230` | instrument panels |
| ink | `#12233A` | `#DCE5EE` | primary text |
| ink-muted | `#50627A` | `#8EA0B3` | labels, secondary values |
| rule | `#C9D2DB` | `#2A3A4B` | dividers, axes, grid |
| **cobalt** (accent) | `#2747B8` | `#6C84F0` | **the model's current belief and its decision**: the live estimate line, the chosen action. Nothing else. |
| prior | `#6B7A8D` | `#7F90A4` | the population prior (always dashed) |
| gain | `#1F7A57` | `#3FA37A` | state: positive EV, chips won, a significant win |
| loss | `#C24A2C` | `#E0704F` | state: negative EV, chips lost, a loss or error |

Playing cards (content, not data): face `#FFFFFF` light / `#E9EEF3` dark (cards stay light, like
real cards), black suits in ink `#12233A`, red suits `#B8323E`. Used only inside card graphics.

Validated with the dataviz validator (2026-11-07): gain/loss sit in the 6-8 colour-blind band, so
every value is also signed (+/-) and positioned above/below zero. No gradients, no decorative colour. Cobalt appears only where the model "speaks".

## Type
- Display and UI: **IBM Plex Sans** (600 for panel titles and the focal number, 400 for text).
- Numbers: Plex Sans with `font-variant-numeric: tabular-nums`. No monospace for labels.
- The focal number on each panel (e.g. the chosen action's EV) is set large (28-32px, 600);
  everything around it stays at 12-14px. Sentence-case headings; no tracked all-caps eyebrows.

## Density
Instrument-dense: closer to a terminal than an editorial page. Panels tile edge-to-edge on a
strong grid with 1px rules between them; values beat whitespace. Mobile stacks panels.

## Voice
Plain and explanatory: every metric carries a one-line meaning on hover or underneath.
- Empty: "No trace yet. Run a traced match to watch the model think."
- Error: "Trace failed: 12 decisions raised errors. The first is shown below; the bot would
  have silently checked on those hands."
- Labels name what the user understands: "How often they fold to a bet", not `fold_to_bet`.

## Motion
Restrained. Spent on one moment: stepping or playing through decisions, where the diagram's
values and the posterior markers move to the new decision (about 180 ms). Nothing animates on
load; `prefers-reduced-motion` turns it off.

## Signature move
The **live model diagram**: cards and opponent actions flow through posteriors, range and
equity into an EV bar per action, with the chosen action lit in cobalt. A scrubber replays
the half, and the posterior gauges visibly drift from the dashed prior toward what the bot has
learned about this opponent.
