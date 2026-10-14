"""How long does one conversational turn take, and what drives it?

The demo agent carries 36 tool schemas (~12k tokens) on every request. This
measures whether that is what's causing the pauses between turns, by timing
the same turn with progressively fewer tools.

    python scripts/bench_turn.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import time

import anthropic

from core.config import DEMO_SYSTEM_PROMPT, SETTINGS
from schemas import call_tools, demo_tools
from services.salesmsg_mcp import MCP

# What a demo agent plausibly needs to act on the account during a call.
DEMO_ESSENTIALS = {
    "salesmsg_send_sms",
    "salesmsg_send_message",
    "salesmsg_schedule_message",
    "salesmsg_create_contact",
    "salesmsg_get_contact",
    "salesmsg_search_contacts",
    "salesmsg_get_all_inboxes",
}

TURN = "Yeah, we're a twenty person mortgage team. What does it cost?"


def kwargs_for(tools: list[dict]) -> dict:
    return {
        "model": SETTINGS.model,
        "max_tokens": SETTINGS.max_tokens,
        "system": [
            {"type": "text", "text": DEMO_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Context: benchmark. The person is on +12145550000."},
        ],
        "messages": [{"role": "user", "content": TURN}],
        "tools": tools,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": SETTINGS.effort},
        "cache_control": {"type": "ephemeral"},
    }


async def time_turn(client: anthropic.AsyncAnthropic, tools: list[dict]) -> tuple[float, float]:
    started = time.monotonic()
    first = None
    async with client.beta.messages.stream(**kwargs_for(tools)) as stream:
        async for event in stream:
            if event.type == "content_block_delta" and event.delta.type == "text_delta" and first is None:
                first = time.monotonic() - started
        await stream.get_final_message()
    return (first or 0.0), time.monotonic() - started


async def main() -> None:
    mcp = await MCP.tool_definitions()
    if not mcp:
        print("MCP unreachable — measuring without account tools only.\n")

    local = list(call_tools.TOOLS) + list(demo_tools.TOOLS)
    search_tool = {
        "type": SETTINGS.tool_search_type,
        "name": SETTINGS.tool_search_type.rsplit("_", 1)[0],
    }
    deferred = [search_tool] + [{**t, "defer_loading": True} for t in mcp]

    scenarios = [
        ("local tools only (5)", local),
        (f"local + essential account ({len(local) + len(DEMO_ESSENTIALS & {t['name'] for t in mcp})})",
         local + [t for t in mcp if t["name"] in DEMO_ESSENTIALS]),
        (f"everything ({len(local) + len(mcp)})", local + mcp),
        (f"all {len(mcp)}, tool search (deferred)", local + deferred),
    ]

    client = anthropic.AsyncAnthropic()
    print(f"model {SETTINGS.model}, effort {SETTINGS.effort}\n")
    print(f"{'scenario':<34} {'tokens':>8} {'1st token':>11} {'full turn':>11}")
    print("-" * 68)

    # Warm the connection so the first row isn't measuring TLS setup.
    await time_turn(client, local)

    for label, tools in scenarios:
        size = len(json.dumps(tools)) // 4
        first, total = await time_turn(client, tools)
        print(f"{label:<34} {size:>8,} {first:>10.2f}s {total:>10.2f}s")

    await client.close()
    await MCP.aclose()


if __name__ == "__main__":
    asyncio.run(main())
