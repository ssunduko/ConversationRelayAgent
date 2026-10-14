# Salesmessage Voice Agents — operator guide

Two phone applications share one FastAPI process, one Twilio account, one
Claude client and one Salesmsg MCP session:

| | **Answering service** | **Demo dialer** |
| --- | --- | --- |
| Direction | Inbound — someone calls the Twilio number | Outbound — an operator dials a prospect from a web page |
| Who is on the line | A Salesmessage team member | A prospect who has never used the product |
| Gate | Keypad PIN; account tools are withheld until it is entered | None — the prospect never reaches account data |
| Persona | `SYSTEM_PROMPT` in `core/config.py` | `DEMO_SYSTEM_PROMPT` in `core/config.py` |
| Tools | Every Salesmsg MCP tool (31), plus transfer / hang-up | Knowledge search, lead capture, booking, plus a 7-tool account allowlist |
| Memory | None — state dies with the call | SQLite: who called, what was said, what was booked |
| Entry point | `POST /twiml` (Twilio webhook) | `GET /` (the dialpad page) |

Which one a call becomes is decided by a single TwiML parameter. Outbound TwiML
carries `<Parameter name="mode" value="demo" />`; everything without it is the
answering service. Both then run through the same WebSocket (`/ws`) and the
same `CallSession` object.

```
                     ┌─────────────── one uvicorn process ───────────────┐
 caller ─PSTN─┐      │                                                   │
              ├─ Twilio ─ConversationRelay─ WS /ws ─ CallSession ─┬─ Claude API
 prospect ────┘      │         (STT + TTS)          mode=?        ├─ Salesmsg MCP
        ▲            │                                            ├─ Voyage AI
        └── dialpad ─┤ POST /calls → Twilio REST                  └─ SQLite
                     └───────────────────────────────────────────────────┘
```

Detailed C4 diagrams live beside this file: `c4-1-context.puml` through
`c4-6-deployment.puml`, plus a dynamic view per application
(`c4-4-dynamic-answering.puml`, `c4-5-dynamic-demo.puml`). Render them with
`plantuml docs/c4-*.puml`.

---

## Application 1 — the answering service

A phone line for your Salesmessage account. Call the number, key in a PIN, and
ask for what you need: look up a contact or conversation, send or schedule a
message, tag someone, or check how the numbers look.

**Endpoints**

| Route | Purpose |
| --- | --- |
| `POST /twiml` | Twilio's inbound-call webhook. Returns TwiML opening a ConversationRelay session pointed at `/ws`. |
| `WS /ws` | The live call. Transcribed speech in, streamed text tokens out. |
| `POST /handoff` | Twilio calls this when the session ends, so a requested transfer can `<Dial>` a human. |

**The PIN gate.** Anyone who dials reaches a system that can read *and change*
the account, so the gate is enforced in code, not in the prompt: until the
caller keys in `ACCESS_PIN`, the MCP tools are not attached to the request at
all. The model has nothing to reach for, so there is nothing to talk it out of.
`*` clears the buffer, `#` submits early, and `MAX_PIN_ATTEMPTS` wrong tries
end the call. If `ACCESS_PIN` is unset the app **fails closed** and logs a
warning at startup.

**What it can do** is whatever the Salesmsg MCP server exposes — currently 31
tools across reporting and analytics, conversations, tags, messages, contacts,
and inboxes and team. Nothing is hardcoded: the list is fetched from the server
at connect time, so a tool added there appears on the phone line with no change
here. The group names are derived from the tool names and read out once the
caller is verified.

**Transfers.** `transfer_to_human` ends the ConversationRelay session with
`handoffData`; Twilio then POSTs `/handoff`, which replies with `<Dial>` to
`ESCALATION_NUMBER`. Without the `action` attribute on `<Connect>` the caller
would simply be hung up on.

---

## Application 2 — the outbound demo dialer

An operator opens the dialpad, types a number, and the agent calls it: it
introduces itself, asks what the prospect uses today, answers product questions
out of a vector index of the Salesmsg feature list and website, captures the
lead, books a demo, and can text a confirmation.

**Endpoints**

