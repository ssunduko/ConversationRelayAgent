"""Does fast mode make a call turn noticeably quicker?

Runs the same turns twice — standard speed and fast mode — in the request
shape a demo call actually uses. Fast mode is a research preview on the Claude
API only, priced at $10/$50 per MTok against $5/$25 standard, and draws on a
separate rate-limit pool.

    python scripts/bench_fast_mode.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import statistics
import time

import anthropic

from core.config import DEMO_SYSTEM_PROMPT, SETTINGS
from schemas import call_tools, demo_tools
from services.salesmsg_mcp import MCP

TURNS = [
    "Yeah, we're a twenty person mortgage team. Tell me what makes this different.",
    "Sounds interesting. How does the AI agent handle after-hours texts?",
    "Alright, Tuesday at two works for me.",
]

FAST_BETA = "fast-mode-2026-02-01"


async def turn(client: anthropic.AsyncAnthropic, tools: list[dict], text: str, fast: bool):
    kwargs = {
        "model": SETTINGS.model,
        "max_tokens": SETTINGS.max_tokens,
        "system": [
            {"type": "text", "text": DEMO_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Context: benchmark. The person is on +12145550000."},
        ],
        "messages": [{"role": "user", "content": text}],
        "tools": tools,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": SETTINGS.effort},
        "cache_control": {"type": "ephemeral"},
    }
    if fast:
        kwargs["speed"] = "fast"
        kwargs["betas"] = [FAST_BETA]

    started = time.monotonic()
    first = None
    try:
        async with client.beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                if (
                    event.type == "content_block_delta"
                    and event.delta.type == "text_delta"
                    and first is None
                ):
                    first = time.monotonic() - started
            message = await stream.get_final_message()
    except anthropic.APIStatusError as exc:
        return None, None, f"{exc.status_code}: {str(exc.message)[:70]}"

    return first, time.monotonic() - started, getattr(message.usage, "speed", None)


async def main() -> None:
    mcp = await MCP.tool_definitions()
    allowed = {n.strip() for n in SETTINGS.demo_account_tools.split(",") if n.strip()}
    tools = list(call_tools.TOOLS) + list(demo_tools.TOOLS) + [t for t in mcp if t["name"] in allowed]
    print(f"model {SETTINGS.model}, effort {SETTINGS.effort}, {len(tools)} tools\n")

    client = anthropic.AsyncAnthropic()
    await turn(client, tools, "warm up", fast=False)  # don't measure TLS setup

    results: dict[str, list[float]] = {"standard": [], "fast": []}
    for label, fast in (("standard", False), ("fast", True)):
        print(f"{label}:")
        for text in TURNS:
            first, total, note = await turn(client, tools, text, fast)
            if total is None:
                print(f"  REQUEST FAILED  {note}")
            elif first is None:
                # The model went straight to a tool call. Not a failure — just
                # a turn with no speech in it, so there is no latency to time.
                print(f"  {total:5.2f}s total, no speech (tool call first)")
            else:
                results[label].append(first)
                speed = f"  [usage.speed={note}]" if note else ""
                print(f"  {first:5.2f}s to first token, {total:5.2f}s total{speed}")
        print()

    if results["standard"] and results["fast"]:
        s = statistics.median(results["standard"])
        f = statistics.median(results["fast"])
        print(f"median first token: standard {s:.2f}s vs fast {f:.2f}s "
              f"({(s - f) / s * 100:+.0f}%)")
        print("fast mode costs $10/$50 per MTok against $5/$25 standard")
    await client.close()
    await MCP.aclose()


if __name__ == "__main__":
    asyncio.run(main())
