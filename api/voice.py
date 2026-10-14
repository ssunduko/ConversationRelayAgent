"""Twilio voice webhooks: the TwiML that starts a call, and the handoff that
ends one.
"""

from __future__ import annotations

import json
import logging
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Request, Response

from api.dependencies import public_host, verify_twilio_signature
from core.config import SETTINGS

log = logging.getLogger("relay.api")

router = APIRouter(tags=["voice"])

HANGUP = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup /></Response>'

INTERRUPT_MODES = {"none", "dtmf", "speech", "any"}


def _interrupt_mode(value: str, field: str) -> str:
    """Normalize an interruption setting to none | dtmf | speech | any.

    Booleans are still accepted for backward compatibility with older configs
    (and older Twilio docs), where true meant "anything interrupts".
    """
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return "any"
    if lowered in {"false", "no", "0", "off"}:
        return "none"
    if lowered not in INTERRUPT_MODES:
        log.warning("%s=%r is not one of %s; using 'any'", field, value, sorted(INTERRUPT_MODES))
        return "any"
    return lowered


@router.post("/twiml")
async def twiml(request: Request) -> Response:
    """Point your Twilio number's 'A call comes in' webhook here."""
    await verify_twilio_signature(request, await request.body())

    host = public_host(request)
    ws_url = f"wss://{host}{SETTINGS.websocket_path}"
    attributes = [
        f"url={quoteattr(ws_url)}",
        f"welcomeGreeting={quoteattr(SETTINGS.welcome_greeting)}",
        f"ttsProvider={quoteattr(SETTINGS.tts_provider)}",
        f"voice={quoteattr(SETTINGS.voice)}",
        f"interruptible={quoteattr(_interrupt_mode(SETTINGS.interruptible, 'INTERRUPTIBLE'))}",
        # Without this the greeting is cut off by a "hello", by line noise, or
        # by the caller starting to key the PIN it just asked them for.
        "welcomeGreetingInterruptible="
        + quoteattr(
            _interrupt_mode(
                SETTINGS.welcome_greeting_interruptible, "WELCOME_GREETING_INTERRUPTIBLE"
            )
        ),
        # Twilio withholds keypad input while the agent speaks unless told not to.
        "reportInputDuringAgentSpeech="
        + quoteattr(
            _interrupt_mode(
                SETTINGS.report_input_during_agent_speech, "REPORT_INPUT_DURING_AGENT_SPEECH"
            )
        ),
        f"ignoreBackchannel={quoteattr('true' if SETTINGS.ignore_backchannel else 'false')}",
        f"dtmfDetection={quoteattr('true' if SETTINGS.dtmf_detection else 'false')}",
    ]
    if SETTINGS.interrupt_sensitivity:
        attributes.append(f"interruptSensitivity={quoteattr(SETTINGS.interrupt_sensitivity)}")
    if SETTINGS.intelligence_service:
        # Twilio writes a Transcript per call and runs the service's Language
        # Operators (sentiment, summary, escalation, ...) against it.
        attributes.append(f"intelligenceService={quoteattr(SETTINGS.intelligence_service)}")

    # Twilio POSTs the action URL when the ConversationRelay session ends,
    # carrying HandoffData. That's what turns `transfer_to_human` into an
    # actual transfer rather than just a hang-up.
    connect = f'<Connect action={quoteattr(f"https://{host}/handoff")} method="POST">'

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{connect}"
        f"<ConversationRelay {' '.join(attributes)} />"
        "</Connect></Response>"
    )
    return Response(content=xml, media_type="text/xml")


@router.post("/handoff")
async def handoff(request: Request) -> Response:
    """Twilio calls this when the ConversationRelay session ends.

    If the agent asked for a transfer, dial a human. Otherwise the caller is
    finished, so let the call end.
    """
    body = await request.body()
    await verify_twilio_signature(request, body)

    form = await request.form()
    raw = form.get("HandoffData") or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}

    reason_code = data.get("reasonCode", "")
    log.info(
        "handoff for call %s: status=%s reason=%s",
        form.get("CallSid", "unknown"),
        form.get("SessionStatus", "?"),
        reason_code or "(none)",
    )

    if reason_code != "live-agent-handoff" or not SETTINGS.escalation_number:
        if reason_code == "live-agent-handoff":
            log.error("transfer requested but ESCALATION_NUMBER is unset; hanging up")
        return Response(content=HANGUP, media_type="text/xml")

    # Present the number the caller originally dialled, unless overridden.
    caller_id = SETTINGS.escalation_caller_id or form.get("To") or ""
    dial_attrs = [f'timeout="{SETTINGS.escalation_timeout}"']
    if caller_id:
        dial_attrs.append(f"callerId={quoteattr(str(caller_id))}")

    log.info(
        "transferring call %s to %s",
        form.get("CallSid", "unknown"),
        SETTINGS.escalation_number,
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Dial {' '.join(dial_attrs)}>{escape(SETTINGS.escalation_number)}</Dial>"
        "</Response>"
    )
    return Response(content=xml, media_type="text/xml")