| Route | Purpose |
| --- | --- |
| `GET /` | The dialpad page (`app/static/dialpad.html`) — number pad, call progress, hang-up. |
| `POST /calls` | Places the call via the Twilio REST API. Body: `{"to": "..."}`; accepts anything keypad-shaped and normalizes to E.164. |
| `GET /calls/{sid}` | Progress for the page to poll: queued, ringing, in-progress, completed. |
| `POST /calls/{sid}/hangup` | The hang-up button. |
| `POST /outbound-twiml` | TwiML for a call we placed — same relay, `mode=demo`. |
| `POST /amd-status` | Asynchronous answering-machine result. |
| `POST /call-status` | Twilio's progress callbacks: initiated, ringing, answered, completed. |
| `GET /explore` | The vector-store explorer page (see below). |

**Answering-machine detection.** Detection is *asynchronous*: Twilio connects
the call immediately and reports who answered afterwards. Synchronous detection
makes it listen before fetching the TwiML, so the person picks up to several
seconds of silence and often says "hello?" twice — or hangs up first. If the
verdict is `machine` or `fax`, at `/outbound-twiml` or later at `/amd-status`,
the call is ended rather than left as an AI voicemail. Turn it off with
`DETECT_ANSWERING_MACHINE=false`.

**The knowledge base.** `data/knowledge.npz` holds L2-normalized Voyage
embeddings of the product feature list (`data/features.json`) and a crawl of
`KNOWLEDGE_SITE_URL`. A search is one embedding call plus a dot product over a
few hundred vectors — fast enough to sit inside a phone turn. Hits below
`KNOWLEDGE_MIN_SCORE` are dropped, and the agent says the demo will cover it
rather than reading out something unrelated. If the index is missing or Voyage
is unreachable the same fallback applies, so a demo call still works — it just
cannot answer product questions.

**Memory.** `db/answering_service.db` (SQLite) holds `callers`, `conversations`
and `appointments`, keyed by phone number, written on a worker thread so a slow
disk never stalls a live call. A second call to the same person starts from
what is already known. Every knowledge lookup is recorded with the
conversation, including the ones that found nothing — that list is the
shortlist of what to add to the index.

**Demo tools** (`schemas/demo_tools.py`, `services/demo_tools.py`):
`search_product_knowledge`, `save_caller_details`, `book_demo`.

**Account tools on a demo call** are a deliberate seven-tool allowlist
(`DEMO_ACCOUNT_TOOLS`) — enough to text the prospect, look someone up and
create a contact. The full 31 schemas are ~12k tokens on every request,
measured at +1.8s before the agent speaks; a prospect call does not need
credit-usage analytics. Set it to `*` to offer everything.

---

## What the two applications share

- **Streaming is not optional.** Each text delta is forwarded to Twilio as
  `{"type": "text", "token": ...}` the moment it arrives, so TTS starts
  speaking before Claude finishes the sentence.
- **Barge-in cancels the in-flight request.** Twilio sends `interrupt` with
  `utteranceUntilInterrupt` — the words it actually spoke. The task is
  cancelled and the last assistant turn rewritten to exactly that text, so the
  next turn is not built on a paragraph nobody heard.
- **One persistent MCP session per process.** Anthropic's MCP connector opens a
  session and lists tools on *every* request: +4s to first spoken token, paid
  even on turns that use no tools (`scripts/bench_mcp.py`). Holding the session
  here pays that handshake once.
- **Today's date goes into the per-call system context**, computed once per
  call so a ticking clock does not sit in the cached prefix.
- **Numbers are spoken, not printed.** Rounded figures, one headline number
  first, no raw JSON or field names.
- **Logging** goes to `logs/server.log`, warnings and errors also to
  `logs/server.err.log` (5 MB rotation, 3 generations), configured in the app
  so it behaves the same from a shell or an IDE.
- **`GET /health`** is the liveness endpoint for both: `{"status": "ok",
  "model": ...}`.

---

## Configuration

Everything is environment variables, read once at import in `core/config.py`
and loaded from `.env` if `python-dotenv` is installed. Paths are anchored to
the repo root, so it does not matter which directory you launch from.

### Required

