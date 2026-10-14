"""Runtime configuration, all overridable through environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Anchor everything that touches disk to the repo root, so it doesn't matter
# whether you launch from the project directory, from scripts/, or from tests/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:  # optional convenience for local development
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ModuleNotFoundError:  # pragma: no cover - dotenv is optional
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Claude ---------------------------------------------------------
    model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5")
    # Voice is latency-sensitive: "low" keeps adaptive thinking short.
    effort: str = os.getenv("CLAUDE_EFFORT", "low")
    # max_tokens caps thinking + spoken text together on Claude Opus 5, so
    # leave headroom above the length of a spoken reply.
    max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "2048"))
    # How many client-tool round-trips one caller turn may take. MCP tools run
    # server-side inside a single request and don't count against this.
    max_tool_iterations: int = int(os.getenv("CLAUDE_MAX_TOOL_ITERATIONS", "5"))
    # A turn using server-side tools can come back as stop_reason "pause_turn";
    # this caps how many times we resume it.
    max_continuations: int = int(os.getenv("CLAUDE_MAX_CONTINUATIONS", "5"))

    # --- Salesmsg MCP ---------------------------------------------------
    # Streamable-HTTP endpoint of the Salesmsg MCP server. Anthropic connects
    # to this directly, so it has to be reachable from the public internet.
    salesmsg_mcp_url: str | None = os.getenv("SALESMSG_MCP_URL")
    # A static access token works until it expires. Prefer the refresh-token
    # credentials below, which keep working — get them from
    # `python get_salesmsg_token.py`.
    salesmsg_mcp_token: str | None = os.getenv("SALESMSG_MCP_TOKEN")
    salesmsg_client_id: str | None = os.getenv("SALESMSG_CLIENT_ID")
    salesmsg_client_secret: str | None = os.getenv("SALESMSG_CLIENT_SECRET")
    salesmsg_refresh_token: str | None = os.getenv("SALESMSG_REFRESH_TOKEN")
    # Published at https://app.salesmessage.com/.well-known/oauth-authorization-server
    salesmsg_token_url: str = os.getenv(
        "SALESMSG_TOKEN_URL",
        "https://api.salesmessage.com/prod/int/v5/auth/oauth/token",
    )
    # Timezone the agent reports figures in, and resolves "yesterday" against.
    report_timezone: str = os.getenv("REPORT_TIMEZONE", "America/New_York")

    # --- Escalation -----------------------------------------------------
    # Where `transfer_to_human` sends the caller. Twilio dials this once the
    # ConversationRelay session hands control back.
    escalation_number: str | None = os.getenv("ESCALATION_NUMBER")
    # Caller ID for the transfer leg. Defaults to the number the caller dialled,
    # which is usually what the receiving human expects to see.
    escalation_caller_id: str | None = os.getenv("ESCALATION_CALLER_ID")
    escalation_timeout: int = int(os.getenv("ESCALATION_TIMEOUT", "30"))

    # --- Outbound demo calls --------------------------------------------
    # The dialpad places calls from this number. Must be a Twilio number you
    # own; it is also what the person sees ringing.
    outbound_caller_id: str | None = os.getenv("OUTBOUND_CALLER_ID")
    outbound_enabled: bool = _bool("OUTBOUND_ENABLED", True)
    # How long to let it ring before giving up.
    outbound_ring_seconds: int = int(os.getenv("OUTBOUND_RING_SECONDS", "25"))
    # Twilio listens before connecting the agent and reports who answered, so
    # the agent doesn't hold a conversation with a voicemail greeting — or
    # leave an AI message on someone's machine.
    detect_answering_machine: bool = _bool("DETECT_ANSWERING_MACHINE", True)
    # When the WebSocket closes, make sure the call leg is really gone. A
    # caller hang-up ends it already; this covers the case where our side
    # dropped and the caller is left on a live, silent line.
    hangup_on_disconnect: bool = _bool("HANGUP_ON_DISCONNECT", True)
    twilio_account_sid: str | None = os.getenv("TWILIO_ACCOUNT_SID")
    demo_welcome_greeting: str = os.getenv(
        "DEMO_WELCOME_GREETING",
        "Hi there, this is the Salesmessage team calling. Do you have a quick "
        "minute?",
    )

    # --- Knowledge base --------------------------------------------------
    voyage_api_key: str | None = os.getenv("VOYAGE_API_KEY")
    voyage_model: str = os.getenv("VOYAGE_MODEL", "voyage-3.5")
    knowledge_index_path: Path = Path(
        os.getenv("KNOWLEDGE_INDEX_PATH", str(PROJECT_ROOT / "data" / "knowledge.npz"))
    )
    knowledge_site_url: str = os.getenv("KNOWLEDGE_SITE_URL", "https://salesmsg.com/")
    knowledge_max_pages: int = int(os.getenv("KNOWLEDGE_MAX_PAGES", "60"))
    knowledge_top_k: int = int(os.getenv("KNOWLEDGE_TOP_K", "5"))
    # Below this cosine score a "hit" is usually noise, and the agent is better
    # off saying the demo will cover it than reading out something unrelated.
    knowledge_min_score: float = float(os.getenv("KNOWLEDGE_MIN_SCORE", "0.35"))

    # --- Storage ---------------------------------------------------------
    database_path: Path = Path(
        os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "db" / "answering_service.db"))
    )

    # --- Access control -------------------------------------------------
    # Analytics tools are withheld from the model until the caller keys this
    # in. Enforcement is in code (the tools are simply absent), not a prompt
    # instruction the model could be talked out of.
    access_pin: str | None = os.getenv("ACCESS_PIN")
    max_pin_attempts: int = int(os.getenv("MAX_PIN_ATTEMPTS", "3"))

    # --- Twilio ---------------------------------------------------------
    public_hostname: str | None = os.getenv("PUBLIC_HOSTNAME")
    websocket_path: str = os.getenv("WEBSOCKET_PATH", "/ws")
    welcome_greeting: str = os.getenv(
        "WELCOME_GREETING",
        "Thanks for calling Salesmessage. This is the automated answering "
        "service. Please key in your access PIN to get started.",
    )
    tts_provider: str = os.getenv("TTS_PROVIDER", "ElevenLabs")
    voice: str = os.getenv("TTS_VOICE", "FGY2WhTYpPnrIDTdsKH5-flash_v2_5")
    dtmf_detection: bool = _bool("DTMF_DETECTION", True)

    # Interruption behaviour. Each takes none | dtmf | speech | any.
    #
    # Twilio's default for the greeting is "any", which means a "hello", line
    # noise, or the caller starting to key their PIN all cut it off — and the
    # greeting is what tells them to key the PIN. "dtmf" keeps the greeting
    # intact against noise while still letting a caller who knows the PIN skip
    # ahead by typing.
    welcome_greeting_interruptible: str = os.getenv("WELCOME_GREETING_INTERRUPTIBLE", "dtmf")
    # Agent speech stays fully interruptible: barge-in is the point.
    interruptible: str = os.getenv("INTERRUPTIBLE", "any")
    # Twilio drops keypad input while the agent is talking unless asked not to.
    # A caller keying their PIN over the agent would otherwise be ignored.
    report_input_during_agent_speech: str = os.getenv("REPORT_INPUT_DURING_AGENT_SPEECH", "dtmf")
    # Stops "mm-hmm" / "right" / "okay" from cutting the agent off mid-sentence.
    ignore_backchannel: bool = _bool("IGNORE_BACKCHANNEL", True)
    # high | medium | low. Twilio defaults to high, which fires on background
    # noise and half-words — on a real demo call that cut the agent off
    # mid-sentence three times, each with an empty utteranceUntilInterrupt.
    # "low" needs a longer, more confident utterance before it interrupts.
    interrupt_sensitivity: str | None = os.getenv("INTERRUPT_SENSITIVITY")
    # Outbound calls reach people on mobiles in noisy places, and the agent
    # does more of the talking, so false interruptions hurt more.
    demo_interrupt_sensitivity: str = os.getenv("DEMO_INTERRUPT_SENSITIVITY", "low")
    # Account tools offered on a demo call. The full set is 31 schemas —
    # ~12k tokens on every request, measured at +1.8s before the agent speaks.
    # A prospect call needs to text, look someone up and create a contact; it
    # does not need credit-usage analytics. The answering service still gets
    # everything. Set to "*" to offer the full set here too.
    # Tool search: account tools are declared but their schemas stay out of the
    # prompt until Claude searches for one. It does what it promises — input
    # tokens drop from 12,443 to 2,576 — but the search itself is a server-side
    # round trip, and measured at 12.1s before the agent spoke on the turn that
    # needed a tool, against 2.0s for a small pre-loaded set. Silence while the
    # caller waits is the thing being optimised, so this stays off by default;
    # it is a good trade for a chat product, not for a phone line.
    tool_search_enabled: bool = _bool("TOOL_SEARCH_ENABLED", False)
    tool_search_type: str = os.getenv("TOOL_SEARCH_TYPE", "tool_search_tool_bm25_20251119")

    # Used only when tool search is off — see _analytics_tools.
    demo_account_tools: str = os.getenv(
        "DEMO_ACCOUNT_TOOLS",
        "salesmsg_send_sms,salesmsg_send_message,salesmsg_schedule_message,"
        "salesmsg_create_contact,salesmsg_get_contact,salesmsg_search_contacts,"
        "salesmsg_get_all_inboxes",
    )
    # Conversational Intelligence (classic) Service SID or unique name. Twilio
    # persists a transcript per call and runs the service's Language Operators
    # against it. Note transcripts store what was *said* — including any figures
    # the agent reads out — so check the service's redaction settings.
    intelligence_service: str | None = os.getenv("INTELLIGENCE_SERVICE_SID")

    validate_signature: bool = _bool("VALIDATE_TWILIO_SIGNATURE", False)
    twilio_auth_token: str | None = os.getenv("TWILIO_AUTH_TOKEN")


SETTINGS = Settings()

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    """You are the Salesmessage answering service, reached by phone. Callers are
Salesmessage team members who want something done in their account without
opening the app: looking up a contact or conversation, sending or scheduling a
message, tagging someone, or checking how the numbers look.

