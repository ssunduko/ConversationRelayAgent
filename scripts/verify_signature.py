"""Send a correctly-signed Twilio webhook through the public tunnel.

Reproduces exactly what Twilio does on an inbound call: the same form fields
(including the empty ones), signed with your TWILIO_AUTH_TOKEN using Twilio's
own RequestValidator. If this gets a 200, real calls will too.

    python verify_signature.py
    python verify_signature.py --url https://host/twiml
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import sys

import httpx

from core.config import SETTINGS

try:
    from twilio.request_validator import RequestValidator
except ModuleNotFoundError:
    sys.exit("Needs the `twilio` package: pip install -r requirements.txt")

# The shape Twilio actually POSTs for an inbound voice call. Several fields
# are empty, and Twilio signs them all.
PAYLOAD = {
    "AccountSid": "AC00000000000000000000000000000000",
    "ApiVersion": "2010-04-01",
    "CallSid": "CA00000000000000000000000000000000",
    "CallStatus": "ringing",
    "CallToken": "",
    "Called": "+15555550111",
    "CalledCity": "",
    "CalledCountry": "US",
    "CalledState": "OH",
    "CalledZip": "",
    "Caller": "+15555550133",
    "CallerCity": "",
    "CallerCountry": "US",
    "CallerName": "",
    "CallerState": "CA",
    "CallerZip": "",
    "Direction": "inbound",
    "ForwardedFrom": "",
    "From": "+15555550133",
    "FromCity": "",
    "FromCountry": "US",
    "FromState": "CA",
    "FromZip": "",
    "ParentCallSid": "",
    "StirVerstat": "",
    "To": "+15555550111",
    "ToCity": "",
    "ToCountry": "US",
    "ToState": "OH",
    "ToZip": "",
}


def tunnel_url() -> str | None:
    try:
        tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=5).json()["tunnels"]
    except Exception:
        return None
    https = [t for t in tunnels if t["public_url"].startswith("https://")]
    return f"{https[0]['public_url']}/twiml" if https else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Full webhook URL; defaults to the running ngrok tunnel")
    args = parser.parse_args()

    url = args.url or tunnel_url()
    if not url:
        sys.exit("No --url given and no ngrok tunnel found on :4040.")
    if not SETTINGS.twilio_auth_token:
        sys.exit("TWILIO_AUTH_TOKEN is not set in .env.")

    signature = RequestValidator(SETTINGS.twilio_auth_token).compute_signature(url, PAYLOAD)
    blanks = sum(1 for v in PAYLOAD.values() if v == "")
    print(f"POST {url}")
    print(f"  {len(PAYLOAD)} form fields, {blanks} of them empty")
    print(f"  signature computed with TWILIO_AUTH_TOKEN ({len(SETTINGS.twilio_auth_token)} chars)\n")

    resp = httpx.post(
        url,
        data=PAYLOAD,
        headers={
            "X-Twilio-Signature": signature,
            "User-Agent": "TwilioProxy/1.1",
            "ngrok-skip-browser-warning": "true",
        },
        timeout=30,
    )

    print(f"  HTTP {resp.status_code}")
    if resp.status_code == 200 and "<ConversationRelay" in resp.text:
        print("  PASS  signed webhook accepted; TwiML returned\n")
        print(resp.text)
        return 0

    if resp.status_code == 403:
        print("  FAIL  rejected as an invalid signature.")
        print("        Check the server log for the URL it validated against — a")
        print("        mismatch there (PUBLIC_HOSTNAME, or a proxy rewriting the")
        print("        host) is the usual cause, not the token.")
    else:
        print(f"  FAIL  unexpected response\n{resp.text[:400]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