| Variable | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | both | Read by the SDK directly. |
| `SALESMSG_MCP_URL` | both | `https://mcp.salesmessage.com` — the **root**, not `/mcp`, which 404s. |
| `SALESMSG_CLIENT_ID` + `SALESMSG_REFRESH_TOKEN` | both | From `scripts/get_salesmsg_token.py`. Without them there are no account tools. |
| `ACCESS_PIN` | answering | Digits only. Unset means locked forever. |
| `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` | dialer (and signature checks) | Needed to place, poll and end outbound calls. |
| `OUTBOUND_CALLER_ID` | dialer | A Twilio number you own; what the prospect sees ring. |
| `VOYAGE_API_KEY` | dialer | Free tier at dashboard.voyageai.com. Without it the index loads but cannot be searched. |

### Claude

| Variable | Default | Notes |
| --- | --- | --- |
| `CLAUDE_MODEL` | `claude-opus-5` | |
| `CLAUDE_EFFORT` | `low` | Voice is latency-sensitive; keeps adaptive thinking short. |
| `CLAUDE_MAX_TOKENS` | `2048` | Caps thinking **and** speech together — leave headroom above the reply length. |
| `CLAUDE_MAX_TOOL_ITERATIONS` | `5` | Client-tool round-trips per caller turn. MCP tools run server-side and do not count. |
| `CLAUDE_MAX_CONTINUATIONS` | `5` | How many times a `pause_turn` may resume. |
| `SYSTEM_PROMPT` / `DEMO_SYSTEM_PROMPT` | see `core/config.py` | Override the persona of either application. |

### Salesmsg MCP and auth

| Variable | Default | Notes |
| --- | --- | --- |
| `SALESMSG_MCP_TOKEN` | — | Static access token fallback. Works until it expires, then fails with a 401 only visible in the log. |
| `SALESMSG_CLIENT_SECRET` | — | Only if your client was registered confidential. |
| `SALESMSG_TOKEN_URL` | `https://api.salesmessage.com/prod/int/v5/auth/oauth/token` | Override only if the endpoint moves. |
| `REPORT_TIMEZONE` | `America/New_York` | IANA name. Resolves "yesterday" and dates every figure. |
| `TOOL_SEARCH_ENABLED` | `false` | Keeps tool schemas out of the prompt until Claude searches. Input tokens drop 12,443 → 2,576, but the search is a server round trip: 12.1s before the agent spoke, against 2.0s with a small pre-loaded set. Good for chat, bad for a phone line. |
| `TOOL_SEARCH_TYPE` | `tool_search_tool_bm25_20251119` | Only read when the above is on. |
| `DEMO_ACCOUNT_TOOLS` | 7-tool allowlist | Comma-separated, or `*` for all. |

### Twilio and voice

| Variable | Default | Notes |
| --- | --- | --- |
| `PUBLIC_HOSTNAME` | — | Leave unset to derive the host per request, which survives ngrok handing you a new URL. Scheme and path are stripped, so a pasted full URL is tolerated. |
| `WEBSOCKET_PATH` | `/ws` | |
| `WELCOME_GREETING` | "Thanks for calling Salesmessage…" | Inbound. |
| `DEMO_WELCOME_GREETING` | "Hi there, this is the Salesmessage team calling…" | Outbound. |
| `TTS_PROVIDER` / `TTS_VOICE` | `ElevenLabs` / `FGY2WhTYpPnrIDTdsKH5-flash_v2_5` | |
| `DTMF_DETECTION` | `true` | Required for the PIN. |
| `WELCOME_GREETING_INTERRUPTIBLE` | `dtmf` | Twilio's default `any` lets a "hello" or line noise kill the greeting — which is the part that tells the caller to key the PIN. |
| `INTERRUPTIBLE` | `any` | Agent speech stays fully interruptible; barge-in is the point. |
| `REPORT_INPUT_DURING_AGENT_SPEECH` | `dtmf` | Otherwise Twilio drops keypad input while the agent talks. |
| `IGNORE_BACKCHANNEL` | `true` | Stops "mm-hmm" cutting the agent off. |
| `INTERRUPT_SENSITIVITY` | Twilio default (`high`) | `low` needs a longer, more confident utterance before interrupting. |
| `DEMO_INTERRUPT_SENSITIVITY` | `low` | Outbound reaches mobiles in noisy places; false interruptions hurt more. |
| `VALIDATE_TWILIO_SIGNATURE` | `false` | Set `true` in production; needs `TWILIO_AUTH_TOKEN`. |
| `INTELLIGENCE_SERVICE_SID` | — | Conversational Intelligence service SID. Twilio then stores a transcript of every call, **including any figures read aloud** — check the service's redaction settings. |

