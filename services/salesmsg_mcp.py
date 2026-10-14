"""A persistent MCP session to the Salesmsg server.

Why not Anthropic's MCP connector: it opens a session and lists the server's
tools on *every* request, which measured at roughly +4s to first spoken token —
paid even on turns that never touch analytics. Holding one session here costs
that handshake once per process instead, and a tool call itself takes ~0.3s.

The session is shared across calls and reconnects on demand: if the access
token rotates, or the connection drops, the next use rebuilds it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from core.config import SETTINGS
from services.salesmsg_auth import TOKENS

log = logging.getLogger("relay.mcp")

# Broad areas the agent names at the start of a call. Derived from the tool
# names the server actually advertises, so this stays true as the server
# changes rather than drifting out of date in the prompt.
#
# Order matters: the first match wins, so more specific patterns come first —
# "get_contacts_analytics" is reporting, "add_tag_to_contact" is tagging, and
# "get_contact_conversation" is conversations.
SERVICE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("reporting and analytics", ("analytics", "credit", "performance")),
    ("conversations", ("conversation",)),
    ("tags", ("tag",)),
    ("messages", ("message", "sms")),
    ("contacts", ("contact",)),
    ("inboxes and team", ("inbox", "team", "member")),
)

VENDOR_PREFIX = "salesmsg_"


def group_for(tool_name: str) -> str | None:
    """Bucket one tool name into a caller-facing area, or None.

    Matching is on whole underscore-separated words, after dropping the vendor
    prefix. Naive substring matching is wrong here in a way that is easy to
    miss: "salesmsg" contains "sms", so every single tool looked like a
    messaging tool.
    """
    stem = tool_name.lower()
    if stem.startswith(VENDOR_PREFIX):
        stem = stem[len(VENDOR_PREFIX):]
    words = stem.split("_")

    for label, keywords in SERVICE_GROUPS:
        # startswith absorbs plurals: "contacts" matches "contact".
        if any(word.startswith(keyword) for word in words for keyword in keywords):
            return label
    return None


def to_anthropic_tool(tool: Any) -> dict[str, Any]:
    """Convert one MCP tool definition into an Anthropic tool schema.

    Field naming has changed across MCP SDK versions — `input_schema` today,
    `inputSchema` historically — and getting it wrong produces an agent that
    silently has no analytics tools, so accept either.
    """
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": schema or {"type": "object", "properties": {}},
    }


class SalesmsgMCP:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._token: str | None = None
        self._definitions: list[dict[str, Any]] = []
        self._groups: list[str] = []
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(SETTINGS.salesmsg_mcp_url)

    # --- connection -----------------------------------------------------

    async def _connect(self, token: str) -> None:
        await self._teardown()

        url = SETTINGS.salesmsg_mcp_url.rstrip("/") + "/"
        stack = AsyncExitStack()
        try:
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
            streams = await stack.enter_async_context(streamable_http_client(url, http_client=http))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            listed = await session.list_tools()
        except Exception:
            await stack.aclose()
            raise

        self._stack, self._session, self._token = stack, session, token

        self._definitions = [to_anthropic_tool(tool) for tool in listed.tools]

        # Preserve the declared order so the agent names them predictably.
        present = {group_for(tool.name) for tool in listed.tools}
        self._groups = [label for label, _ in SERVICE_GROUPS if label in present]

        log.info(
            "Salesmsg MCP connected: %d tools across %s",
            len(self._definitions),
            ", ".join(self._groups) or "no recognised groups",
        )
        unclassified = [t.name for t in listed.tools if group_for(t.name) is None]
        if unclassified:
            # Still offered to the model; just not announced to the caller.
            log.info("tools outside the named groups: %s", ", ".join(unclassified))

    async def _teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._definitions = []
        self._groups = []
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                log.debug("ignoring error closing the MCP session: %s", exc)

    async def _ensure(self) -> ClientSession | None:
        """A live session, reconnecting if the token rotated or it dropped."""
        if not self.configured:
            return None

        token = await TOKENS.access_token()
        if token is None:
            return None

        async with self._lock:
            if self._session is not None and token == self._token:
                return self._session
            try:
                await self._connect(token)
            except Exception as exc:
                log.error("could not reach the Salesmsg MCP server: %s", exc)
                await self._teardown()
                return None
            return self._session

    # --- use ------------------------------------------------------------

    async def tool_definitions(self) -> list[dict[str, Any]]:
        """Anthropic tool schemas for every tool the server offers, or []."""
        session = await self._ensure()
        return list(self._definitions) if session is not None else []

    async def service_groups(self) -> list[str]:
        """Broad areas to announce, e.g. ['contacts', 'messages', ...]."""
        session = await self._ensure()
        return list(self._groups) if session is not None else []

    def handles(self, name: str) -> bool:
        """Anything the Salesmsg server owns; local tools use other names."""
        return name.startswith("salesmsg_")

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one analytics tool. Returns (text, is_error)."""
        session = await self._ensure()
        if session is None:
            return "The analytics service is unreachable right now.", True

        try:
            result = await session.call_tool(name, args)
        except Exception as exc:
            # One retry on a fresh session: a dropped connection shows up here.
            log.warning("%s failed (%s); reconnecting once", name, exc)
            await self._teardown()
            session = await self._ensure()
            if session is None:
                return "The analytics service is unreachable right now.", True
            try:
                result = await session.call_tool(name, args)
            except Exception as retry_exc:
                log.error("%s failed after reconnect: %s", name, retry_exc)
                return f"{name} failed: {retry_exc}", True

        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        failed = getattr(result, "is_error", None)
        if failed is None:
            failed = getattr(result, "isError", False)
        return (text or "(the tool returned no content)"), bool(failed)

    async def aclose(self) -> None:
        async with self._lock:
            await self._teardown()


MCP = SalesmsgMCP()
