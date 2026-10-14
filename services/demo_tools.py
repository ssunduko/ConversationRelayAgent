"""Handlers for the outbound demo call: answer questions, capture the lead.

Everything written here lands in SQLite keyed by the caller's phone number, so
a second call to the same person starts from what is already known.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from db.store import STORE
from schemas.demo_tools import TOOLS
from services.knowledge import KNOWLEDGE, format_hits

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.call_session import CallSession

log = logging.getLogger("relay.demo")

ToolHandler = Callable[["CallSession", dict[str, Any]], Awaitable[str]]

__all__ = ["TOOLS", "HANDLERS", "execute", "handles"]


async def search_product_knowledge(session: "CallSession", args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "No query given."

    hits = await KNOWLEDGE.search(query)
    failure = KNOWLEDGE.last_error if not hits else None
    session.record_search(query, hits, error=failure)

    if failure:
        # An outage is not a knowledge gap; say so in the log so a run of these
        # is not mistaken for the index being thin.
        log.error("call %s: knowledge %r FAILED -> %s", session.call_sid, query, failure)
    else:
        log.info(
            "call %s: knowledge %r -> %s",
            session.call_sid,
            query,
            f"{len(hits)} hit(s), best {hits[0]['score']}" if hits else "nothing (deferred to demo)",
        )
    return format_hits(hits)


async def save_caller_details(session: "CallSession", args: dict[str, Any]) -> str:
    fields = {
        key: str(args.get(key, "")).strip()
        for key in ("first_name", "last_name", "email", "experience", "notes")
    }
    provided = {k: v for k, v in fields.items() if v}
    if not provided:
        return "Nothing to save yet."

    await STORE.upsert_caller(session.contact_phone, **provided)
    session.collected.update(provided)
    log.info("call %s: saved %s", session.call_sid, ", ".join(sorted(provided)))
    return "Saved: " + ", ".join(sorted(provided)) + "."


async def book_demo(session: "CallSession", args: dict[str, Any]) -> str:
    requested_time = str(args.get("requested_time", "")).strip()
    if not requested_time:
        return "No time given — ask the caller when suits them."

    known = session.collected
    appointment_id = await STORE.save_appointment(
        session.contact_phone,
        first_name=known.get("first_name"),
        last_name=known.get("last_name"),
        email=known.get("email"),
        requested_time=requested_time,
        notes=str(args.get("notes", "")).strip() or None,
        call_sid=session.call_sid,
    )
    session.booked = True
    log.info(
        "call %s: DEMO BOOKED #%d for %s at %r",
        session.call_sid,
        appointment_id,
        session.contact_phone,
        requested_time,
    )

    # Nudge rather than fail: a booking with no email is much less useful.
    missing = [f for f in ("first_name", "last_name", "email") if not known.get(f)]
    if missing:
        return (
            f"Booked for {requested_time}. Still missing "
            + ", ".join(m.replace("_", " ") for m in missing)
            + " — ask for those before you finish the call."
        )
    return f"Booked for {requested_time}. You have their name and email on file."


HANDLERS: dict[str, ToolHandler] = {
    "search_product_knowledge": search_product_knowledge,
    "save_caller_details": save_caller_details,
    "book_demo": book_demo,
}

_schema_names = {tool["name"] for tool in TOOLS}
if _schema_names != set(HANDLERS):
    raise RuntimeError(
        "demo tool schemas and handlers disagree: "
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