### Outbound calling

| Variable | Default |
| --- | --- |
| `OUTBOUND_ENABLED` | `true` — `false` makes `POST /calls` return 403 |
| `OUTBOUND_RING_SECONDS` | `25` |
| `DETECT_ANSWERING_MACHINE` | `true` |
| `HANGUP_ON_DISCONNECT` | `true` — if our socket drops, end the leg rather than leave a live silent line |

### Escalation

| Variable | Default |
| --- | --- |
| `ESCALATION_NUMBER` | unset — transfers hang up instead |
| `ESCALATION_CALLER_ID` | the number the caller dialled |
| `ESCALATION_TIMEOUT` | `30` |

### Knowledge base and storage

| Variable | Default |
| --- | --- |
| `VOYAGE_MODEL` | `voyage-3.5` |
| `KNOWLEDGE_INDEX_PATH` | `data/knowledge.npz` |
| `KNOWLEDGE_SITE_URL` | `https://salesmsg.com/` |
| `KNOWLEDGE_MAX_PAGES` | `60` |
| `KNOWLEDGE_TOP_K` | `5` |
| `KNOWLEDGE_MIN_SCORE` | `0.35` |
| `DATABASE_PATH` | `db/answering_service.db` |

---

## Starting them

### 1. Install

```bash
pip install -r requirements.txt
```

`requirements.txt` is currently incomplete — four packages the code imports are
not listed. Install them too:

```bash
pip install mcp numpy voyageai beautifulsoup4
```

(`mcp` and `numpy` are needed to start the process at all; `voyageai` for
knowledge search; `beautifulsoup4` only for `scripts/build_index.py`.)

### 2. Salesmsg MCP credentials

`https://mcp.salesmessage.com` is an OAuth 2.0 protected resource whose
authorization server supports only `authorization_code` and `refresh_token` —
**there is no client_credentials grant**, so there is no token to copy out of a
settings page. A human approves access once in a browser.

```bash
python scripts/get_salesmsg_token.py --write-env
```

It discovers the endpoints, registers a client, runs authorization_code + PKCE
against a local callback on port 8765, verifies the result with a real MCP
`initialize`, decodes the granted scopes, and writes the credentials into
`.env`.

Two things that will otherwise bite:

- **The MCP endpoint is the root**, not `/mcp`.
- **A client can only request the scopes it was registered with.** Widening
  scopes needs `--new-client`; reusing the old one fails the token exchange
  with "The requested scope is invalid, unknown, or malformed".

`--show-scopes` prints what the current token carries without re-authorizing.

Access tokens last a day, so `.env` holds the **refresh token** and
`services/salesmsg_auth.py` mints access tokens on demand, caching each until
two minutes before expiry. Salesmessage rotates the refresh token on every use;
the new one is written to `.salesmsg_tokens.json` (gitignored) and outranks
`.env` on the next start. If a refresh fails the call still runs — the agent
simply has no account tools and says so.

### 3. Build the knowledge index (demo dialer only)

```bash
python scripts/build_index.py                 # features + crawl salesmsg.com
python scripts/build_index.py --no-crawl      # features only, no network
python scripts/build_index.py --max-pages 100
```

Re-run it whenever `data/features.json` or the website changes. Without an
index the process still starts and inbound calls are unaffected; demo calls
just cannot answer product questions.

### 4. Run the process

Both applications come up together:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Startup logs tell you what is and is not wired up — a missing PIN, missing
Salesmsg credentials, a missing knowledge index, an unset escalation number.
Check `logs/server.log` if you started it detached.

### 5. Expose it and point Twilio at it

```bash
ngrok http 8000
python scripts/preflight.py                              # the whole public path, WebSocket included
python scripts/setup_twilio.py --host <ngrok-host> --number +1...
```

