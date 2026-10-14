"""Pre-flight check: exercise the public path Twilio will take, before dialling.

Finds your running ngrok tunnel, POSTs /twiml through the public URL exactly as
Twilio's webhook does, then opens the advertised wss:// endpoint and runs one
turn of conversation through it.

If this passes, everything except the telephony itself is verified.

    python preflight.py
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import re
import sys

import httpx

try:
    import websockets
except ModuleNotFoundError:
    sys.exit("Missing `websockets`. Run: pip install -r requirements.txt")

NGROK_API = "http://127.0.0.1:4040/api/tunnels"
# ngrok's free tier serves a browser interstitial unless this header is present.
# Twilio's own requests skip it, but ours look browser-ish.
HEADERS = {"ngrok-skip-browser-warning": "true", "User-Agent": "TwilioProxy/1.1"}


def ok(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if passed else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return passed


def public_url() -> str:
    try:
        tunnels = httpx.get(NGROK_API, timeout=5).json()["tunnels"]
    except Exception as exc:
        sys.exit(f"Can't reach ngrok's local API at {NGROK_API}: {exc}\nIs `ngrok http 8000` running?")
    https = [t for t in tunnels if t["public_url"].startswith("https://")]
    if not https:
        sys.exit("ngrok is running but has no https tunnel.")
    return https[0]["public_url"]


async def run() -> int:
    failures = 0
    base = public_url()
    print(f"Tunnel: {base}\n")

    print("1. Twilio's webhook -> /twiml")
    try:
        resp = httpx.post(f"{base}/twiml", data={"CallSid": "CApreflight"}, headers=HEADERS, timeout=15)
    except Exception as exc:
        sys.exit(f"  FAIL  request failed — {exc}")

    failures += not ok("responds 200", resp.status_code == 200, f"got {resp.status_code}")
    failures += not ok("returns XML", "<Response>" in resp.text, resp.text[:120])

    match = re.search(r'url="(wss://[^"]+)"', resp.text)
    if not match:
        print(f"  FAIL  no wss:// URL in TwiML\n\n{resp.text}")
        return 1
    ws_url = match.group(1)
    expected = f"wss://{base.replace('https://', '')}/ws"
    # Exact match, not a substring: "wss://https://host/ws" contains the host
    # but is not dialable.
    failures += not ok(
        "advertises the right WebSocket URL to Twilio",
        ws_url == expected,
        f"got {ws_url}, expected {expected}",
    )
    if ws_url != expected:
        print("        (check PUBLIC_HOSTNAME in .env — it takes a bare host, or leave it unset)")
    print(f"\n{resp.text}\n")

    print(f"2. The call itself -> {ws_url}")
    try:
        async with websockets.connect(ws_url, additional_headers=HEADERS, open_timeout=20) as ws:
            failures += not ok("WebSocket accepted", True)

            await ws.send(json.dumps({
                "type": "setup",
                "sessionId": "VXpreflight",
                "callSid": "CApreflight",
                "from": "+18005550100",
                "to": "+18005550101",
                "callType": "PSTN",
                "direction": "inbound",
            }))
            await ws.send(json.dumps({
                "type": "prompt",
                "voicePrompt": "Hi, can you tell me what you can help with?",
                "last": True,
            }))

            tokens: list[str] = []
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if msg.get("type") != "text":
                    continue
                if msg.get("token"):
                    tokens.append(msg["token"])
                if msg.get("last"):
                    break

            reply = "".join(tokens)
            failures += not ok("agent replied over the tunnel", bool(reply.strip()))
            failures += not ok("streamed in chunks", len(tokens) > 1, f"{len(tokens)} chunks")
            print(f"\n  agent > {reply}\n")

    except asyncio.TimeoutError:
        print("  FAIL  timed out waiting for the agent")
        failures += 1
    except Exception as exc:
        print(f"  FAIL  WebSocket failed — {type(exc).__name__}: {exc}")
        failures += 1

    if failures:
        print(f"{failures} check(s) failed — fix these before dialling.")
        return 1

    print("Public path verified. Point your Twilio number at:")
    print(f"  {base}/twiml   (HTTP POST)")
    print(f"\n  python setup_twilio.py --host {base.replace('https://', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
