"""Vector search over the Salesmsg feature list and website.

The index is built offline by scripts/build_index.py and loaded here at
startup. Querying is a single Voyage embedding call plus a dot product over a
few hundred vectors — fast enough to sit inside a phone turn.

If the index is missing or Voyage is unreachable, retrieval returns nothing and
the agent falls back to "I'll cover that on the demo call", which is the
behaviour we want anyway.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from core.config import SETTINGS

log = logging.getLogger("relay.knowledge")

INDEX_PATH = SETTINGS.knowledge_index_path


class KnowledgeBase:
    def __init__(self, index_path: Path | None = None) -> None:
        self._path = Path(index_path or INDEX_PATH)
        self._vectors: np.ndarray | None = None
        self._documents: list[dict[str, Any]] = []
        self._model: str | None = None
        self._client: Any = None
        # Set when a search fails rather than genuinely finding nothing. An
        # empty result list means the same thing to a caller either way, and
        # confusing the two hides rate limiting as "we have no answer".
        self.last_error: str | None = None
        self._load()

    # --- loading ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            log.warning(
                "no knowledge index at %s — run `python scripts/build_index.py`",
                self._path,
            )
            return
        try:
            payload = np.load(self._path, allow_pickle=False)
            self._vectors = payload["vectors"].astype(np.float32)
            self._documents = json.loads(str(payload["documents"]))
            self._model = str(payload["model"])
        except Exception as exc:
            log.error("could not load the knowledge index: %s", exc)
            self._vectors, self._documents = None, []
            return

        log.info(
            "knowledge index: %d documents, %d dimensions, model %s",
            len(self._documents),
            self._vectors.shape[1] if self._vectors is not None else 0,
            self._model,
        )

    @property
    def ready(self) -> bool:
        return self._vectors is not None and bool(self._documents)

    @property
    def size(self) -> int:
        return len(self._documents)

    # --- querying --------------------------------------------------------

    def _voyage(self) -> Any:
        if self._client is None:
            import voyageai

            self._client = voyageai.AsyncClient(api_key=SETTINGS.voyage_api_key)
        return self._client

    async def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """Most relevant documents for a caller's question, best first."""
        top_k = top_k or SETTINGS.knowledge_top_k
        self.last_error = None

        if not self.ready or not query.strip():
            self.last_error = "no index loaded" if not self.ready else "empty query"
            return []
        if not SETTINGS.voyage_api_key:
            self.last_error = "VOYAGE_API_KEY is unset"
            log.warning("knowledge search disabled: %s", self.last_error)
            return []

        try:
            result = await self._voyage().embed(
                [query], model=self._model or SETTINGS.voyage_model, input_type="query"
            )
            vector = np.asarray(result.embeddings[0], dtype=np.float32)
        except Exception as exc:
            # Rate limiting lands here. Distinguishing it from "found nothing"
            # is the difference between a knowledge gap and an outage.
            self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.error("query embedding failed: %s", self.last_error)
            return []

        # Vectors are stored L2-normalized, so a dot product is cosine
        # similarity and the whole search is one matrix multiply.
        norm = np.linalg.norm(vector)
        if norm:
            vector = vector / norm
        scores = self._vectors @ vector

        top = np.argsort(-scores)[:top_k]
        hits = []
        for idx in top:
            score = float(scores[idx])
            if score < SETTINGS.knowledge_min_score:
                continue
            doc = dict(self._documents[int(idx)])
            doc["score"] = round(score, 4)
            hits.append(doc)
        return hits


KNOWLEDGE = KnowledgeBase()


def format_hits(hits: list[dict[str, Any]]) -> str:
    """Render search results for the model — compact, and honest about gaps."""
    if not hits:
        return (
            "Nothing in the knowledge base covers that. Tell the caller you'll "
            "make sure it's answered on the demo call rather than guessing."
        )

    lines = []
    for hit in hits:
        source = hit.get("source") or hit.get("category") or "Salesmsg"
        title = hit.get("title") or hit.get("feature") or ""
        header = f"[{source}]" + (f" {title}" if title else "")
        lines.append(f"{header}\n{hit.get('text', '').strip()}")
    return "\n\n".join(lines)
