"""tools/changelog.py - record what changed in a bot, why, and what it did to the score.

Every iteration on a bot (structure fix, tuned values, new opponents that change the
picture) gets one entry. The dashboard shows them as a timeline. The file is committed.

    python tools/changelog.py add --author Claude --bot mybot_v2 \\
        --title "Separate fold-to-re-raise rate" \\
        --what "Tracks how often opponents fold to a re-raise, apart from a first bet." \\
        --why "Re-raises were assumed to get folds like bets: predicted 33%, actual 1%." \\
        --before "5.25 of 8" --after "6.50 of 8" --evidence "same-deals comparison with v1"
    python tools/changelog.py list
"""
import argparse
import datetime
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "changelog.json")


def load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def add(entry):
    entries = load()
    entry.setdefault("date", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    try:
        entry.setdefault("commit", subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                                           stderr=subprocess.DEVNULL).decode().strip())
    except Exception:
        pass
    entries.append(entry)
    with open(PATH, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    return entry


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    for k in ("author", "bot", "title", "what", "why"):
        a.add_argument(f"--{k}", required=True)
    for k in ("before", "after", "evidence", "kind"):
        a.add_argument(f"--{k}", default="")
    sub.add_parser("list")
    args = p.parse_args(argv)
    if args.cmd == "add":
        e = add({k: getattr(args, k) for k in ("author", "bot", "title", "what", "why", "before", "after", "evidence", "kind")
                 if getattr(args, k)})
        print("added:", e["title"])
    else:
        for e in load():
            print(f"{e.get('date', '')}  {e.get('author', ''):11} {e.get('bot', ''):9} {e['title']}  {e.get('before', '')} -> {e.get('after', '')}")


if __name__ == "__main__":
    main()
