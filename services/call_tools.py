"""Handlers for the call-control tools: hanging up and handing off.

Everything the caller can do to their Salesmessage account arrives from the MCP
server (see services.salesmsg_mcp). What lives here is only what the phone call
itself owns.

Handlers are async and receive the live `CallSession`, so a tool can both
return text for Claude and act on the call.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from schemas.call_tools import TOOLS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.call_session import CallSession

ToolHandler = Callable[["CallSession", dict[str, Any]], Awaitable[str]]

__all__ = ["TOOLS", "HANDLERS", "execute"]


async def transfer_to_human(session: "CallSession", args: dict[str, Any]) -> str:
    reason = str(args.get("reason", "Caller asked for a human agent."))
    # The /handoff endpoint turns this reason code into an actual <Dial>.
    await session.end_call(
        handoff_data={"reasonCode": "live-agent-handoff", "reason": reason}
    )
    return "The call is being transferred now."


async def end_call(session: "CallSession", args: dict[str, Any]) -> str:
    reason = str(args.get("reason", "Caller was finished."))
    await session.end_call(handoff_data={"reasonCode": "completed", "reason": reason})
    return "The call has ended."


HANDLERS: dict[str, ToolHandler] = {
    "transfer_to_human": transfer_to_human,
    "end_call": end_call,
}

# The schemas live in schemas/ and the handlers here, joined only by name. A
# mismatch would surface as the model calling a tool that does nothing, so
# catch it at import instead.
_schema_names = {tool["name"] for tool in TOOLS}
if _schema_names != set(HANDLERS):
    raise RuntimeError(
        "call tool schemas and handlers disagree: "
        f"schemas only={_schema_names - set(HANDLERS)}, handlers only={set(HANDLERS) - _schema_names}"
    )


async def execute(session: "CallSession", name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool. Returns (result_text, is_error)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}", True
    try:
        return await handler(session, args), False
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # surface the failure to Claude so it can recover
        return f"{name} failed: {exc}", True
