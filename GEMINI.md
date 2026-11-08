# Gemini: how to improve v1

You improve v1 (the shipped poker bot) by trying new values for its existing settings.
You never edit code. One tool does everything: `tools/loop.py`. A strict automatic gate
decides whether a change is kept. Follow the steps exactly.

Before every command, set your name so the dashboard shows your runs:

    export RUN_OWNER=Gemini

## The loop (repeat)

1. **See where things stand.**

       .venv/bin/python tools/loop.py status

   It lists every setting, its shipped value, and the range you may use.

2. **Find the biggest leak.**

       .venv/bin/python tools/loop.py leaks --worst 5

   Read the first numbered line. It says what v1 does wrong (calls too much, or folds
   too much), what it cost, and which settings move it.

3. **Pick ONE setting from that line and move it a small step: 0.02 to 0.05.**
   - "fold less": make v1 call more. Lower `tight_call_penalty`, or raise `aggro_margin`.
   - "call less": make v1 call less. Raise `tight_call_penalty`, or lower `aggro_margin`.
   - Other settings: think about which direction fixes the line you read, and write one
     sentence explaining why before you run anything.

4. **Screen it (about 3 minutes).**

       .venv/bin/python tools/loop.py try tight_call_penalty=0.17

   - "Not promising": go back to step 3. Try the other direction, or another setting from
     the same line. After 3 failed screens on one leak, go back to step 2 with
     `--worst 8` and pick a different leak.
   - "Promising": go to step 5.

5. **Gate it (about 25 minutes). Run it and wait. Do not start anything else.**

       .venv/bin/python tools/loop.py gate

   - **REJECT**: it is logged automatically. Go back to step 3. Never retry the same values.
   - **KEEP**: stop. Tell the human exactly this:
     "Gate KEEP for <settings>. Please run `.venv/bin/python tools/loop.py promote`."
     Then wait for the human before starting a new loop.

## Rules (never break these)

- Never edit any file. Not `mybot.py`, not the tools, not this file. If something seems
  broken, stop and describe the error message to the human word for word.
- Never run `tools/loop.py promote`. Only a human can.
- Never run two commands at once. Every command waits its turn in a shared queue; that is
  normal. "[queue] waiting for ..." means wait.
- Never use values outside the range `status` shows. The tool will refuse them anyway.
- Change one setting per try. Two at once is allowed only if a single change already
  passed the screen and you want to add a second to it.
- Do not argue with the gate. If it says REJECT, the change is not better, even if the
  screen looked good. Small differences are usually noise.
- Report honestly: after each gate, tell the human the verdict and the lines it printed.

## What the gate checks (for your understanding)

It plays the full field (8 seeds) and 80 random opponent styles that have never been used
before (a new set every gate). KEEP needs a real gain (field +0.5 points, or swarm average
+0.08 bb per deal) and nothing worse beyond noise: no opponent family, the swarm average,
or the swarm's worst 10% of styles. Every run appears in the dashboard's runs list
(http://localhost:8050) with your name on it.
