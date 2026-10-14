"""Read back what Conversational Intelligence recorded for a call.

With `intelligenceService` set on <ConversationRelay>, Twilio writes a
Transcript per call and runs the service's Language Operators against it. This
prints the conversation and whatever the operators concluded — the observability
half of the feature, which is otherwise only visible in the Console.

    python fetch_transcript.py                  # most recent transcripts
    python fetch_transcript.py --sid GX...      # one transcript in full
    python fetch_transcript.py --operators      # what the service runs
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

try:
    from twilio.base.exceptions import TwilioException
    from twilio.rest import Client
except ModuleNotFoundError:
    sys.exit("Needs the `twilio` package: pip install -r requirements.txt")

from core.config import SETTINGS


def client() -> Client:
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        sys.exit("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env.")
    return Client(sid, token)


def show_operators(c: Client, service_sid: str) -> None:
    print(f"Service {service_sid}\n")
    try:
        service = c.intelligence.v2.services(service_sid).fetch()
        print(f"  name            {service.unique_name}")
        print(f"  auto transcribe {service.auto_transcribe}")
        print(f"  auto redaction  {service.auto_redaction}")
        print(f"  language        {service.language_code}")
    except TwilioException as exc:
        print(f"  could not fetch the service: {exc}")

    print("\n  Prebuilt operators available to attach:")
    try:
        for op in c.intelligence.v2.prebuilt_operators.list(limit=30):
            print(f"    {op.sid}  {op.friendly_name}")
    except TwilioException as exc:
        print(f"    could not list operators: {exc}")
    print("\n  Attach the ones you want in the Twilio Console; this script only reads.")


def show_transcript(c: Client, sid: str) -> None:
    transcript = c.intelligence.v2.transcripts(sid).fetch()
    print(f"Transcript {transcript.sid}")
    print(f"  status     {transcript.status}")
    print(f"  duration   {transcript.duration}s")
    print(f"  created    {transcript.date_created}")

    print("\n  Conversation:")
    try:
        for sentence in c.intelligence.v2.transcripts(sid).sentences.list():
            who = getattr(sentence, "media_channel", "?")
            speaker = {0: "caller", 1: "agent"}.get(who, f"channel {who}")
            print(f"    [{speaker:6}] {sentence.transcript}")
    except TwilioException as exc:
        print(f"    could not read sentences: {exc}")

    print("\n  Language Operator results:")
    try:
        results = c.intelligence.v2.transcripts(sid).operator_results.list()
        if not results:
            print("    (none — no operators attached to the service, or still processing)")
        for r in results:
            print(f"    {r.name} [{r.operator_type}]")
            for field in ("predicted_label", "predicted_probability", "text_generation_results",
                          "extract_results", "label_probabilities"):
                value = getattr(r, field, None)
                if value:
                    print(f"        {field}: {str(value)[:300]}")
    except TwilioException as exc:
        print(f"    could not read operator results: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sid", help="Transcript SID (GX...) to show in full")
    parser.add_argument("--operators", action="store_true", help="Show the service config and available operators")
    parser.add_argument("--limit", type=int, default=10, help="How many recent transcripts to list")
    args = parser.parse_args()

    c = client()

    if args.operators:
        service = SETTINGS.intelligence_service
        if not service:
            sys.exit("INTELLIGENCE_SERVICE_SID is not set in .env.")
        show_operators(c, service)
        return 0

    if args.sid:
        show_transcript(c, args.sid)
        return 0

    try:
        transcripts = c.intelligence.v2.transcripts.list(limit=args.limit)
    except TwilioException as exc:
        sys.exit(f"could not list transcripts: {exc}")

    if not transcripts:
        print("No transcripts yet.")
        print("Transcripts appear a short while after a call ends, and only when")
        print("INTELLIGENCE_SERVICE_SID was set on the TwiML that answered it.")
        return 0

    print(f"{'SID':<36} {'status':<12} {'dur':>5}  created")
    for t in transcripts:
        print(f"{t.sid:<36} {t.status:<12} {str(t.duration) + 's':>5}  {t.date_created}")
    print("\nShow one in full:  python fetch_transcript.py --sid <SID>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
