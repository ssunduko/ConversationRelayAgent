"""The dialpad: a page to enter a number, and the endpoint that dials it.

An outbound call gets `mode=demo` as a ConversationRelay <Parameter>, which is
what tells the session to run the demo-booking persona instead of the
PIN-gated answering service.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from api.dependencies import public_host, verify_twilio_signature
from core.config import SETTINGS

log = logging.getLogger("relay.dialpad")

router = APIRouter(tags=["dialpad"])

DIALPAD_PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "dialpad.html"


class CallRequest(BaseModel):
    to: str


def to_e164(raw: str) -> str:
    """Accept what someone types on a keypad; produce what Twilio needs."""
    cleaned = re.sub(r"[^\d+]", "", raw.strip())
    if not cleaned:
        raise HTTPException(400, "No number given")
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif len(cleaned) == 10:  # bare US number
        digits = "1" + cleaned
    elif len(cleaned) == 11 and cleaned.startswith("1"):
        digits = cleaned
    else:
        digits = cleaned

    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise HTTPException(400, f"{raw!r} does not look like a phone number")
    return "+" + digits


@router.get("/")
async def dialpad() -> FileResponse:
    return FileResponse(DIALPAD_PAGE, media_type="text/html")


@router.post("/calls")
async def place_call(request: Request, body: CallRequest) -> dict[str, str]:
    if not SETTINGS.outbound_enabled:
        raise HTTPException(403, "Outbound calling is disabled (OUTBOUND_ENABLED)")
    if not SETTINGS.outbound_caller_id:
        raise HTTPException(500, "OUTBOUND_CALLER_ID is not set")

    account_sid = SETTINGS.twilio_account_sid
    if not (account_sid and SETTINGS.twilio_auth_token):
        raise HTTPException(500, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set")

    to = to_e164(body.to)
    if to == SETTINGS.outbound_caller_id:
        raise HTTPException(400, "That is the number you are calling from")

    try:
        from twilio.base.exceptions import TwilioException
        from twilio.rest import Client
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise HTTPException(500, "outbound calling needs the `twilio` package") from exc

    host = public_host(request)
    options: dict[str, object] = {
        "to": to,
        "from_": SETTINGS.outbound_caller_id,
        "url": f"https://{host}/outbound-twiml",
        "method": "POST",
        "status_callback": f"https://{host}/call-status",
        # Every transition, not just completed — the dialpad shows progress.
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
        "status_callback_method": "POST",
        "timeout": SETTINGS.outbound_ring_seconds,
    }
    if SETTINGS.detect_answering_machine:
        # Asynchronous detection: Twilio connects the call immediately and
        # reports who answered afterwards. Synchronous detection makes it
        # listen *before* fetching the TwiML, so the person picks up to several
        # seconds of silence and often says "hello?" twice before the greeting
        # begins — or hangs up first.
        options["machine_detection"] = "Enable"
        options["machine_detection_timeout"] = 5
        options["async_amd"] = "true"
        options["async_amd_status_callback"] = f"https://{host}/amd-status"
        options["async_amd_status_callback_method"] = "POST"

    try:
        call = Client(account_sid, SETTINGS.twilio_auth_token).calls.create(**options)
    except TwilioException as exc:
        log.error("outbound call to %s failed: %s", to, exc)
        raise HTTPException(502, f"Twilio rejected the call: {exc}") from exc

    log.info("outbound demo call %s -> %s", call.sid, to)
    return {"call_sid": call.sid, "to": to, "from": SETTINGS.outbound_caller_id}


@router.get("/calls/{call_sid}")
async def call_state(call_sid: str) -> dict[str, object]:
    """Progress for the dialpad: queued, ringing, in-progress, completed."""
    if not (SETTINGS.twilio_account_sid and SETTINGS.twilio_auth_token):
        raise HTTPException(500, "Twilio credentials are not set")
    from twilio.base.exceptions import TwilioException
    from twilio.rest import Client

    try:
        call = Client(SETTINGS.twilio_account_sid, SETTINGS.twilio_auth_token).calls(call_sid).fetch()
    except TwilioException as exc:
        raise HTTPException(404, f"Unknown call: {exc}") from exc

    # CallInstance has no error_message field — reading it 500'd every poll,
    # which is why the dialpad showed no progress at all.
    return {
        "call_sid": call.sid,
        "status": call.status,
        "answered_by": getattr(call, "answered_by", None),
        "duration": call.duration,
        "error": None,
    }


@router.post("/calls/{call_sid}/hangup")
async def hang_up(call_sid: str) -> dict[str, str]:
    """End a call in progress. This is the dialpad's hang-up button."""
    if not (SETTINGS.twilio_account_sid and SETTINGS.twilio_auth_token):
        raise HTTPException(500, "Twilio credentials are not set")
    from twilio.base.exceptions import TwilioException
    from twilio.rest import Client

    try:
        call = (
            Client(SETTINGS.twilio_account_sid, SETTINGS.twilio_auth_token)
            .calls(call_sid)
            .update(status="completed")
        )
    except TwilioException as exc:
        log.error("could not hang up %s: %s", call_sid, exc)
        raise HTTPException(502, f"Twilio rejected the hang-up: {exc}") from exc

    log.info("hung up %s from the dialpad", call_sid)
    return {"call_sid": call.sid, "status": call.status}


