"""WebMCP fallback: tools the Salesmessage web app exposes to in-page agents.

app.salesmessage.com registers WebMCP tools (via the webmcp-react library)
inside the logged-in single-page app. They are reachable only from a browser
page, so this keeps one headless Chromium page open on the app and calls the
tools through `navigator.modelContextTesting` — the testing surface the app's
own polyfill installs, which needs no Chrome flags.

Why it is a fallback, not a peer of the MCP server:

  * Most of the web tools duplicate the Salesmsg MCP server (contacts, tags) or
    only move the on-screen UI (open_*, filters, the broadcast form). Any tool
    the MCP server already covers is dropped here, so the agent only ever sees
    the web version of something MCP cannot do.
  * Only an allowlist is offered (WEBMCP_TOOLS). reply_to_conversation is never
    on it: it pops an approval dialog no one is there to click.
  * The page is warmed at startup. A cold load is 4-10s, which cannot sit
    inside a phone turn.

Auth is a saved Playwright storage state (the app's token lives in
localStorage) — create it with `python scripts/webmcp_login.py`. The app also
only registers tools when the account has its in-app AI agent enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.config import SETTINGS

log = logging.getLogger("relay.webmcp")

PREFIX = "webmcp_"

# The app's polyfill installs navigator.modelContextTesting; the native browser
# API (document.modelContext) is the fallback, for when Chrome ships it.
LIST_JS = """async () => {
  const t = navigator.modelContextTesting;
  if (t?.listTools) return await t.listTools();
  if (document.modelContext?.getTools) {
    return (await document.modelContext.getTools()).map(x => ({
      name: x.name, description: x.description,
      inputSchema: typeof x.inputSchema === 'string' ? x.inputSchema : JSON.stringify(x.inputSchema)}));
  }
  return null;
}"""

CALL_JS = """async ({name, args}) => {
  const t = navigator.modelContextTesting;
  if (t?.executeTool) return await t.executeTool(name, JSON.stringify(args));
  const tool = (await document.modelContext.getTools()).find(x => x.name === name);
  if (!tool) throw new Error(`Tool ${name} not found`);
  return await document.modelContext.executeTool(tool, args);
}"""

READY_JS = "() => (navigator.modelContextTesting?.listTools?.() || []).length > 0"


def to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema) if schema.strip() else {}
        except ValueError:
            schema = {}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return {
        "name": PREFIX + tool["name"],
        "description": (
            "[Salesmessage web app] "
            + (tool.get("description") or tool["name"])
            + " Use this only when no salesmsg_ account tool can do it."
        ),
        "input_schema": schema,
    }


def select_tools(
    listed: list[dict[str, Any]], allowlist: set[str], mcp_names: set[str]
) -> list[dict[str, Any]]:
    """The web tools worth offering: allowlisted, and not already an MCP tool.

    MCP names carry a vendor prefix ("salesmsg_search_contacts"); web tool
    names don't ("search_contacts"), so compare on the bare name.
    """
    covered = {name.removeprefix("salesmsg_") for name in mcp_names}
    chosen = []
    for tool in listed:
        name = tool.get("name") or ""
        if name in allowlist and name not in covered:
            chosen.append(to_anthropic_tool(tool))
    return chosen


class WebMCP:
    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._listed: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self.start_task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return SETTINGS.webmcp_enabled

    @property
    def ready(self) -> bool:
        return self._page is not None and bool(self._listed)

    def handles(self, name: str) -> bool:
        return name.startswith(PREFIX)

    def start(self) -> None:
        """Warm the browser in the background so startup isn't held up by it."""
        if self.configured:
            self.start_task = asyncio.create_task(self._start())

    async def _start(self) -> None:
        async with self._lock:
            await self._open()

    async def _open(self) -> None:
        await self._teardown()
        state = SETTINGS.webmcp_storage_state
        if not state.exists():
            log.error(
                "WebMCP disabled: no login state at %s — run `python scripts/webmcp_login.py`",
                state,
            )
            return
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError:
            log.error("WebMCP disabled: `pip install playwright && playwright install chromium`")
            return

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            context = await self._browser.new_context(storage_state=str(state))
            # The agent needs the app's JavaScript, not its pictures.
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,mp3,mp4}",
                lambda route: route.abort(),
            )
            self._page = await context.new_page()
            await self._page.goto(SETTINGS.webmcp_url, wait_until="domcontentloaded")
            await self._page.wait_for_function(READY_JS, timeout=30_000)
            self._listed = await self._page.evaluate(LIST_JS) or []
        except Exception as exc:
            # Usually an expired login (the page never registers tools) or an
            # account without the in-app AI agent feature.
            log.error("WebMCP unavailable: %s", exc)
            await self._teardown()
            return
        log.info(
            "WebMCP ready on %s: %d tools (%s)",
            SETTINGS.webmcp_url,
            len(self._listed),
            ", ".join(t.get("name", "?") for t in self._listed),
        )

    async def tool_definitions(self, allowlist: set[str], mcp_names: set[str]) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        return select_tools(self._listed, allowlist, mcp_names)

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one web tool. Returns (text, is_error)."""
        bare = name.removeprefix(PREFIX)
        if not self.ready:
            return "The Salesmessage web app is not reachable right now.", True

        for attempt in (1, 2):
            try:
                raw = await asyncio.wait_for(
                    self._page.evaluate(CALL_JS, {"name": bare, "args": args}),
                    SETTINGS.webmcp_timeout,
                )
                break
            except asyncio.TimeoutError:
                return f"{bare} took too long to respond.", True
            except Exception as exc:
                if attempt == 2:
                    return f"{bare} failed: {exc}", True
                # A crashed page or lapsed session: reload once, then retry.
                log.warning("WebMCP %s failed (%s); reloading the page once", bare, exc)
                async with self._lock:
                    await self._open()
                if not self.ready:
                    return "The Salesmessage web app is not reachable right now.", True

        text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        return text or "(the tool returned no content)", False

    async def _teardown(self) -> None:
        self._page, self._listed = None, []
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        for closer in (browser and browser.close, playwright and playwright.stop):
            if closer:
                try:
                    await closer()
                except Exception as exc:
                    log.debug("ignoring error closing the browser: %s", exc)

    async def aclose(self) -> None:
        task, self.start_task = self.start_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        async with self._lock:
            await self._teardown()


WEBMCP = WebMCP()
