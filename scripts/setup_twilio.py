"""Point a Twilio phone number at this app's /twiml webhook.

Needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env.

    python setup_twilio.py                                  # list numbers + current webhooks
    python setup_twilio.py --host abc123.ngrok-free.app     # set it on your only number
    python setup_twilio.py --host abc123.ngrok-free.app --number +18005550100
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import os
import re
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioException
except ModuleNotFoundError:
    sys.exit("The `twilio` package is missing. Run: pip install -r requirements.txt")


def client_from_env() -> Client:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        sys.exit(
            "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env.\n"
            "Both are on the Twilio Console home page: https://console.twilio.com"
        )
    # Catch paste damage here rather than as an opaque 401 from the API.
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
        sys.exit(
            f"TWILIO_ACCOUNT_SID doesn't look right: {len(sid)} chars, starts {sid[:4]!r}.\n"
            "It should be exactly 34 characters: 'AC' followed by 32 hex digits.\n"
            "Copy it again from https://console.twilio.com (Account Info panel)."
        )
    if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        sys.exit(
            f"TWILIO_AUTH_TOKEN doesn't look right: {len(token)} chars.\n"
            "It should be exactly 32 hex digits."
        )
    return Client(sid, token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Public hostname, no scheme (e.g. abc123.ngrok-free.app)")
    parser.add_argument("--number", help="Which number to configure, E.164 (e.g. +18005550100)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Repoint a number whose webhook currently points somewhere other than --host. "
        "Required when the number looks like it is carrying production traffic.",
    )
    args = parser.parse_args()

    client = client_from_env()

    try:
        numbers = client.incoming_phone_numbers.list()
    except TwilioException as exc:
        sys.exit(
            f"Twilio rejected the request: {exc}\n"
            "A 401 here means the Account SID or auth token is wrong — re-copy "
            "both from https://console.twilio.com"
        )

    if not numbers:
        sys.exit(
            "No phone numbers on this account.\n"
            "Buy one (trial credit covers it): "
            "https://console.twilio.com/us1/develop/phone-numbers/manage/search\n"
            "It needs the Voice capability."
        )

    if not args.host:
        print("Numbers on this account:\n")
        for n in numbers:
            print(f"  {n.phone_number}  {n.friendly_name}")
            print(f"      voice webhook: {n.voice_url or '(none)'} [{n.voice_method}]")
        print("\nRe-run with --host <your-ngrok-host> to point one at this app.")
        return 0

    host = args.host.replace("https://", "").replace("http://", "").rstrip("/")
    target = f"https://{host}/twiml"

    if args.number:
        matches = [n for n in numbers if n.phone_number == args.number]
        if not matches:
            sys.exit(f"{args.number} is not on this account. Run without --host to list them.")
        chosen = matches[0]
    elif len(numbers) == 1:
        chosen = numbers[0]
    else:
        sys.exit("Several numbers on this account — pick one with --number +1...")

    current = chosen.voice_url or ""
    # An account can hold production numbers alongside the one you're testing
    # with. Repointing a live number silently breaks real calls, so a number
    # that isn't already aimed at --host needs an explicit --force.
    if current and host not in current and not args.force:
        sys.exit(
            f"{chosen.phone_number} currently points at:\n"
            f"    {current}\n"
            f"That is not {host}, so this number may be carrying real traffic.\n"
            "Re-run with --force if you are certain you want to repoint it."
        )

    print(f"  number   {chosen.phone_number}")
    print(f"  was      {current or '(none)'}")
    print(f"  now      {target} [POST]")

    chosen.update(voice_url=target, voice_method="POST")
    print("\nUpdated. Call the number — the app should answer with the welcome greeting.")
    print("Watch the uvicorn log for 'call CA... connected'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
