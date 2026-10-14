# Salesmessage Answering Service

A phone line for your Salesmessage account. Call the number, key in a PIN, and
ask for what you need — look up a contact or conversation, send or schedule a
message, tag someone, or check how the numbers look.

[Twilio ConversationRelay](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay)
handles the call, speech-to-text, and text-to-speech; the
[Claude API](https://platform.claude.com/docs) handles the conversation; the
Salesmsg MCP server does the work.

The structure comes from
[bentoml/BentoTwilioConversationRelay](https://github.com/bentoml/BentoTwilioConversationRelay),
with the self-hosted vLLM service replaced by the Claude API — no GPU, no model
weights, no serving infrastructure — and the demo tool replaced by a real MCP
server.

## How it works

```
caller ──PSTN──> Twilio ──ConversationRelay──> this app ──Messages API──> Claude
                    │                              │
              STT + TTS                    persistent MCP session
                                                   │
                                           Salesmsg MCP server
```

| Piece | Responsibility |
| --- | --- |
| Twilio | Call media, speech-to-text, text-to-speech, barge-in detection, DTMF |
| This app | Conversation state, streaming, PIN gate, MCP session, transfer/hang-up |
| Claude | Deciding what to say, which tools to call, how to say a number out loud |
| Salesmsg MCP | Everything that touches the account |

Three endpoints:

- `POST /twiml` — Twilio's inbound-call webhook. Returns TwiML that opens a
  ConversationRelay session pointed at the WebSocket below.
- `WS /ws` — the live call.
- `POST /handoff` — Twilio hits this when the session ends, so a requested
  transfer can dial a human.

## Layout

```
app/       main.py — FastAPI app, lifespan, router registration
api/       HTTP and WebSocket surface
           health.py, voice.py (/twiml, /handoff), relay.py (/ws),
           dependencies.py (host normalization, Twilio signature check)
core/      config.py — settings, system prompt, PROJECT_ROOT
services/  call_session.py  one call: history, PIN gate, Claude loop, barge-in
           salesmsg_mcp.py  persistent MCP session, tool discovery, grouping
           salesmsg_auth.py OAuth token refresh and rotation
           call_tools.py    handlers for transfer_to_human / end_call
schemas/   call_tools.py — JSON schemas for the call-control tools
scripts/   operator tooling (see below)
tests/     test_relay.py (offline), test_live.py (real Claude)
docs/      scratch HTTP requests
```

`models/` and `db/` are unused — there is no persistence layer; conversation
state lives in memory for the duration of a call.

Run it from the repo root:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Scripts

Each runs standalone from the repo root; they put the root on `sys.path`
themselves.

| Script | What it does |
| --- | --- |
| `scripts/get_salesmsg_token.py` | One-time OAuth flow for Salesmsg MCP credentials |
| `scripts/setup_twilio.py` | Points a Twilio number at this app |
| `scripts/preflight.py` | Verifies the public path through ngrok, WebSocket included |
| `scripts/verify_signature.py` | Sends a correctly-signed Twilio webhook |
| `scripts/fetch_transcript.py` | Reads back Conversational Intelligence transcripts |
| `scripts/bench_latency.py` | Time-to-first-spoken-token per model/effort |
| `scripts/bench_mcp.py` | Why the MCP connector was replaced (see Latency) |

## What the agent can do

Whatever the Salesmsg MCP server exposes — currently 31 tools across
**reporting and analytics**, **conversations**, **tags**, **messages**,
**contacts**, and **inboxes and team**. Nothing is hardcoded: the tool list
comes from the server at connect time, so adding a tool there adds it to the
phone agent with no change here.

Those group names are derived from the tool names themselves, and the agent
reads them out once the caller is verified.

## Setup

```bash
pip install -r requirements.txt
```

Fill in `.env` (see the file itself for the full annotated list):

```ini
ANTHROPIC_API_KEY=sk-ant-...
SALESMSG_MCP_URL=https://mcp.salesmessage.com
SALESMSG_CLIENT_ID=...
SALESMSG_REFRESH_TOKEN=...
ACCESS_PIN=     
ESCALATION_NUMBER=
REPORT_TIMEZONE=America/New_York
```

### Getting Salesmsg MCP credentials

`https://mcp.salesmessage.com` is an OAuth 2.0 protected resource whose
authorization server (`https://app.salesmessage.com`) supports only
`authorization_code` and `refresh_token` — **there is no client_credentials
grant**, so there is no token to copy out of a settings page. A human approves
access once in a browser; after that you hold a refresh token.

```bash
python scripts/get_salesmsg_token.py --write-env
```

It discovers the endpoints, registers a client, runs authorization_code + PKCE
against a local callback on port 8765, verifies the result with a real MCP
`initialize`, decodes the granted scopes out of the access token, and writes
the credentials into `.env`.

Two things that will bite otherwise:

- **The MCP endpoint is the root** — `https://mcp.salesmessage.com`, not
  `/mcp`, which 404s.
- **A client can only request the scopes it was registered with.** Widening
  scopes needs `--new-client`; reusing the old client fails the token exchange
  with "The requested scope is invalid, unknown, or malformed".

`python scripts/get_salesmsg_token.py --show-scopes` prints what the current
token actually carries, without re-authorizing.

Access tokens last a day, so `.env` holds the **refresh token**, and
`services/salesmsg_auth.py` mints access tokens on demand, caching each until
two minutes before it expires. Salesmessage rotates the refresh token on every
use; the new one is written to `.salesmsg_tokens.json` (gitignored) and takes
precedence over `.env` on the next start. If a refresh fails, the call still
runs — the agent simply has no account tools and says so.

### Going live

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
ngrok http 8000
python scripts/preflight.py                                  # public path, end to end
python scripts/setup_twilio.py --host <ngrok-host> --number +1...
```

## Access control

Anyone who dials reaches a system that can read *and change* your account, so
the gate is enforced in code rather than in the prompt: **until the caller keys
in `ACCESS_PIN`, the MCP tools are not attached to the request at all.** The
model has nothing to reach for, so there is nothing to talk it out of. A locked
caller who asks for numbers is told to enter the PIN and gets no figures —
verified in `tests/test_live.py`.

Three wrong attempts (`MAX_PIN_ATTEMPTS`) end the call. `*` clears the buffer,
`#` submits early. If `ACCESS_PIN` is unset the app **fails closed** and says so
at startup.

With write scopes granted, the line can send messages and delete contacts. The
"confirm before changing anything" rule is a prompt instruction, not a hard
gate. If you want a structural limit, narrow the OAuth scopes rather than
relying on the prompt.

`VALIDATE_TWILIO_SIGNATURE=true` rejects requests that aren't signed by Twilio.

## Verifying without a phone

```bash
python tests/test_relay.py       # offline: protocol, PIN gate, barge-in, transfer
python tests/test_live.py        # real Claude and real MCP
python scripts/preflight.py      # the public ngrok path, including the WebSocket
python scripts/verify_signature.py
```

## The parts worth understanding

**Streaming is not optional.** Each text delta is forwarded to Twilio as a
`{"type": "text", "token": ...}` message the moment it arrives, so TTS starts
speaking before Claude has finished the sentence. Waiting for the full response
adds its entire generation time to the silence the caller hears.

**Barge-in cancels the request.** When the caller talks over the agent, Twilio
sends `interrupt` with `utteranceUntilInterrupt` — the words it actually spoke
before stopping. We cancel the in-flight task and rewrite the last assistant
turn to exactly that text. Skip this and Claude's next turn is built on a
paragraph nobody heard.

**The MCP session is ours, and it is persistent.** This originally used
Anthropic's MCP connector, which opens a session and lists the server's tools on
*every* request: measured at **+4s to first spoken token, paid even on turns
that used no tools** (`scripts/bench_mcp.py`). Holding one session here pays
that handshake once per process. The server itself is quick — initialize 0.57s,
list_tools 0.79s, a tool call ~0.3s.

**Transfers need Twilio to hand control back.** `transfer_to_human` ends the
ConversationRelay session with `handoffData`; Twilio then POSTs `/handoff`,
which replies with `<Dial>`. Without the `action` attribute on `<Connect>` the
caller would just be hung up on.

**The model needs today's date.** Analytics tools take `from_date`/`to_date` as
`YYYY-MM-DD` and callers say "last week". The current date and reporting
timezone go into the per-call system context — computed once per call, so a
ticking clock doesn't sit in the cached prefix and cost a cache miss every
minute.

**Numbers have to be spoken, not printed.** The system prompt asks for rounded
figures, one headline number first, and no raw JSON or field names.

## Notes and limits

- Conversation state is in memory, keyed to the WebSocket. A process restart
  drops in-flight calls.
- Unlocking mid-call changes the tool list, which invalidates that call's prompt
  cache once. Cheap at one miss per call.
- With `INTELLIGENCE_SERVICE_SID` set, Twilio stores a transcript of every call
  — including any figures read aloud. Check the service's redaction settings.
- No cap on conversation length. For very long calls, add
  [compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)
  or trim old turns.
- On Windows, `zoneinfo` needs the `tzdata` package (in `requirements.txt`).
  Without it the app falls back to UTC rather than failing the call.
