"""Live end-to-end test: real ConversationRelay protocol, real Claude API.

Everything a phone call does except the phone — same WebSocket messages Twilio
would send, same app code, real Messages API requests. Costs a few cents.

The analytics leg only runs when SALESMSG_MCP_URL is configured; without it the
PIN gate and conversation are still exercised.

    python test_live.py
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import os
import re
import time

os.environ.setdefault("PUBLIC_HOSTNAME", "example.ngrok-free.app")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from core.config import SETTINGS  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

SETUP = {
    "type": "setup",
    "sessionId": "VXlive",
    "callSid": "CAlive",
    "from": "+18005550100",
    "to": "+18005550101",
    "callType": "PSTN",
    "direction": "inbound",
}

latencies: list[float] = []


def spoken_form(text: str) -> str:
    """Strip spacing and punctuation before matching.

    A voice agent spells things out — "U P S", "one zero four three two" — so
    naive substring checks miss.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def collect(ws, started: float) -> tuple[str, dict | None]:
    tokens: list[str] = []
    first_at: float | None = None
    while True:
        msg = ws.receive_json()
        if msg["type"] != "text":
            continue
        if msg.get("token"):
            if first_at is None:
                first_at = time.monotonic() - started
            tokens.append(msg["token"])
        if msg.get("last"):
            break

    reply = "".join(tokens)
    print(f"  agent  > {reply}")
    if first_at is not None:
        latencies.append(first_at)
        print(f"           [{first_at:.2f}s to first token, {len(tokens)} chunks]")
    return reply, None


def say(ws, text: str) -> str:
    print(f"\n  caller > {text}")
    started = time.monotonic()
    ws.send_json({"type": "prompt", "voicePrompt": text, "last": True})
    return collect(ws, started)[0]


def press(ws, digits: str) -> str:
    print(f"\n  caller > [keypad: {digits}]")
    started = time.monotonic()
    for digit in digits:
        ws.send_json({"type": "dtmf", "digit": digit})
    return collect(ws, started)[0]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def run() -> int:
    if not SETTINGS.access_pin:
        print("ACCESS_PIN is not set — set it in .env before running this.")
        return 1

    failures = 0
    with TestClient(main.app) as client:
        print("=" * 72)
        print("HTTP endpoints")
        print("=" * 72)
        health = client.get("/health").json()
        failures += not check("health responds", health["status"] == "ok", str(health))
        resp = client.post("/twiml", data={"CallSid": "CAlive"})
        if SETTINGS.validate_signature:
            # Unsigned requests are supposed to be rejected once this is on;
            # a 200 here would mean validation isn't actually working.
            failures += not check(
                "rejects the unsigned webhook (VALIDATE_TWILIO_SIGNATURE=true)",
                resp.status_code == 403,
                f"got {resp.status_code}",
            )
        else:
            failures += not check("TwiML opens a ConversationRelay", "<ConversationRelay" in resp.text)
            failures += not check("DTMF enabled for PIN entry", 'dtmfDetection="true"' in resp.text)

        print("\n" + "=" * 72)
        print("Live call — real Claude")
        print("=" * 72)

        with client.websocket_connect("/ws") as ws:
            ws.send_json(SETUP)

            # Locked: the analytics tools are absent from the request entirely,
            # so the model has nothing to reach for.
            locked = say(ws, "Hi, how many messages did we send last week?")
            failures += not check("agent replied", bool(locked.strip()))
            failures += not check(
                "asks for the PIN instead of guessing at numbers",
                "pin" in locked.lower(),
                repr(locked),
            )
            failures += not check(
                "quotes no figures while locked",
                not re.search(r"\d{3,}", locked),
                repr(locked),
            )

            unlocked = press(ws, SETTINGS.access_pin)
            failures += not check("acknowledges the PIN", bool(unlocked.strip()))

            if not SETTINGS.salesmsg_mcp_url:
                print("\n  SKIP  analytics leg — SALESMSG_MCP_URL is not set")
            else:
                reply = say(ws, "How many messages did we send last week?")
                failures += not check("answered with analytics", bool(reply.strip()))
                # A voice agent is asked to round and spell numbers out, so
                # "forty-five" is a correct answer and contains no digits.
                number_words = (
                    "zero one two three four five six seven eight nine ten eleven twelve "
                    "thirteen fourteen fifteen twenty thirty forty fifty sixty seventy "
                    "eighty ninety hundred thousand million percent"
                ).split()
                lowered = reply.lower()
                failures += not check(
                    "quotes a figure",
                    bool(re.search(r"\d", reply)) or any(w in lowered for w in number_words),
                    repr(reply),
                )
                failures += not check(
                    "no raw JSON or field names leaked into speech",
                    not any(tok in reply for tok in ("{", "}", "_count", "from_date")),
                    repr(reply),
                )

            bye = say(ws, "That's all I needed, thanks. Goodbye!")
            failures += not check("said goodbye", bool(bye.strip()))

        print("\n" + "=" * 72)
        print(f"Model {SETTINGS.model}, effort {SETTINGS.effort}")
        if latencies:
            print(f"Time to first spoken token: {', '.join(f'{t:.2f}s' for t in latencies)}")
            print(f"  worst {max(latencies):.2f}s   median {sorted(latencies)[len(latencies)//2]:.2f}s")
        print("=" * 72)

    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
