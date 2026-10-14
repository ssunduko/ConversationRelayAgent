"""Salesmessage answering service — Twilio ConversationRelay + Claude.

  POST /twiml    Twilio hits this when a call comes in. Returns TwiML that opens
                 a ConversationRelay session pointed at the WebSocket below.
  WS   /ws       The live call. Twilio streams transcribed speech in; we stream
                 Claude's reply back as text and Twilio speaks it.
  POST /handoff  Twilio hits this when the session ends, so a requested
                 transfer can dial a human.

Run it:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI

from api import dialpad, explore, health, relay, voice
from core.config import SETTINGS
from core.logging import ERROR_LOG, OUTPUT_LOG, setup_logging
from services.knowledge import KNOWLEDGE
from services.salesmsg_auth import TOKENS
from services.salesmsg_mcp import MCP
from services.webmcp import WEBMCP
from services.zep_memory import ZEP

setup_logging()
log = logging.getLogger("relay")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One client for the process; it pools connections across calls.
    app.state.claude = anthropic.AsyncAnthropic()
    log.info("ready — model=%s effort=%s", SETTINGS.model, SETTINGS.effort)
    log.info("logging to %s (warnings and errors also to %s)", OUTPUT_LOG, ERROR_LOG.name)

    # Fail closed and say so: a misconfigured deployment should be obviously
    # useless rather than quietly serving account data to anyone who calls.
    if not SETTINGS.access_pin:
        log.warning(
            "ACCESS_PIN is not set — the account tools stay locked for every "
            "caller. Set it in .env to enable them."
        )
    if not SETTINGS.salesmsg_mcp_url:
        log.warning("SALESMSG_MCP_URL is not set — no account tools will be offered.")
    else:
        log.info("Salesmsg MCP: %s — auth: %s", SETTINGS.salesmsg_mcp_url, TOKENS.describe())
        if not TOKENS.can_refresh and not SETTINGS.salesmsg_mcp_token:
            log.warning(
                "No Salesmsg credentials — run `python scripts/get_salesmsg_token.py`."
            )
        elif not TOKENS.can_refresh:
            log.warning(
                "Only a static SALESMSG_MCP_TOKEN is set; it will stop working when it "
                "expires. Set SALESMSG_CLIENT_ID and SALESMSG_REFRESH_TOKEN for auto-renewal."
            )
    if not SETTINGS.escalation_number:
        log.warning("ESCALATION_NUMBER is not set — transfers will hang up instead.")

    if KNOWLEDGE.ready:
        log.info("knowledge base: %d documents", KNOWLEDGE.size)
        if not SETTINGS.voyage_api_key:
            log.warning("VOYAGE_API_KEY is unset — the index is loaded but unsearchable.")
    else:
        log.warning(
            "No knowledge index — demo calls cannot answer product questions. "
            "Run `python scripts/build_index.py`."
        )
    if SETTINGS.outbound_enabled:
        if SETTINGS.outbound_caller_id:
            log.info("outbound demo calls from %s", SETTINGS.outbound_caller_id)
        else:
            log.warning("OUTBOUND_CALLER_ID is not set — the dialpad cannot place calls.")

    # Answering-service memory. Both start in the background: the feature sync
    # takes minutes and the browser several seconds, and neither should hold
    # up the first call.
    if ZEP.configured:
        log.info("Zep memory on for the answering service")
        ZEP.start_feature_sync()
    else:
        log.warning("ZEP_API_KEY is not set — the answering service has no long-term memory.")
    if WEBMCP.configured:
        WEBMCP.start()
    if SETTINGS.web_search_enabled:
        log.info("web search fallback on (%s)", SETTINGS.web_search_tool_type)

    try:
        yield
    finally:
        await app.state.claude.close()
        await MCP.aclose()
        await TOKENS.aclose()
        await ZEP.aclose()
        await WEBMCP.aclose()


app = FastAPI(title="Salesmessage Answering Service", lifespan=lifespan)

app.include_router(health.router)
app.include_router(explore.router)
app.include_router(dialpad.router)
app.include_router(voice.router)
app.include_router(relay.router)
