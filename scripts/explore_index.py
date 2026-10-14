"""Explore the local vector store.

The index is 628 vectors in data/knowledge.npz. Most of what you'd want to know
about it needs no API calls at all — neighbours, duplicates and clusters all
come from vectors already on disk. Only `search` spends a Voyage request.

    python scripts/explore_index.py stats
    python scripts/explore_index.py list --source salesmsg.com
    python scripts/explore_index.py show feature-001
    python scripts/explore_index.py similar feature-001        # free
    python scripts/explore_index.py duplicates                 # free
    python scripts/explore_index.py outliers                   # free
    python scripts/explore_index.py search "does it do MMS?"   # 1 Voyage request
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
from collections import Counter

import numpy as np

from core.config import SETTINGS


def load() -> tuple[np.ndarray, list[dict], str]:
    path = Path(SETTINGS.knowledge_index_path)
    if not path.exists():
        sys.exit(f"No index at {path}. Run: python scripts/build_index.py")
    z = np.load(path, allow_pickle=False)
    return z["vectors"], json.loads(str(z["documents"])), str(z["model"])


def label(doc: dict, width: int = 52) -> str:
    title = doc.get("title") or doc.get("id") or ""
    return title[:width]


def find(docs: list[dict], needle: str) -> int:
    """Locate a document by id, exact title, or position."""
    if needle.isdigit() and int(needle) < len(docs):
        return int(needle)
    for i, d in enumerate(docs):
        if d.get("id") == needle:
            return i
    lowered = needle.lower()
    matches = [i for i, d in enumerate(docs) if lowered in (d.get("title") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"No document matches {needle!r}")
    print(f"{needle!r} matches {len(matches)} documents:")
    for i in matches[:15]:
        print(f"  {docs[i].get('id', i)}  {label(docs[i])}")
    sys.exit(1)


# --- commands ------------------------------------------------------------


def cmd_stats(vectors, docs, model, args) -> None:
    print(f"{len(docs)} documents, {vectors.shape[1]} dimensions, model {model}")
    print(f"{Path(SETTINGS.knowledge_index_path).stat().st_size/1024:.0f} KB on disk, "
          f"{vectors.nbytes/1024/1024:.1f} MB in memory\n")

    for source, count in Counter(d["source"] for d in docs).most_common():
        chars = sum(len(d["text"]) for d in docs if d["source"] == source)
        print(f"  {count:>4} docs  {chars:>8,} chars  {source}")

    categories = Counter(d.get("category", "") for d in docs if d.get("category"))
    if categories:
        print("\nfeature categories:")
        for name, count in categories.most_common():
            print(f"  {count:>4}  {name}")

    lengths = np.array([len(d["text"]) for d in docs])
    print(f"\ndocument length: min={lengths.min()} median={int(np.median(lengths))} "
          f"max={lengths.max()} chars")

    # How spread out the corpus is. A mean near 1.0 would mean everything looks
    # alike and retrieval can't discriminate.
    sample = vectors[np.random.default_rng(0).choice(len(docs), min(200, len(docs)), replace=False)]
    sims = sample @ sample.T
    off = sims[~np.eye(len(sample), dtype=bool)]
    print(f"pairwise similarity (200 sampled): mean={off.mean():.3f} p95={np.percentile(off, 95):.3f}")


def cmd_list(vectors, docs, model, args) -> None:
    rows = [
        (i, d) for i, d in enumerate(docs)
        if (not args.source or args.source.lower() in d["source"].lower())
        and (not args.category or args.category.lower() in (d.get("category") or "").lower())
        and (not args.contains or args.contains.lower() in d["text"].lower())
    ]
    print(f"{len(rows)} matching document(s)\n")
    for i, d in rows[: args.limit]:
        print(f"  [{i:>3}] {d.get('id', ''):<28} {label(d)}")
    if len(rows) > args.limit:
        print(f"\n  ... {len(rows) - args.limit} more (--limit)")


def cmd_show(vectors, docs, model, args) -> None:
    i = find(docs, args.document)
    d = docs[i]
    print(f"[{i}] {d.get('id', '')}")
    for key in ("source", "category", "subcategory", "title", "url", "is_new"):
        if d.get(key) not in (None, "", False):
            print(f"  {key:11} {d[key]}")
    print(f"\n{d['text']}\n")
    print(f"vector: {vectors[i][:6].round(4).tolist()} ... ({vectors.shape[1]} dims, "
          f"norm {np.linalg.norm(vectors[i]):.6f})")


def cmd_similar(vectors, docs, model, args) -> None:
    """Nearest neighbours — no API call, the vectors are already here."""
    i = find(docs, args.document)
    scores = vectors @ vectors[i]
    order = np.argsort(-scores)[: args.limit + 1]
    print(f"nearest to [{i}] {label(docs[i])}\n")
    for j in order:
        if int(j) == i:
            continue
        print(f"  {scores[j]:.4f}  [{docs[int(j)]['source']:<17}] {label(docs[int(j)])}")


def cmd_duplicates(vectors, docs, model, args) -> None:
    """Near-identical chunks waste prompt space and crowd out real answers."""
    print(f"pairs above {args.threshold}\n")
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, 0.0)
    pairs = np.argwhere(np.triu(sims) >= args.threshold)

    if not len(pairs):
        print("  none — no redundant chunks at this threshold")
        return
    for a, b in pairs[: args.limit]:
        print(f"  {sims[a, b]:.4f}")
        print(f"    [{docs[int(a)]['source']:<17}] {label(docs[int(a)])}")
        print(f"    [{docs[int(b)]['source']:<17}] {label(docs[int(b)])}")
    print(f"\n  {len(pairs)} pair(s) total")


def cmd_outliers(vectors, docs, model, args) -> None:
    """Documents unlike everything else — often boilerplate or junk chunks."""
    centroid = vectors.mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    scores = vectors @ centroid
    order = np.argsort(scores)[: args.limit]
    print("least like the rest of the corpus (candidates for removal):\n")
    for i in order:
        d = docs[int(i)]
        print(f"  {scores[i]:.4f}  [{d['source']:<17}] {label(d)}")
        print(f"           {d['text'][:100].strip()}...")


def cmd_search(vectors, docs, model, args) -> None:
    """The only command that spends a Voyage request."""
    import voyageai

    if not SETTINGS.voyage_api_key:
        sys.exit("VOYAGE_API_KEY is not set.")
    q = voyageai.Client(api_key=SETTINGS.voyage_api_key).embed(
        [args.query], model=model, input_type="query"
    ).embeddings[0]
    q = np.asarray(q, dtype=np.float32)
    q /= np.linalg.norm(q)

    scores = vectors @ q
    print(f'"{args.query}"\n')
    for i in np.argsort(-scores)[: args.limit]:
        d = docs[int(i)]
        kept = "" if scores[i] >= SETTINGS.knowledge_min_score else "  (below threshold)"
        print(f"  {scores[i]:.4f}  [{d['source']:<17}] {label(d)}{kept}")
    print(f"\n  threshold is {SETTINGS.knowledge_min_score}; the agent sees the top "
          f"{SETTINGS.knowledge_top_k} above it")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Size, sources, categories, spread")

    p = sub.add_parser("list", help="Browse documents")
    p.add_argument("--source"); p.add_argument("--category"); p.add_argument("--contains")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("show", help="One document in full")
    p.add_argument("document", help="id, title, or index")

    p = sub.add_parser("similar", help="Nearest neighbours (no API call)")
    p.add_argument("document"); p.add_argument("--limit", type=int, default=8)

    p = sub.add_parser("duplicates", help="Near-identical chunks (no API call)")
    p.add_argument("--threshold", type=float, default=0.95); p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("outliers", help="Documents unlike the rest (no API call)")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("search", help="Semantic search (1 Voyage request)")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=8)

    args = parser.parse_args()
    vectors, docs, model = load()
    globals()[f"cmd_{args.command}"](vectors, docs, model, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