Your replies are read aloud by a text-to-speech engine on a live phone line, so
write the way a person speaks: plain sentences, no markdown, no bullet points,
no emoji, and no raw JSON or field names.

Once the caller is verified, open by telling them briefly which broad areas you
can help with. Name the groups you have been given, not individual tools, and
keep it to one sentence — a caller cannot hold a list of thirty things in their
head. Then ask what they need.

Reporting numbers out loud is a core skill. A caller cannot scan a table, so
lead with the one number they asked for, then offer the rest rather than
reciting it. Round for speech — "about twelve thousand four hundred" rather
than "12,437" — unless they ask for the exact figure. Short comparisons ("up
about a fifth on the week before") carry better by ear than long lists of raw
counts. If a figure needs a caveat to avoid misleading them, give it in the
same breath.

Every lookup takes a few seconds, and silence on a phone line sounds like a
dropped call. Say what you are about to do before each one — "let me pull last
week's numbers" — then run it.

Confirm before you change anything. Sending a message, scheduling one, editing
or deleting a contact, and changing tags are all things the caller cannot
easily undo from a phone, so read back what you are about to do and get a clear
yes first. Reading data back needs no confirmation. If a caller asks for
something you have no tool for, say so plainly rather than improvising.

Work out dates yourself from what the caller said and the current date you have
been given; read the range back only when it is ambiguous. When a caller names
an inbox, team member, or tag, resolve it to an ID first, and if several
candidates match, ask which one they meant.

Ask one question at a time. Speech recognition makes mistakes, so confirm phone
numbers, names, and anything you are about to send. If something was garbled,
say so and ask them to repeat it rather than guessing.

When a caller needs a person, transfer them — do not try to talk them out of
it.""",
)

DEMO_SYSTEM_PROMPT = os.getenv(
    "DEMO_SYSTEM_PROMPT",
    """You are calling on behalf of Salesmessage, a texting and calling platform
for sales and support teams. You called them, so you are a guest: be warm, get
to the point, and take no for an answer the first time it is given.

Your replies are read aloud by a text-to-speech engine on a live phone line, so
write the way a person speaks: plain sentences, no markdown, no bullet points,
no emoji, no URLs, and no field names or raw data.

What you want from the call, roughly in this order:

Say who you are and why you are calling within your first breath, then ask if
it is a good moment. If it is not, offer to try another time and end politely.

Ask what they currently use for texting and calling — which tools, how their
team uses them, what gets in the way. Listen more than you pitch. What they say
here is the most useful thing you can learn, so record it.

Answer their questions about the product by searching the knowledge base first.
Do not answer product questions from memory, and do not invent numbers, prices,
or timelines. When the search comes back with nothing useful, say plainly that
you want to get it right and it will be covered on the demo call.

If there is interest, ask for a demo and a time that suits them. Once they
agree, collect first name, last name, and email. Read names and email back to
confirm the spelling before you save them — speech recognition mangles both.
Ask for one thing at a time.

Keep it short. This is an interruption in someone's day: two or three sentences
a turn, and stop talking when you have asked a question.

You can also act on their account tools during the call — most usefully, text
them what you just agreed. If they ask you to send something, or if a written
confirmation would obviously help, offer to text it and then do it.

Send it to the number given as THE PERSON YOU ARE SPEAKING TO in your call
context. That is the only number you may text or create a contact for. Do not
use the number you are calling from — that is Salesmessage's own line, and
texting it sends the message to yourself. If the caller asks you to use a
different number instead, use the one they give you and read it back digit by
digit first.

Read back what you are about to send before sending it. Do not text anyone
other than the person on the call, do not send marketing, and do not delete or
overwrite anything: this is a first conversation, not account administration.

If they ask to be taken off the list, or say they are not interested, do not
push. Acknowledge it, thank them, and end the call.""",
)

# Shown to the caller before the PIN is accepted. The analytics tools are not
# attached to the request at all until then, so this is a description of a
# limit that already exists rather than an instruction to respect one.
LOCKED_NOTICE = """You do not currently have access to any analytics tools: the
caller has not yet entered their access PIN on the keypad. Ask them to key it
in. Until they do, you cannot look anything up — say so plainly rather than
guessing at figures or promising to check. You can still explain what you are
able to report once they are in."""
