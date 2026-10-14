"""The live call: the ConversationRelay WebSocket.

Twilio streams transcribed speech in as JSON messages; the session streams
Claude's reply back out as text tokens. This module only routes messages — the
conversation itself lives in services.call_session.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import SETTINGS
from services.call_session import CallSession

log = logging.getLogger("relay")

router = APIRouter()


@router.websocket(SETTINGS.websocket_path)
async def conversation_relay(websocket: WebSocket) -> None:
    await websocket.accept()
    session: CallSession | None = None
    caller_hung_up = False

    try:
        while True:
            message: dict[str, Any] = json.loads(await websocket.receive_text())
            kind = message.get("type")

            if kind == "setup":
                try:
                    session = CallSession(websocket, websocket.app.state.claude, message)
                except Exception:
                    # Close rather than sit mute: a caller hearing silence has
                    # no idea anything is wrong, and nor does the client.
                    log.exception("could not start a session for %s", message.get("callSid"))
                    await websocket.close(code=1011, reason="session setup failed")
                    return
                # Loads any memory of this caller before the first turn.
                await session.prepare()
                log.info(
                    "call %s connected: mode=%s contact=%s",
                    session.call_sid,
                    session.mode,
                    session.contact_phone,
                )
                continue

            if session is None:
                log.warning("ignoring %r received before setup", kind)
                continue

            if kind == "prompt":
                # Partial prompts arrive with last=false when partialPrompts is
                # enabled; only a finished utterance is a turn.
                if message.get("last"):
                    text = message.get("voicePrompt", "")
                    log.info("call %s caller: %s", session.call_sid, text)
                    await session.on_user_text(text)

            elif kind == "interrupt":
                heard = message.get("utteranceUntilInterrupt", "")
                log.info("call %s barge-in after: %r", session.call_sid, heard)
                await session.on_interrupt(heard)

            elif kind == "dtmf":
                # Before verification these digits are PIN entry, so the
                # session decides what a keypress means.
                await session.on_dtmf(message.get("digit", ""))

            elif kind == "error":
                log.error("call %s relay error: %s", session.call_sid, message.get("description"))

            else:
                log.debug("call %s unhandled message type %r", session.call_sid, kind)

    except WebSocketDisconnect:
        # Twilio closes the socket when the call ends — normally because the
        # caller hung up.
        caller_hung_up = True
        log.info("call %s: caller hung up", session.call_sid if session else "unknown")
    except Exception:
        log.exception("call %s failed", session.call_sid if session else "unknown")
    finally:
        if session is not None:
            await session.aclose(caller_hung_up=caller_hung_up)
