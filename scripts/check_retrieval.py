"""Spot-check what the demo agent will find for real prospect questions.

Queries are paced to stay inside the Voyage free-tier limit (3 requests per
minute). Raise --rpm once billing is enabled and this runs in seconds.

    python scripts/check_retrieval.py
    python scripts/check_retrieval.py --rpm 60 --query "does it do MMS?"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio

from services.knowledge import KNOWLEDGE

DEFAULT_QUERIES = [
    "how much does it cost per month?",
    "is it HIPAA compliant for healthcare?",
    "can I text from Salesforce?",
    "do you have an AI agent that books meetings?",
    "can I make phone calls or is it just texting?",
    "do you integrate with SAP ERP?",
    "what is the airspeed velocity of a swallow?",
]


async def run(queries: list[str], rpm: int, top_k: int) -> int:
    if not KNOWLEDGE.ready:
        sys.exit("No index. Run: python scripts/build_index.py")

    print(f"{KNOWLEDGE.size} documents indexed\n")
    interval = 60.0 / max(rpm, 1)
    misses = 0

    for i, query in enumerate(queries):
        if i:
            await asyncio.sleep(interval)
        hits = await KNOWLEDGE.search(query, top_k=top_k)
        print(f"Q: {query}")
        if not hits and KNOWLEDGE.last_error:
            # Not a knowledge gap — the lookup never happened.
            print(f"   SEARCH FAILED: {KNOWLEDGE.last_error}")
            print("   (rate limited? free tier allows 3 requests/minute)")
        elif not hits:
            misses += 1
            print("   (nothing above the score threshold — agent defers to the demo)")
        for hit in hits:
            title = (hit.get("title") or "")[:60]
            print(f"   {hit['score']:.3f}  [{hit['source']}] {title}")
        print()
        sys.stdout.flush()

    print(f"{len(queries) - misses}/{len(queries)} queries found something.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Ask your own (repeatable)")
    parser.add_argument("--rpm", type=int, default=3, help="Queries per minute (free tier is 3)")
    parser.add_argument("--top-k", type=int, default=2)
    args = parser.parse_args()
    return asyncio.run(run(args.query or DEFAULT_QUERIES, args.rpm, args.top_k))


if __name__ == "__main__":
    raise SystemExit(main())
