"""Shared request handling for the Twilio-facing endpoints."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from core.config import SETTINGS

log = logging.getLogger("relay.api")


def normalize_host(value: str) -> str:
    """Reduce whatever was configured to a bare host[:port].

    PUBLIC_HOSTNAME is documented as hostname-only, but pasting the full ngrok
    URL is the obvious thing to do — and 'wss://https://host/ws' fails in a way
    that only shows up once Twilio tries to dial it.
    """
    value = value.strip()
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", value)  # drop any scheme
    return value.split("/", 1)[0]  # drop any path


def public_host(request: Request) -> str:
    if SETTINGS.public_hostname:
        return normalize_host(SETTINGS.public_hostname)
    forwarded = request.headers.get("x-forwarded-host")
    # X-Forwarded-Host can carry a proxy chain: "public.example, internal:8000".
    if forwarded:
        return normalize_host(forwarded.split(",")[0])
    return normalize_host(request.url.netloc)


async def verify_twilio_signature(request: Request, body: bytes) -> None:
    if not SETTINGS.validate_signature:
        return
    if not SETTINGS.twilio_auth_token:
        raise HTTPException(500, "VALIDATE_TWILIO_SIGNATURE is on but TWILIO_AUTH_TOKEN is unset")

    try:
        from twilio.request_validator import RequestValidator
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise HTTPException(500, "signature validation needs the `twilio` package") from exc

    # Twilio signs the full URL it requested, query string included.
    url = f"https://{public_host(request)}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # keep_blank_values is essential: Twilio sends empty params on every voice
    # webhook (ForwardedFrom, CallerName, ParentCallSid) and signs all of them.
    # Dropping them changes the hash, so every genuine request 403s.
    params = dict(parse_qsl(body.decode(), keep_blank_values=True))
    signature = request.headers.get("x-twilio-signature", "")

    if not RequestValidator(SETTINGS.twilio_auth_token).validate(url, params, signature):
        # Log what we validated against — a mismatch is nearly always the URL
        # (wrong PUBLIC_HOSTNAME, or a proxy rewriting the host), not the token.
        log.warning(
            "Twilio signature rejected: validated against %s with %d params, signature %s",
            url,
            len(params),
            "present" if signature else "MISSING",
        )
        raise HTTPException(403, "invalid Twilio signature")
