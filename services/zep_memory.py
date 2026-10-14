"""Long-term memory in Zep: the product catalogue, and every caller's history.

Two kinds of graph:

  * One shared graph holding data/features.json, one episode per feature. It is
    synced at startup: new features are added, changed ones replaced, and ones
    dropped from the file removed — but only when the file is at least as new
    as what Zep already holds, so deploying an old checkout can't roll the
    catalogue back.

  * One Zep user per caller, keyed by phone number, so each caller is their
    own tenant with their own graph. Each call is a thread, and every turn is
    written to it as the call goes. Zep extracts facts from those turns, which
    is what lets the agent recognise a returning caller and answer what it
    already knows without a round trip to the account tools.

Pinned to zep-cloud 3.x, which addresses users, threads and graphs by our own
IDs. 4.x switches to Zep-generated UUIDs; everything that would change lives in
this module.

Zep is a nice-to-have on a live call. Every public method swallows its own
failures, logs them, and returns something empty — a Zep outage must never
cost the caller a turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from core.config import SETTINGS

log = logging.getLogger("relay.zep")

# Zep rejects messages over this many characters, and more than this many
# messages in one add_messages call.
MAX_MESSAGE_CHARS = 4096
MAX_MESSAGES_PER_CALL = 30


def user_id_for(phone: str) -> str:
    """One Zep user per caller. Digits only keeps the ID free of '+'."""
    digits = re.sub(r"\D", "", phone or "")
    return f"caller-{digits or 'unknown'}"


def thread_id_for(call_sid: str) -> str:
    return f"call-{call_sid}"


def feature_payload(item: dict[str, Any], product: str) -> dict[str, Any]:
    """The part of a feature that goes into Zep, flat so Zep can extract from it."""
    return {
        "product": product,
        "feature": item.get("feature", ""),
        "category": item.get("category", ""),
        "subcategory": item.get("subcategory", ""),
        "description": item.get("description") or item.get("text", ""),
        "is_new": bool(item.get("is_new")),
    }


def content_hash(payload: dict[str, Any]) -> str:
    # sort_keys: the same feature must hash the same however the dict was built.
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def plan_feature_sync(
    features: dict[str, dict[str, Any]],
    generated: str,
    existing: list[dict[str, Any]],
) -> dict[str, list]:
    """Work out what to add and delete. Pure, so it can be tested without Zep.

    `features` maps feature id -> {"payload", "hash"}. `existing` is what Zep
    holds: [{"uuid", "feature_id", "hash", "generated"}]. Returns
    {"add": [feature ids], "delete": [episode uuids], "kept": n, "stale": n}.
    """
    by_feature: dict[str, list[dict[str, Any]]] = {}
    for episode in existing:
        by_feature.setdefault(episode.get("feature_id") or "", []).append(episode)

    add: list[str] = []
    delete: list[str] = []
    kept = stale = 0

    for feature_id, feature in features.items():
        episodes = by_feature.pop(feature_id, [])
        # Newest first; anything after the first is a leftover from an
        # interrupted replace and goes regardless.
        episodes.sort(key=lambda e: e.get("generated") or "", reverse=True)
        current, extras = (episodes[0] if episodes else None), episodes[1:]
        delete += [e["uuid"] for e in extras]

        if current is None:
            add.append(feature_id)
        elif current.get("hash") == feature["hash"]:
            kept += 1
        elif generated >= (current.get("generated") or ""):
            delete.append(current["uuid"])
            add.append(feature_id)
        else:
            # Zep holds a newer version than this file. Leave it alone.
            stale += 1

    # Features no longer in the file — removed only if the file is newer.
    for feature_id, episodes in by_feature.items():
        for episode in episodes:
            if not feature_id or generated >= (episode.get("generated") or ""):
                delete.append(episode["uuid"])
            else:
                stale += 1

    return {"add": add, "delete": delete, "kept": kept, "stale": stale}


class ZepMemory:
    def __init__(self) -> None:
        self._client: Any = None
        self._known_users: set[str] = set()
        self.sync_task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return bool(SETTINGS.zep_api_key)

    def client(self) -> Any:
        if self._client is None:
            # Imported lazily so the app runs without the package when Zep is off.
            from zep_cloud.client import AsyncZep

            self._client = AsyncZep(api_key=SETTINGS.zep_api_key)
        return self._client

    # --- product catalogue ------------------------------------------------

    def start_feature_sync(self, path: Path | None = None) -> None:
        """Sync in the background: ~260 episodes is minutes of work, and calls
        can be answered meanwhile (catalogue search just returns less)."""
        if not (self.configured and SETTINGS.zep_sync_features):
            return
        self.sync_task = asyncio.create_task(self.sync_features(path))

    async def sync_features(self, path: Path | None = None) -> dict[str, int] | None:
        path = Path(path or SETTINGS.zep_features_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("feature sync skipped: cannot read %s: %s", path, exc)
            return None

        generated = str(data.get("generated") or "")
        product = str(data.get("product") or "Salesmsg")
        features: dict[str, dict[str, Any]] = {}
        for item in data.get("documents", []):
            if not item.get("id"):
                continue
            payload = feature_payload(item, product)
            features[item["id"]] = {"payload": payload, "hash": content_hash(payload)}

        graph_id = SETTINGS.zep_features_graph_id
        try:
            await self._ensure_graph(graph_id)
            existing = await self._feature_episodes(graph_id, expected=len(features))
            plan = plan_feature_sync(features, generated, existing)

            # Bounded: the SDK retries 429s, but there is no point provoking them.
            gate = asyncio.Semaphore(SETTINGS.zep_sync_concurrency)

            async def delete(uuid: str) -> None:
                async with gate:
                    await self.client().graph.episode.delete(uuid)

            async def add(feature_id: str) -> None:
                feature = features[feature_id]
                async with gate:
                    await self.client().graph.add(
                        graph_id=graph_id,
                        type="json",
                        data=json.dumps(feature["payload"]),
                        source_description=f"{product} feature catalogue {generated}",
                        metadata={
                            "feature_id": feature_id,
                            "content_hash": feature["hash"],
                            "generated": generated,
                        },
                    )

            # Deletes first, so a replaced feature never has two live versions
            # longer than it must.
            await asyncio.gather(*(delete(u) for u in plan["delete"]))
            await asyncio.gather(*(add(f) for f in plan["add"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("feature sync to Zep failed")
            return None

        summary = {
            "added": len(plan["add"]),
            "deleted": len(plan["delete"]),
            "unchanged": plan["kept"],
            "kept_newer": plan["stale"],
        }
        log.info(
            "Zep feature catalogue (%s, file dated %s): %s",
            graph_id,
            generated or "undated",
            ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in summary.items()),
        )
        return summary

    async def _ensure_graph(self, graph_id: str) -> None:
        from zep_cloud.errors import NotFoundError

        try:
            await self.client().graph.get(graph_id)
        except NotFoundError:
            await self.client().graph.create(
                graph_id=graph_id,
                name="Salesmsg feature catalogue",
                description="Product features, synced from data/features.json at startup.",
            )
            log.info("created Zep graph %s", graph_id)

    async def _feature_episodes(self, graph_id: str, expected: int) -> list[dict[str, Any]]:
        # The graph holds only catalogue episodes, so a generous lastn returns
        # all of them in one request. Flag it if we may have hit the ceiling:
        # unseen episodes would never be replaced.
        lastn = max(1000, expected * 4)
        response = await self.client().graph.episode.get_by_graph_id(graph_id, lastn=lastn)
        episodes = response.episodes or []
        if len(episodes) >= lastn:
            log.warning("Zep returned %d episodes, the request ceiling; some may be unseen", lastn)
        out = []
        for episode in episodes:
            meta = episode.metadata or {}
            out.append(
                {
                    "uuid": episode.uuid_,
                    "feature_id": meta.get("feature_id"),
                    "hash": meta.get("content_hash"),
                    "generated": meta.get("generated"),
                }
            )
        return out

    # --- callers -----------------------------------------------------------

    async def ensure_user(self, phone: str, **profile: str | None) -> str | None:
        """Create the caller's Zep user if new. Returns the user id, or None."""
        if not self.configured:
            return None
        from zep_cloud.errors import NotFoundError

        user_id = user_id_for(phone)
        if user_id in self._known_users:
            return user_id
        fields = {k: v for k, v in profile.items() if v}
        try:
            try:
                await self.client().user.get(user_id)
            except NotFoundError:
                await self.client().user.add(
                    user_id=user_id, metadata={"phone": phone}, **fields
                )
                log.info("new Zep user %s", user_id)
            self._known_users.add(user_id)
            return user_id
        except Exception as exc:
            log.warning("Zep user %s unavailable: %s", user_id, exc)
            return None

    async def update_profile(self, user_id: str, **profile: str | None) -> None:
        fields = {k: v for k, v in profile.items() if v}
        if not (self.configured and fields):
            return
        try:
            await self.client().user.update(user_id, **fields)
        except Exception as exc:
            log.warning("could not update Zep user %s: %s", user_id, exc)

    async def previous_context(self, user_id: str, exclude: str | None = None) -> str:
        """What Zep knows about a returning caller, from their latest thread.

        A brand-new thread has no messages for Zep to key off, so the most
        recent earlier call's context is the best opening briefing there is.
        Returns "" for a first-time caller.
        """
        if not self.configured:
            return ""
        try:
            threads = await asyncio.wait_for(
                self.client().user.get_threads(user_id), SETTINGS.zep_timeout
            )
            threads = [t for t in threads or [] if t.thread_id != exclude]
            if not threads:
                return ""
            latest = max(threads, key=lambda t: t.created_at or "")
            context = await asyncio.wait_for(
                self.client().thread.get_user_context(latest.thread_id),
                SETTINGS.zep_timeout,
            )
            return (context.context or "").strip()
        except Exception as exc:
            log.warning("Zep context for %s unavailable: %s", user_id, exc)
            return ""

    async def start_thread(self, user_id: str, call_sid: str) -> str | None:
        if not self.configured:
            return None
        thread_id = thread_id_for(call_sid)
        try:
            await self.client().thread.create(thread_id=thread_id, user_id=user_id)
            return thread_id
        except Exception as exc:
            log.warning("could not open Zep thread %s: %s", thread_id, exc)
            return None

    async def add_messages(self, thread_id: str, messages: list[dict[str, Any]]) -> None:
        """Append turns: [{"role", "name", "content", "metadata"?}]."""
        if not (self.configured and messages):
            return
        from zep_cloud.types import Message

        prepared = []
        for message in messages:
            content = (message.get("content") or "").strip()
            # Long turns are split, not cut: losing the end of what someone
            # said loses whatever they were getting to.
            for start in range(0, len(content), MAX_MESSAGE_CHARS):
                prepared.append(
                    Message(
                        role=message["role"],
                        name=message.get("name"),
                        content=content[start : start + MAX_MESSAGE_CHARS],
                        metadata=message.get("metadata"),
                    )
                )
        try:
            for start in range(0, len(prepared), MAX_MESSAGES_PER_CALL):
                await self.client().thread.add_messages(
                    thread_id, messages=prepared[start : start + MAX_MESSAGES_PER_CALL]
                )
        except Exception as exc:
            log.warning("could not store turns in Zep thread %s: %s", thread_id, exc)

    # --- recall --------------------------------------------------------------

    async def search(self, query: str, user_id: str | None) -> dict[str, list[str]]:
        """Facts about this caller (if `user_id`) and the catalogue, in parallel.

        Pass user_id=None to search the catalogue only — that is how memory
        stays locked before the caller has keyed their PIN.
        """
        empty: dict[str, list[str]] = {"caller": [], "catalogue": []}
        if not (self.configured and query.strip()):
            return empty

        async def facts(**scope: str) -> list[str]:
            results = await asyncio.wait_for(
                self.client().graph.search(
                    query=query, scope="edges", limit=SETTINGS.zep_search_limit, **scope
                ),
                SETTINGS.zep_timeout,
            )
            lines = []
            for edge in results.edges or []:
                if edge.invalid_at:  # superseded; the newer fact will be here too
                    continue
                when = (edge.valid_at or edge.created_at or "")[:10]
                lines.append(f"{edge.fact}" + (f" (as of {when})" if when else ""))
            return lines

        lookups = {"catalogue": facts(graph_id=SETTINGS.zep_features_graph_id)}
        if user_id:
            lookups["caller"] = facts(user_id=user_id)
        outcomes = await asyncio.gather(*lookups.values(), return_exceptions=True)

        found = dict(empty)
        for key, outcome in zip(lookups, outcomes):
            if isinstance(outcome, BaseException):
                log.warning("Zep %s search failed: %s", key, outcome)
            else:
                found[key] = outcome
        return found

    async def aclose(self) -> None:
        task, self.sync_task = self.sync_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass


ZEP = ZepMemory()


def format_recall(found: dict[str, list[str]], caller_locked: bool) -> str:
    """Render recall results for the model, honest about what was searched."""
    parts = []
    if found.get("caller"):
        parts.append(
            "From earlier calls with this caller (a snapshot as of the date shown "
            "— for current figures, use the account tools):\n- "
            + "\n- ".join(found["caller"])
        )
    if found.get("catalogue"):
        parts.append("From the product catalogue:\n- " + "\n- ".join(found["catalogue"]))
    if not parts:
        note = (
            " Earlier calls were not searched: the caller has not entered their PIN yet."
            if caller_locked
            else ""
        )
        return "Nothing in memory covers that." + note + " Move on to the next way of finding out."
    if caller_locked:
        parts.append("(Earlier calls were not searched: the caller has not entered their PIN yet.)")
    return "\n\n".join(parts)