`preflight.py` finds your running tunnel, POSTs `/twiml` through the public URL
exactly as Twilio's webhook does, then opens the advertised `wss://` endpoint
and runs one turn of conversation through it. If it passes, everything except
the telephony itself is verified.

### 6. Use them

- **Answering service** — dial the Twilio number you configured, key in
  `ACCESS_PIN`, and ask for what you need.
- **Demo dialer** — open `https://<host>/` (or `http://localhost:8000/`), type
  a number, press call. Watch the status line; hang up from the same page.
- **Index explorer** — `https://<host>/explore`, after generating the page with
  `python scripts/visualize_index.py`. It serves a 404 with that instruction if
  you have not.

---

## Operator scripts

Each runs standalone from the repo root; they put the root on `sys.path`
themselves.

| Script | What it does |
| --- | --- |
| `get_salesmsg_token.py` | One-time OAuth flow for Salesmsg MCP credentials |
| `setup_twilio.py` | Points a Twilio number at this app |
| `preflight.py` | Verifies the public path through ngrok, WebSocket included |
| `verify_signature.py` | Sends a correctly-signed Twilio webhook |
| `fetch_transcript.py` | Reads back Conversational Intelligence transcripts |
| `build_index.py` | Builds the knowledge index |
| `visualize_index.py` | Generates `data/index_explorer.html` for `/explore` (PCA to 2D, precomputed neighbours, no network) |
| `explore_index.py` | CLI over the index: `stats`, `list`, `show`, `similar`, `duplicates`, `outliers`, `search` — only `search` spends a Voyage request |
| `check_retrieval.py` | Spot-checks what the demo agent finds for real prospect questions (paced for the Voyage free tier: 3 rpm) |
| `review_searches.py` | What callers asked and what the index could not answer — `--all`, `--calls` |
| `reset_db.py` | Clears stored call data — `--show`, `--all`, `--before`, `--phone` |
| `bench_latency.py` | Time-to-first-spoken-token per model/effort |
| `bench_turn.py` | Whether tool-schema volume is what causes the pauses between turns |
| `bench_mcp.py` | Why the MCP connector was replaced |
| `bench_fast_mode.py` | Whether fast mode makes a call turn noticeably quicker |

## Verifying without a phone

```bash
python tests/test_relay.py       # offline: protocol, PIN gate, barge-in, transfer
python tests/test_live.py        # real Claude and real MCP
python scripts/preflight.py      # the public ngrok path, including the WebSocket
python scripts/verify_signature.py
```

## Layout

```
app/       main.py — FastAPI app, lifespan, router registration
           static/dialpad.html — the dialer UI
api/       health.py, explore.py, dialpad.py (outbound + status callbacks),
           voice.py (/twiml, /handoff), relay.py (/ws),
           dependencies.py (host normalization, Twilio signature check)
core/      config.py — settings and both system prompts; logging.py
services/  call_session.py  one call: mode, history, PIN gate, Claude loop, barge-in
           salesmsg_mcp.py  persistent MCP session, tool discovery, grouping
           salesmsg_auth.py OAuth token refresh and rotation
           call_tools.py    transfer_to_human / end_call
           demo_tools.py    knowledge search, lead capture, booking
           knowledge.py     the vector index and search
db/        store.py, answering_service.db — callers, conversations, appointments
data/      features.json, knowledge.npz, index_explorer.html
schemas/   call_tools.py, demo_tools.py — JSON tool schemas
scripts/   operator tooling (above)
tests/     test_relay.py (offline), test_live.py (real Claude)
docs/      this file, the C4 diagrams, test_main.http
```

`models/` is empty and unused.

## Notes and limits

- Conversation state is in memory, keyed to the WebSocket. A process restart
  drops in-flight calls. Only the demo dialer persists anything, and only after
  the call ends.
- Unlocking mid-call changes the tool list, invalidating that call's prompt
  cache once. Cheap at one miss per call.
- With write scopes granted, the line can send messages and delete contacts.
  "Confirm before changing anything" is a prompt instruction, not a hard gate.
  For a structural limit, narrow the OAuth scopes.
- No cap on conversation length. For very long calls, add compaction or trim
  old turns.
- On Windows, `zoneinfo` needs `tzdata` (in `requirements.txt`). Without it the
  app falls back to UTC rather than failing the call.
