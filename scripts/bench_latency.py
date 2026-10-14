"""Measure time-to-first-spoken-token across model/effort/speed settings.

On a phone line the only latency that matters is how long the caller sits in
silence before the agent starts talking — i.e. the first `text_delta`, not the
full response. Twilio adds its own STT and TTS time on top of whatever this
reports.

    python bench_latency.py
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import statistics
import time

import anthropic

from services import call_tools as tools
from core.config import SYSTEM_PROMPT

ROUNDS = 3

PROMPTS = [
    "Hi, what are your customer service hours?",
    "I need to change the delivery address on an order I placed yesterday.",
]

CONFIGS: list[dict] = [
    {"label": "opus-5    effort=low", "model": "claude-opus-5", "effort": "low"},
    {"label": "opus-5    effort=low  fast", "model": "claude-opus-5", "effort": "low", "fast": True},
    {"label": "opus-5    effort=medium", "model": "claude-opus-5", "effort": "medium"},
    {"label": "sonnet-5  effort=low", "model": "claude-sonnet-5", "effort": "low"},
]


async def first_token_latency(client: anthropic.AsyncAnthropic, cfg: dict, prompt: str) -> float | None:
    kwargs = {
        "model": cfg["model"],
        "max_tokens": 2048,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools.TOOLS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": cfg["effort"]},
    }

    started = time.monotonic()
    if cfg.get("fast"):
        kwargs["speed"] = "fast"
        kwargs["betas"] = ["fast-mode-2026-02-01"]
        stream_cm = client.beta.messages.stream(**kwargs)
    else:
        stream_cm = client.messages.stream(**kwargs)

    async with stream_cm as stream:
        async for event in stream:
            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                return time.monotonic() - started
    return None  # turn produced only a tool call


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    print(f"{ROUNDS} rounds x {len(PROMPTS)} prompts per config; "
          "time to first spoken token, in seconds\n")
    print(f"{'config':<30} {'median':>8} {'best':>8} {'worst':>8}")
    print("-" * 58)

    try:
        for cfg in CONFIGS:
            samples: list[float] = []
            error: str | None = None
            for _ in range(ROUNDS):
                for prompt in PROMPTS:
                    try:
                        value = await first_token_latency(client, cfg, prompt)
                    except anthropic.APIStatusError as exc:
                        error = f"{exc.status_code} {exc.message[:60]}"
                        break
                    if value is not None:
                        samples.append(value)
                if error:
                    break

            if error:
                print(f"{cfg['label']:<30}   unavailable — {error}")
            elif samples:
                print(f"{cfg['label']:<30} {statistics.median(samples):>7.2f}s "
                      f"{min(samples):>7.2f}s {max(samples):>7.2f}s")
            else:
                print(f"{cfg['label']:<30}   no text produced")
    finally:
        await client.close()

    print("\nBudget roughly 1-2s on top for Twilio's own speech-to-text and "
          "text-to-speech.\nUnder ~1.5s total feels conversational; over ~3s "
          "callers start saying 'hello?'.")


if __name__ == "__main__":
    asyncio.run(main())
