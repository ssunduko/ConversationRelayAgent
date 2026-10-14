"""Where does the latency go when the MCP server is attached?

The PIN turn took 27s despite calling no tools, so the cost is not the analytics
query. This isolates the suspects: attaching `mcp_servers` makes Anthropic open
an MCP session and list the server's tools before generating a single token.

    python bench_mcp.py
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import time

import anthropic

from services import call_tools as tools
from core.config import SETTINGS, SYSTEM_PROMPT
from services.salesmsg_auth import TOKENS

PROMPT = "Say the single word: ready."


def base_kwargs() -> dict:
    return {
        "model": SETTINGS.model,
        "max_tokens": 512,
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Context: benchmark run."},
        ],
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": list(tools.TOOLS),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": SETTINGS.effort},
    }


def with_mcp(kwargs: dict, token: str, enabled: bool = True) -> dict:
    kwargs = {**kwargs, "tools": list(kwargs["tools"])}
    kwargs["mcp_servers"] = [
        {
            "type": "url",
            "name": "salesmsg",
            "url": SETTINGS.salesmsg_mcp_url,
            "authorization_token": token,
        }
    ]
    toolset: dict = {"type": "mcp_toolset", "mcp_server_name": "salesmsg"}
    if not enabled:
        # Allowlist shape: connect to the server but expose nothing.
        toolset["default_config"] = {"enabled": False}
    kwargs["tools"].append(toolset)
    kwargs["betas"] = ["mcp-client-2025-11-20"]
    return kwargs


async def timed(client: anthropic.AsyncAnthropic, kwargs: dict) -> tuple[float, str]:
    started = time.monotonic()
    try:
        async with client.beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    return time.monotonic() - started, "ok"
            return time.monotonic() - started, "no text"
    except anthropic.APIStatusError as exc:
        return time.monotonic() - started, f"{exc.status_code}: {str(exc.message)[:70]}"


async def main() -> None:
    token = await TOKENS.access_token()
    if not token:
        raise SystemExit("No Salesmsg token — run get_salesmsg_token.py first.")

    client = anthropic.AsyncAnthropic()
    print(f"model {SETTINGS.model}, effort {SETTINGS.effort}\n")
    print(f"{'scenario':<42} {'first token':>12}   note")
    print("-" * 78)

    async def run(label: str, kwargs: dict) -> float:
        seconds, note = await timed(client, kwargs)
        print(f"{label:<42} {seconds:>11.2f}s   {note}")
        return seconds

    # Warm the HTTP connection and the cached system prefix first, so the
    # numbers below aren't measuring process start-up.
    await run("warm-up (no MCP)", base_kwargs())

    plain = await run("no MCP, warm", base_kwargs())
    cold = await run("MCP attached, first request", with_mcp(base_kwargs(), token))
    warm = await run("MCP attached, second request", with_mcp(base_kwargs(), token))
    third = await run("MCP attached, third request", with_mcp(base_kwargs(), token))

    print("\nDoes attaching the server with every tool disabled still cost the connect?")
    disabled = await run("MCP attached but toolset disabled", with_mcp(base_kwargs(), token, enabled=False))

    print("\n" + "-" * 78)
    print(f"MCP attach overhead, cold : {cold - plain:+.2f}s vs no-MCP baseline")
    print(f"MCP attach overhead, warm : {min(warm, third) - plain:+.2f}s")
    if disabled > 0:
        print(f"Disabled-toolset overhead : {disabled - plain:+.2f}s")

    await client.close()
    await TOKENS.aclose()


if __name__ == "__main__":
    asyncio.run(main())
