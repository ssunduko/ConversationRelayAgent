"""Handler for recall_memory: the answering service's long-term memory in Zep.

Memory about a caller's account is guarded exactly like the account tools.
Caller ID is easy to spoof, so until the PIN is entered this searches the
product catalogue only — never the caller's own history.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from schemas.memory_tools import TOOLS
from services.zep_memory import ZEP, format_recall

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.call_session import CallSession

log = logging.getLogger("relay.memory")

ToolHandler = Callable[["CallSession", dict[str, Any]], Awaitable[str]]

__all__ = ["TOOLS", "HANDLERS", "execute", "handles"]


async def recall_memory(session: "CallSession", args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "No query given."

    unlocked = session.verified and session.zep_user_id is not None
    found = await ZEP.search(query, user_id=session.zep_user_id if unlocked else None)
    log.info(
        "call %s: recall %r -> %d caller fact(s), %d catalogue fact(s)%s",
        session.call_sid,
        query,
        len(found["caller"]),
        len(found["catalogue"]),
        "" if unlocked else " (caller history locked)",
    )
    return format_recall(found, caller_locked=not session.verified)


HANDLERS: dict[str, ToolHandler] = {"recall_memory": recall_memory}

_schema_names = {tool["name"] for tool in TOOLS}
if _schema_names != set(HANDLERS):
    raise RuntimeError(
        "memory tool schemas and handlers disagree: "
        f"schemas only={_schema_names - set(HANDLERS)}, handlers only={set(HANDLERS) - _schema_names}"
    )


def handles(name: str) -> bool:
    return name in HANDLERS


async def execute(session: "CallSession", name: str, args: dict[str, Any]) -> tuple[str, bool]:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}", True
    try:
        return await handler(session, args), False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("call %s: %s failed", session.call_sid, name)
        return f"{name} failed: {exc}", True