@router.post("/outbound-twiml")
async def outbound_twiml(request: Request) -> Response:
    """TwiML for a call we placed. Same relay, demo persona."""
    body = await request.body()
    await verify_twilio_signature(request, body)

    form = await request.form()
    answered_by = str(form.get("AnsweredBy") or "")
    # With machine detection on, Twilio tells us who picked up. Talking to a
    # voicemail greeting wastes the call and leaves an AI message nobody asked
    # for, so hang up instead.
    if answered_by.startswith("machine") or answered_by == "fax":
        log.info(
            "call %s answered by %s — hanging up rather than leaving a message",
            form.get("CallSid", "unknown"),
            answered_by,
        )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup /></Response>',
            media_type="text/xml",
        )
    if answered_by:
        log.info("call %s answered by %s", form.get("CallSid", "unknown"), answered_by)

    host = public_host(request)
    attributes = [
        f"url={quoteattr(f'wss://{host}{SETTINGS.websocket_path}')}",
        f"welcomeGreeting={quoteattr(SETTINGS.demo_welcome_greeting)}",
        f"ttsProvider={quoteattr(SETTINGS.tts_provider)}",
        f"voice={quoteattr(SETTINGS.voice)}",
        'interruptible="any"',
        # A person who just picked up will say "hello" — that must not kill
        # the opening line, or they never hear who is calling.
        'welcomeGreetingInterruptible="none"',
        'reportInputDuringAgentSpeech="dtmf"',
        'ignoreBackchannel="true"',
        # Twilio's default sensitivity fires on noise and half-words, which on
        # a live demo call cancelled the agent mid-sentence repeatedly.
        f"interruptSensitivity={quoteattr(SETTINGS.demo_interrupt_sensitivity)}",
        'dtmfDetection="true"',
    ]
    if SETTINGS.intelligence_service:
        attributes.append(f"intelligenceService={quoteattr(SETTINGS.intelligence_service)}")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Connect action={quoteattr(f"https://{host}/handoff")} method="POST">'
        f"<ConversationRelay {' '.join(attributes)}>"
        # This is what puts the session into demo mode.
        '<Parameter name="mode" value="demo" />'
        "</ConversationRelay></Connect></Response>"
    )
    return Response(content=xml, media_type="text/xml")


@router.post("/amd-status")
async def amd_status(request: Request) -> Response:
    """Asynchronous answering-machine result.

    The call is already connected and the agent may be mid-greeting. If it
    turns out a machine answered, end the call rather than leave an AI
    voicemail.
    """
    await verify_twilio_signature(request, await request.body())
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    answered_by = str(form.get("AnsweredBy") or "")

    if not answered_by.startswith("machine") and answered_by != "fax":
        log.info("call %s answered by %s", call_sid, answered_by or "unknown")
        return Response(status_code=204)

    log.info("call %s answered by %s — ending it", call_sid, answered_by)
    if SETTINGS.twilio_account_sid and SETTINGS.twilio_auth_token and call_sid:
        from twilio.base.exceptions import TwilioException
        from twilio.rest import Client

        try:
            Client(SETTINGS.twilio_account_sid, SETTINGS.twilio_auth_token).calls(
                call_sid
            ).update(status="completed")
        except TwilioException as exc:
            log.error("could not end %s after machine detection: %s", call_sid, exc)
    return Response(status_code=204)


@router.post("/call-status")
async def call_status(request: Request) -> Response:
    """Twilio's progress callbacks: initiated, ringing, answered, completed."""
    await verify_twilio_signature(request, await request.body())
    form = await request.form()
    status = form.get("CallStatus", "?")

    detail = ""
    if status == "completed":
        detail = f" duration={form.get('CallDuration', '?')}s"
    if form.get("AnsweredBy"):
        detail += f" answered_by={form.get('AnsweredBy')}"
    # no-answer, busy and failed are the ones worth noticing: the phone never
    # rang audibly, or the carrier diverted the call.
    if status in ("no-answer", "busy", "failed", "canceled"):
        log.warning("call %s: %s%s", form.get("CallSid", "unknown"), status, detail)
    else:
        log.info("call %s: %s%s", form.get("CallSid", "unknown"), status, detail)
    return Response(status_code=204)
