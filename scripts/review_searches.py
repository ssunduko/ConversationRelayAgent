"""What callers asked, and what the knowledge base could not answer.

Every knowledge lookup is stored on the conversation record. This reads them
back so you can see which questions came up and which ones found nothing —
the shortlist of what to add to data/features.json or the website.

    python scripts/review_searches.py              # unanswered first
    python scripts/review_searches.py --all        # every search
    python scripts/review_searches.py --calls      # grouped by call
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import sqlite3
from collections import Counter

from core.config import SETTINGS


def load(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT call_sid, phone, mode, started_at, searches FROM conversations"
        " WHERE searches IS NOT NULL AND searches != '[]' ORDER BY id DESC"
    ).fetchall()
    calls = []
    for row in rows:
        try:
            searches = json.loads(row["searches"])
        except (TypeError, ValueError):
            continue
        if searches:
            calls.append({**dict(row), "searches": searches})
    return calls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Show answered searches too")
    parser.add_argument("--calls", action="store_true", help="Group by call instead of by query")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    db = Path(SETTINGS.database_path)
    if not db.exists():
        sys.exit(f"No database at {db}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    calls = load(conn)
    conn.close()

    if not calls:
        print("No searches recorded yet.")
        print("They are written when a call ends, on demo calls that use the knowledge base.")
        return 0

    every = [s for call in calls for s in call["searches"]]
    answered = [s for s in every if s.get("answered")]
    failed = [s for s in every if not s.get("answered") and s.get("error")]
    missed = [s for s in every if not s.get("answered") and not s.get("error")]

    print(f"{len(every)} search(es) across {len(calls)} call(s)")
    print(f"  {len(answered)} found something, {len(missed)} found nothing")
    if failed:
        print(f"  {len(failed)} FAILED (outage, not a knowledge gap):")
        for s in failed[:5]:
            print(f"      {s['query']}  -> {s['error']}")
    print()

    if args.calls:
        for call in calls[: args.limit]:
            print(f"{call['started_at'][:16]}  {call['phone']}  [{call['mode']}]  {call['call_sid']}")
            for s in call["searches"]:
                mark = " " if s.get("answered") else "!"
                score = f"{s['best_score']:.3f}" if s.get("best_score") is not None else "  -  "
                print(f"  {mark} {score}  {s['query']}")
                if s.get("top"):
                    print(f"           -> {s['top']}")
            print()
        return 0

    if missed:
        print("Found nothing — these are the gaps:")
        for query, count in Counter(s["query"] for s in missed).most_common(args.limit):
            print(f"  {count:>3}x  {query}")
        print()

    if args.all and answered:
        print("Answered:")
        for s in answered[: args.limit]:
            print(f"  {s['best_score']:.3f}  {s['query']}")
            print(f"         -> [{s.get('source', '?')}] {s.get('top', '')}")
    elif answered:
        weakest = sorted(answered, key=lambda s: s["best_score"])[:10]
        print("Weakest answers (closest to being missed):")
        for s in weakest:
            print(f"  {s['best_score']:.3f}  {s['query']}")
            print(f"         -> [{s.get('source', '?')}] {s.get('top', '')}")
        print("\n  (--all shows every search)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
