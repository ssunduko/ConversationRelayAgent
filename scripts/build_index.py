"""Build the vector index the demo agent searches during a call.

Two sources:
  data/features.json   261 product features, already one document each
  salesmsg.com         crawled, same-domain, text extracted and chunked

Embeds everything with Voyage and writes a single .npz the app loads at
startup. Re-run it whenever the features file or the website changes.

    python scripts/build_index.py
    python scripts/build_index.py --no-crawl        # features only, no network
    python scripts/build_index.py --max-pages 100
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import re
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import numpy as np
from bs4 import BeautifulSoup

from core.config import PROJECT_ROOT, SETTINGS

FEATURES_PATH = PROJECT_ROOT / "data" / "features.json"
UA = "SalesmsgDemoAgent/1.0 (+internal knowledge index build)"

# Long pages get split so a hit points at the relevant passage rather than a
# whole page of navigation and footer.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

# Legal boilerplate is the largest thing on the site and the least useful on a
# sales call — the Messaging Service Agreement alone contributed 57 chunks of
# arbitration clauses, competing for retrieval slots against real answers.
# Security and compliance pages are deliberately NOT here: prospects do ask
# about HIPAA and SOC 2.
DEFAULT_EXCLUDES = (
    r"/terms-of-use",
    r"/privacy-policy",
    r"/messaging-service-agreement",
    r"/acceptable-use-policy",
    r"/billing-policy",
    r"/cookie",
    r"/dpa\b",
    r"/legal",
    r"-old$",  # /product-updates-old and friends
)


# --- feature documents ---------------------------------------------------


def load_features() -> list[dict]:
    if not FEATURES_PATH.exists():
        print(f"  {FEATURES_PATH} not found — skipping features")
        return []

    payload = json.loads(FEATURES_PATH.read_text(encoding="utf-8"))
    docs = []
    for item in payload.get("documents", []):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        docs.append(
            {
                "id": item.get("id", ""),
                "text": text,
                "title": item.get("feature", ""),
                "source": "product features",
                "category": item.get("category", ""),
                "subcategory": item.get("subcategory", ""),
                "is_new": bool(item.get("is_new")),
            }
        )
    print(f"  {len(docs)} feature documents from {FEATURES_PATH.name}")
    return docs


# --- website crawl -------------------------------------------------------


def extract(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    text = re.sub(r"[ \t]+", " ", soup.get_text("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def chunk(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = start + size
        # Prefer a paragraph boundary so chunks don't cut mid-sentence.
        split = text.rfind("\n\n", start + size // 2, end)
        if split == -1 or end >= len(text):
            split = end
        out.append(text[start:split].strip())
        start = max(split - overlap, start + 1)
    return [c for c in out if len(c) > 80]


def resolve_base(url: str) -> str:
    """Follow redirects to the site's real home.

    salesmsg.com is a vanity domain that 301s to www.salesmessage.com. Crawling
    the pre-redirect host finds one page and stops.
    """
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
            final = str(client.get(url).url)
    except Exception as exc:
        print(f"  could not resolve {url} ({exc}); using it as given")
        return url

    if urlparse(final).netloc != urlparse(url).netloc:
        print(f"  {url} redirects to {final}")
    # Drop any tracking query the redirect added.
    parsed = urlparse(final)
    return f"{parsed.scheme}://{parsed.netloc}/"


def load_robots(base_url: str) -> RobotFileParser:
    """Read robots.txt for the host we are actually crawling.

    RobotFileParser.read() disallows everything when the fetch fails, and it
    follows cross-host redirects — salesmsg.com/robots.txt lands on a URL
    shortener whose "Disallow: /" is not this site's policy. So fetch it here
    and only honour it when it came from the same host.
    """
    robots = RobotFileParser()
    host = urlparse(base_url).netloc
    url = f"{urlparse(base_url).scheme}://{host}/robots.txt"

    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=15, follow_redirects=True) as client:
            resp = client.get(url)
    except Exception as exc:
        print(f"  no robots.txt ({exc}); proceeding")
        robots.parse([])
        return robots

    if urlparse(str(resp.url)).netloc != host:
        print(f"  robots.txt redirected off-host to {resp.url} — ignoring it")
        robots.parse([])
    elif resp.status_code == 200:
        robots.parse(resp.text.splitlines())
        print(f"  robots.txt loaded from {host}")
    else:
        # 404 and friends mean "no rules", which is permission.
        print(f"  no robots.txt (HTTP {resp.status_code}); proceeding")
        robots.parse([])
    return robots


def strip_boilerplate(pages: list[dict], min_share: float = 0.3) -> tuple[list[dict], list[str]]:
    """Remove text blocks that appear on many pages.

    Sites render navigation, CTAs and footers as plain divs, so tag-based
    stripping misses them: "Sign In / Sign Up / Get a Demo / Questions? Text
    us" survived into the first chunk of every page. That dilutes each page's
    vector with text identical to every other page's — which is why the calling
    page lost to a one-line feature doc, and why de-duplication was finding
    similarity 1.000 between unrelated pages.

    Anything appearing on a third or more of the pages is furniture by
    definition, whatever it says.
    """
    if len(pages) < 4:
        return pages, []

    counts: dict[str, int] = {}
    for page in pages:
        for line in {ln.strip() for ln in page["lines"] if ln.strip()}:
            counts[line] = counts.get(line, 0) + 1

    cutoff = max(3, int(len(pages) * min_share))
    boilerplate = {line for line, n in counts.items() if n >= cutoff}

    for page in pages:
        page["lines"] = [ln for ln in page["lines"] if ln.strip() not in boilerplate]

    dropped = sorted(boilerplate, key=lambda l: -counts[l])
    print(f"  stripped {len(boilerplate)} boilerplate block(s) seen on >= {cutoff} of {len(pages)} pages:")
    for line in dropped[:10]:
        preview = line if len(line) <= 58 else line[:58] + "…"
        print(f"    {counts[line]:>3} pages  {preview!r}")
    if len(dropped) > 10:
        print(f"    ... and {len(dropped) - 10} more")
    return pages, dropped


def crawl(base_url: str, max_pages: int, delay: float, excludes: tuple[str, ...]) -> list[dict]:
    base_url = resolve_base(base_url)
    domain = urlparse(base_url).netloc
    robots = load_robots(base_url)
    skip = [re.compile(p, re.I) for p in excludes]

    def excluded(url: str) -> bool:
        path = urlparse(url).path or "/"
        return any(rx.search(path) for rx in skip)

    def allowed(url: str) -> bool:
        try:
            return robots.can_fetch(UA, url)
        except Exception:
            return True

    seen: set[str] = set()
    queue: deque[str] = deque([base_url])
    pages: list[dict] = []
    excluded_count = [0]

    with httpx.Client(
        headers={"User-Agent": UA}, timeout=20, follow_redirects=True
    ) as client:
        while queue and len(seen) < max_pages:
            url = urldefrag(queue.popleft())[0].rstrip("/") or base_url
            if url in seen:
                continue
            if excluded(url):
                excluded_count[0] += 1
                seen.add(url)
                continue
            if not allowed(url):
                print(f"  skip (robots): {url}")
                seen.add(url)
                continue
            seen.add(url)

            try:
                resp = client.get(url)
            except Exception as exc:
                print(f"  skip ({type(exc).__name__}): {url}")
                continue
            if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                continue

            title, text = extract(resp.text)
            # Hold the raw lines: boilerplate can only be identified once every
            # page has been seen, so chunking has to wait.
            pages.append({"url": url, "title": title, "lines": text.split("\n")})
            print(f"  [{len(seen):>3}/{max_pages}] {len(text):>6} chars  {url}")

            for anchor in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
                link = urldefrag(urljoin(url, anchor["href"]))[0]
                parsed = urlparse(link)
                # Same site only, and skip anything that isn't a page.
                if parsed.netloc != domain or parsed.scheme not in ("http", "https"):
                    continue
                if re.search(r"\.(pdf|png|jpe?g|gif|svg|zip|mp4|webp|css|js)$", parsed.path, re.I):
                    continue
                if link.rstrip("/") not in seen:
                    queue.append(link)

            time.sleep(delay)  # be a polite guest on your own marketing site

    print(f"  fetched {len(pages)} page(s); skipped {excluded_count[0]} excluded page(s)")

    pages, _ = strip_boilerplate(pages)

    docs: list[dict] = []
    for page in pages:
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(page["lines"])).strip()
        for i, piece in enumerate(chunk(text)):
            docs.append(
                {
                    "id": f"{page['url']}#{i}",
                    "text": piece,
                    "title": page["title"],
                    "source": "salesmsg.com",
                    "url": page["url"],
                }
            )
    print(f"  {len(docs)} chunks after stripping boilerplate")
    return docs


def deduplicate(docs: list[dict], vectors: np.ndarray, threshold: float) -> tuple[list[dict], np.ndarray]:
    """Drop documents that say the same thing as one we're already keeping.

    features.json describes several capabilities more than once under different
    names ("Shared Inbox" and "Team Collaboration and Shared Inboxes" scored
    0.94). Near-duplicates crowd the top-5, so one query returns three phrasings
    of a single answer instead of three distinct facts.

    Longest text wins, on the assumption it is the fullest description.
    """
    if threshold <= 0 or len(docs) < 2:
        return docs, vectors

    order = sorted(range(len(docs)), key=lambda i: -len(docs[i]["text"]))
    kept: list[int] = []
    dropped: list[tuple[int, int, float]] = []

    for i in order:
        if kept:
            sims = vectors[kept] @ vectors[i]
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                dropped.append((i, kept[best], float(sims[best])))
                continue
        kept.append(i)

    if not dropped:
        print(f"  no near-duplicates above {threshold}")
        return docs, vectors

    kept.sort()  # keep the original ordering for readability
    print(f"  dropped {len(dropped)} near-duplicate(s) above {threshold}:")
    for i, keeper, score in sorted(dropped, key=lambda d: -d[2])[:8]:
        print(f"    {score:.3f}  {(docs[i].get('title') or docs[i]['id'])[:44]}")
        print(f"           kept instead: {(docs[keeper].get('title') or docs[keeper]['id'])[:44]}")
    if len(dropped) > 8:
        print(f"    ... and {len(dropped) - 8} more")

    return [docs[i] for i in kept], vectors[kept]


# --- embedding -----------------------------------------------------------


def embed_all(docs: list[dict], model: str, tpm: int, rpm: int) -> np.ndarray:
    """Embed everything, staying inside the account's rate limits.

    Voyage's free tier without a payment method allows only 3 requests and
    10,000 tokens per minute, so this batches by estimated tokens and paces
    requests rather than firing a hundred documents at once.
    """
    import voyageai
    from voyageai.error import RateLimitError

    client = voyageai.Client(api_key=SETTINGS.voyage_api_key)
    vectors: list[list[float]] = []

    # Leave headroom: the estimate is approximate and the limit is not.
    budget = max(int(tpm * 0.8), 1000)
    interval = 60.0 / max(rpm, 1)

    def size(text: str) -> int:
        return max(len(text) // 4, 1)  # ~4 characters per token

    batches: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0
    for i, doc in enumerate(docs):
        tokens = size(doc["text"])
        if current and current_tokens + tokens > budget:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(i)
        current_tokens += tokens
    if current:
        batches.append(current)

    total_tokens = sum(size(d["text"]) for d in docs)
    eta = len(batches) * interval / 60
    print(
        f"  ~{total_tokens:,} tokens in {len(batches)} batch(es); "
        f"{rpm} req/min limit means roughly {eta:.1f} min"
    )

    done = 0
    for n, batch in enumerate(batches):
        if n:
            time.sleep(interval)

        texts = [docs[i]["text"] for i in batch]
        for attempt in range(5):
            try:
                result = client.embed(texts, model=model, input_type="document")
                break
            except RateLimitError:
                wait = interval * (attempt + 1)
                print(f"    rate limited; waiting {wait:.0f}s")
                time.sleep(wait)
        else:
            raise SystemExit(
                "Voyage kept rate limiting. Add a payment method to lift the free-tier\n"
                "limits (the 200M free tokens still apply), or re-run with --rpm 1."
            )

        vectors.extend(result.embeddings)
        done += len(batch)
        print(f"  embedded {done}/{len(docs)}")

    array = np.asarray(vectors, dtype=np.float32)
    # Normalize once here so search is a plain dot product per query.
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-crawl", action="store_true", help="Index features only")
    parser.add_argument("--base-url", default=SETTINGS.knowledge_site_url)
    parser.add_argument("--max-pages", type=int, default=SETTINGS.knowledge_max_pages)
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between page fetches")
    parser.add_argument("--out", default=str(SETTINGS.knowledge_index_path))
    # Voyage free tier without a payment method: 3 RPM / 10K TPM. Raise both
    # once billing is enabled and this finishes in seconds instead of minutes.
    parser.add_argument("--rpm", type=int, default=3, help="Voyage requests per minute")
    parser.add_argument("--tpm", type=int, default=10000, help="Voyage tokens per minute")
    parser.add_argument(
        "--exclude", action="append", default=None,
        help=f"URL path pattern to skip (repeatable). Defaults: {', '.join(DEFAULT_EXCLUDES)}",
    )
    parser.add_argument("--keep-legal", action="store_true", help="Crawl legal pages too")
    parser.add_argument(
        "--dedupe-threshold", type=float, default=0.93,
        help="Drop documents this similar to one already kept; 0 disables",
    )
    args = parser.parse_args()

    if not SETTINGS.voyage_api_key:
        sys.exit(
            "VOYAGE_API_KEY is not set.\n"
            "Get one at https://dashboard.voyageai.com and put it in .env."
        )

    print("1. Product features")
    docs = load_features()

    if args.no_crawl:
        print("\n2. Website  (skipped)")
    else:
        excludes = () if args.keep_legal else tuple(args.exclude or DEFAULT_EXCLUDES)
        print(f"\n2. Crawling {args.base_url}")
        if excludes:
            print(f"  excluding: {', '.join(excludes)}")
        docs += crawl(args.base_url, args.max_pages, args.delay, excludes)

    if not docs:
        sys.exit("Nothing to index.")

    print(f"\n3. Embedding {len(docs)} documents with {SETTINGS.voyage_model}")
    vectors = embed_all(docs, SETTINGS.voyage_model, args.tpm, args.rpm)

    print(f"\n4. De-duplicating")
    before = len(docs)
    docs, vectors = deduplicate(docs, vectors, args.dedupe_threshold)
    if len(docs) != before:
        print(f"  {before} -> {len(docs)} documents")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        vectors=vectors,
        documents=json.dumps(docs, ensure_ascii=False),
        model=SETTINGS.voyage_model,
    )

    by_source: dict[str, int] = {}
    for doc in docs:
        by_source[doc["source"]] = by_source.get(doc["source"], 0) + 1

    print(f"\nWrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    for source, count in sorted(by_source.items()):
        print(f"  {count:>4} documents from {source}")
    print(f"  {vectors.shape[1]} dimensions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
