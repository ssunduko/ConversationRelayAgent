"""Offline smoke test: drives the ConversationRelay protocol end to end with a
scripted stand-in for Claude, so no API key or MCP server is needed.

    python test_relay.py
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib
import json
import os
import tempfile
import time
from types import SimpleNamespace

# Set before importing config: load_dotenv() does not override what's already
# in the environment, so these keep the suite hermetic regardless of .env.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-not-used")
os.environ.setdefault("PUBLIC_HOSTNAME", "example.ngrok-free.app")
os.environ.setdefault("VALIDATE_TWILIO_SIGNATURE", "false")
os.environ.setdefault("WELCOME_GREETING", "Test greeting.")
os.environ.setdefault("INTELLIGENCE_SERVICE_SID", "")  # empty = feature off
os.environ.setdefault("ESCALATION_NUMBER", "+15550000000")
os.environ.setdefault("ACCESS_PIN", "0000")
os.environ.setdefault("MAX_PIN_ATTEMPTS", "3")
os.environ.setdefault("SALESMSG_MCP_URL", "https://mcp.example.com/mcp")
os.environ.setdefault("SALESMSG_MCP_TOKEN", "test-token-not-real")
# Never touch the real caller database: these tests create callers, book
# appointments, and store transcripts.
os.environ.setdefault(
    "DATABASE_PATH", str(Path(tempfile.gettempdir()) / "relay_test_callers.db")
)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


# --- a scripted stand-in for anthropic.AsyncAnthropic -------------------


def text_delta(text: str):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(tool_id: str, name: str, tool_input: dict):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def tool_start(name: str):
    """The content_block_start event that precedes a tool call."""
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", name=name),
    )


class FakeStream:
    def __init__(self, turn, recorder, delay):
        self._turn, self._recorder, self._delay = turn, recorder, delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        import asyncio

        for chunk in self._turn["say"]:
            if self._delay:
                await asyncio.sleep(self._delay)
            self._recorder.append(chunk)
            yield text_delta(chunk)

        # Real streams announce each tool call before it appears in the final
        # message.
        for call in self._turn.get("tools", []):
            yield tool_start(call.name)

    async def get_final_message(self):
        content = [text_block("".join(self._turn["say"]))] if self._turn["say"] else []
        content += self._turn.get("tools", [])
        return SimpleNamespace(
            stop_reason="tool_use" if self._turn.get("tools") else "end_turn",
            content=content,
        )


class FakeMessages:
    def __init__(self, client):
        self._client = client

    def stream(self, **kwargs):
        # Snapshot: the session keeps appending to this same list after the
        # request, which would otherwise rewrite history we already captured.
        self._client.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        script = self._client.script
        turn = script[min(self._client.call_index, len(script) - 1)]
        self._client.call_index += 1
        return FakeStream(turn, self._client.generated, self._client.delay)


class FakeClaude:
    """Mirrors the shape the session uses: client.beta.messages.stream(...)."""

    def __init__(self, script, delay=0.0):
        self.script, self.delay = script, delay
        self.call_index = 0
        self.requests: list[dict] = []
        self.generated: list[str] = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))
        # The end-of-call summary uses the non-beta, non-streaming path. Kept
        # in its own list so `requests` stays "the turns of the conversation".
        self.summaries: list[dict] = []
        self.messages = SimpleNamespace(create=self._summarize)

    async def _summarize(self, **kwargs):
        self.summaries.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Summary of the call.")],
            stop_reason="end_turn",
        )

    async def close(self):
        pass


SETUP = {
    "type": "setup",
    "sessionId": "VX123",
    "callSid": "CA123",
    "from": "+18005550100",
    "to": "+18005550101",
    "customParameters": {"tier": "gold"},
}

SAY = [{"say": ["Sure. ", "One moment."]}] * 8


def drain(ws, timeout=5.0):
    """Collect outbound messages until the turn closes (last=true)."""
    out, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = ws.receive_json()
        out.append(msg)
        if msg.get("type") == "text" and msg.get("last"):
            return out
        if msg.get("type") == "end":
            return out
    raise AssertionError(f"turn never closed; got {out}")


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(1)


def _tool(name: str) -> dict:
    return {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}}


# Deliberately spans both sides of the demo allowlist: send_sms is offered on a
# demo call, the analytics tools are not.
ANALYTICS_TOOLS = [
    _tool("salesmsg_get_messages_analytics"),
    _tool("salesmsg_resolve_analytics_filters"),
    _tool("salesmsg_send_sms"),
    _tool("salesmsg_create_contact"),
]


class FakeMCP:
    """Stands in for the persistent MCP session."""

    def __init__(self, definitions=ANALYTICS_TOOLS, result=("42 messages", False), groups=("contacts", "messages")):
        self._definitions, self._result, self._groups = definitions, result, list(groups)
        self.calls: list[tuple[str, dict]] = []

    @property
    def configured(self):
        return True

    async def tool_definitions(self):
        return list(self._definitions)

    async def service_groups(self):
        return list(self._groups) if self._definitions else []

    def handles(self, name):
        return name.startswith("salesmsg_")

    async def call(self, name, args):
        self.calls.append((name, args))
        return self._result

    async def aclose(self):
        pass


def install_mcp(fake: FakeMCP) -> FakeMCP:
    from services import call_session as session_mod
    from services import salesmsg_mcp

    salesmsg_mcp.MCP = fake
    session_mod.MCP = fake
    return fake


# SETTINGS is imported by value into several modules, so patching one of them
# leaves the others on the old config — which silently breaks tests that think
# they changed something.
def _settings_modules():
    import api.dependencies
    import api.health
    import api.voice
    import app.main
    import services.call_session
    import services.salesmsg_auth
    import services.salesmsg_mcp

    return [
        api.dependencies,
        api.health,
        api.voice,
        app.main,
        services.call_session,
        services.salesmsg_auth,
        services.salesmsg_mcp,
    ]


@contextlib.contextmanager
def patched_settings(**overrides):
    """Swap SETTINGS everywhere it was imported, then put it all back."""
    from core.config import Settings

    replacement = Settings(**overrides)
    originals = [(m, m.SETTINGS) for m in _settings_modules() if hasattr(m, "SETTINGS")]
    for module, _ in originals:
        module.SETTINGS = replacement
    try:
        yield replacement
    finally:
        for module, original in originals:
            module.SETTINGS = original


def analytics_names(request: dict) -> set[str]:
    return {t["name"] for t in request["tools"] if t.get("name", "").startswith("salesmsg_")}


def has_mcp(request: dict) -> bool:
    """Analytics reachable on this request?"""
    return bool(analytics_names(request))


def enter_pin(ws, pin: str) -> None:
    for digit in pin:
        ws.send_json({"type": "dtmf", "digit": digit})


# --- tests --------------------------------------------------------------


def test_token_refresh() -> None:
    """Access-token lifecycle: cache, expiry, rotation, failure."""
    print("Salesmsg token refresh")
    import asyncio

    from services.salesmsg_auth import SalesmsgTokens

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeHTTP:
        def __init__(self, responses):
            self.responses, self.calls = list(responses), []

        async def post(self, url, data=None):
            self.calls.append(data)
            return self.responses.pop(0) if self.responses else FakeResponse(500, {})

        async def aclose(self):
            pass

    class Clock:
        """Lets a test skip forward instead of sleeping."""

        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

    def build(responses):
        http, clock = FakeHTTP(responses), Clock()
        return (
            SalesmsgTokens(
                token_url="https://token.example/oauth/token",
                client_id="cid",
                client_secret=None,
                refresh_token="refresh-1",
                static_token=None,
                state_file=None,
                http=http,
                clock=clock,
            ),
            http,
            clock,
        )

    # A long-lived token is minted once and reused.
    tokens, http, clock = build([FakeResponse(200, {"access_token": "at-1", "expires_in": 3600})])
    first = asyncio.run(tokens.access_token())
    second = asyncio.run(tokens.access_token())
    check("mints an access token", first == "at-1", str(first))
    check("reuses it while valid", second == "at-1" and len(http.calls) == 1, f"{len(http.calls)} calls")
    check("sends the refresh grant", http.calls[0]["grant_type"] == "refresh_token")

    # Inside the leeway window it renews rather than risking a mid-request lapse.
    tokens, http, clock = build([
        FakeResponse(200, {"access_token": "at-1", "expires_in": 3600}),
        FakeResponse(200, {"access_token": "at-2", "expires_in": 3600}),
    ])
    asyncio.run(tokens.access_token())
    clock.now += 3500  # 100s left, inside the 120s leeway
    check("renews a near-expiry token", asyncio.run(tokens.access_token()) == "at-2")

    # Rotation: the new refresh token must be the one used next.
    tokens, http, clock = build([
        FakeResponse(200, {"access_token": "at-1", "expires_in": 3600, "refresh_token": "refresh-2"}),
        FakeResponse(200, {"access_token": "at-2", "expires_in": 3600}),
    ])
    asyncio.run(tokens.access_token())
    clock.now += 3600
    asyncio.run(tokens.access_token())
    check("uses the rotated refresh token", http.calls[1]["refresh_token"] == "refresh-2", str(http.calls[1]))

    # A rejected refresh must not take the call down.
    tokens, http, clock = build([FakeResponse(400, {"error": "invalid_grant"})])
    check("returns None when refresh fails", asyncio.run(tokens.access_token()) is None)

    # A stale token beats no token if the server is briefly unavailable.
    tokens, http, clock = build([
        FakeResponse(200, {"access_token": "at-1", "expires_in": 3600}),
        FakeResponse(503, {"error": "unavailable"}),
    ])
    asyncio.run(tokens.access_token())
    clock.now += 3600
    check("falls back to the last good token", asyncio.run(tokens.access_token()) == "at-1")

    # With no refresh credentials, a static token is passed through untouched.
    static = SalesmsgTokens(
        token_url="https://token.example/oauth/token",
        client_id=None,
        client_secret=None,
        refresh_token=None,
        static_token="static-token",
        state_file=None,
    )
    check("passes a static token through", asyncio.run(static.access_token()) == "static-token")
    check("reports it as non-renewing", "static" in static.describe(), static.describe())


def test_mcp_tool_conversion() -> None:
    """Convert a real mcp.types.Tool, not a stand-in.

    The fakes elsewhere skip this conversion, which is exactly how a wrong
    field name ('inputSchema' vs 'input_schema') reached a live call and
    silently left the agent with no analytics tools.
    """
    print("MCP tool conversion")
    from mcp.types import Tool

    from services.salesmsg_mcp import to_anthropic_tool

    schema = {
        "type": "object",
        "properties": {"from_date": {"type": "string"}},
        "required": ["from_date"],
    }
    converted = to_anthropic_tool(
        Tool(name="salesmsg_get_messages_analytics", description="Message volume.", input_schema=schema)
    )
    check("keeps the name", converted["name"] == "salesmsg_get_messages_analytics")
    check("keeps the description", converted["description"] == "Message volume.")
    check("carries the real input schema", converted["input_schema"] == schema, str(converted["input_schema"]))
    check(
        "shape matches an Anthropic tool definition",
        set(converted) == {"name", "description", "input_schema"},
        str(set(converted)),
    )

    # A tool with no schema must still be usable rather than malformed.
    bare = to_anthropic_tool(Tool(name="salesmsg_server_info", input_schema={}))
    check("tolerates an empty schema", bare["input_schema"] == {"type": "object", "properties": {}}, str(bare))


def test_host_normalization() -> None:
    print("PUBLIC_HOSTNAME normalization")
    from api.dependencies import normalize_host

    # Pasting the full ngrok URL is the obvious thing to do, and produced
    # 'wss://https://host/ws' — which only fails once Twilio dials it.
    cases = [
        ("https://abc.ngrok-free.dev", "abc.ngrok-free.dev"),
        ("http://abc.ngrok-free.dev/", "abc.ngrok-free.dev"),
        ("wss://abc.ngrok-free.dev/ws", "abc.ngrok-free.dev"),
        ("abc.ngrok-free.dev", "abc.ngrok-free.dev"),
        ("  abc.ngrok-free.dev/  ", "abc.ngrok-free.dev"),
        ("localhost:8000", "localhost:8000"),
        ("https://abc.example.com:8443/path", "abc.example.com:8443"),
    ]
    for raw, want in cases:
        check(f"{raw!r} -> {want!r}", normalize_host(raw) == want)


def test_twiml(client: TestClient) -> None:
    print("TwiML endpoint")
    body = client.post("/twiml", data={"CallSid": "CA123"}).text
    check("returns ConversationRelay TwiML", "<ConversationRelay" in body)
    check("points at the wss endpoint", 'url="wss://example.ngrok-free.app/ws"' in body, body)
    check("enables DTMF for PIN entry", 'dtmfDetection="true"' in body)
    check("omits intelligenceService when unset", "intelligenceService" not in body)


def test_greeting_is_not_cut_off(client: TestClient) -> None:
    """Twilio's defaults let a 'hello' or a keypress kill the welcome greeting."""
    print("Greeting protection")
    body = client.post("/twiml", data={"CallSid": "CA123"}).text

    check(
        "speech and noise cannot interrupt the greeting",
        'welcomeGreetingInterruptible="dtmf"' in body,
        body,
    )
    check("agent speech stays interruptible for barge-in", 'interruptible="any"' in body, body)
    check(
        "keypad input is reported while the agent speaks",
        'reportInputDuringAgentSpeech="dtmf"' in body,
        body,
    )
    check("backchannel does not interrupt", 'ignoreBackchannel="true"' in body, body)
    check("interruptSensitivity omitted unless configured", "interruptSensitivity" not in body)

    from api.voice import _interrupt_mode

    # Older configs (and .env files) used booleans for these.
    for raw, expected in [
        ("true", "any"), ("false", "none"), ("TRUE", "any"),
        ("dtmf", "dtmf"), ("speech", "speech"), ("none", "none"), ("any", "any"),
        ("nonsense", "any"),  # falls back rather than emitting invalid TwiML
    ]:
        check(f"interrupt mode {raw!r} -> {expected!r}", _interrupt_mode(raw, "X") == expected)


def test_intelligence_service() -> None:
    print("Conversational Intelligence attribute")
    with patched_settings(
        public_hostname="example.ngrok-free.app",
        validate_signature=False,
        intelligence_service="GA00000000000000000000000000000000",
    ):
        with TestClient(main.app) as c:
            body = c.post("/twiml", data={"CallSid": "CA123"}).text
    check(
        "passes the service SID to Twilio",
        'intelligenceService="GA00000000000000000000000000000000"' in body,
        body,
    )
    check("still a valid ConversationRelay", "<ConversationRelay" in body and body.endswith("</Response>"))


def test_twilio_signature_validation() -> None:
    """Accept a signature Twilio's own validator produced.

    A real Twilio POST was 403ing because blank-valued params (ForwardedFrom,
    CallerName, ...) were dropped before validation. Twilio signs every param,
    so this uses their RequestValidator as the oracle rather than our idea of
    what a request looks like.
    """
    print("Twilio signature validation")
    from twilio.request_validator import RequestValidator

    token = "test_auth_token_0123456789abcdef"
    host = "example.ngrok-free.app"
    url = f"https://{host}/twiml"

    # Shaped like a real voice webhook: several params are legitimately empty.
    payload = {
        "CallSid": "CA00000000000000000000000000000000",
        "AccountSid": "AC00000000000000000000000000000000",
        "From": "+15555550133",
        "To": "+15555550111",
        "CallStatus": "ringing",
        "Direction": "inbound",
        "ForwardedFrom": "",
        "CallerName": "",
        "ParentCallSid": "",
    }
    signature = RequestValidator(token).compute_signature(url, payload)

    with patched_settings(
        public_hostname=host, validate_signature=True, twilio_auth_token=token
    ):
        with TestClient(main.app) as signed_client:
            good = signed_client.post(
                "/twiml", data=payload, headers={"X-Twilio-Signature": signature}
            )
            check("accepts a genuine signed request", good.status_code == 200, f"got {good.status_code}")
            check("still returns TwiML", "<ConversationRelay" in good.text)

            tampered = signed_client.post(
                "/twiml",
                data={**payload, "From": "+19995550000"},
                headers={"X-Twilio-Signature": signature},
            )
            check("rejects a tampered body", tampered.status_code == 403, f"got {tampered.status_code}")

            unsigned = signed_client.post("/twiml", data=payload)
            check("rejects an unsigned request", unsigned.status_code == 403, f"got {unsigned.status_code}")


def test_phone_normalization() -> None:
    print("Dialpad number handling")
    from fastapi import HTTPException

    from api.dialpad import to_e164

    for raw, expected in [
        ("+1 555 555 0111", "+15555550111"),
        ("(555) 555-0111", "+15555550111"),   # bare US, 10 digits
        ("15555550111", "+15555550111"),
        ("+44 20 7946 0958", "+442079460958"),
        ("  +1-555-555-0111  ", "+15555550111"),
    ]:
        check(f"{raw!r} -> {expected}", to_e164(raw) == expected, f"got {to_e164(raw)}")

    for bad in ("", "abc", "12"):
        try:
            to_e164(bad)
            check(f"rejects {bad!r}", False, "accepted it")
        except HTTPException:
            check(f"rejects {bad!r}", True)


def test_voicemail_is_not_talked_to(client: TestClient) -> None:
    """Machine detection must hang up, not hold a conversation with a greeting."""
    print("Answering-machine detection")
    for answered_by, should_hang_up in [
        ("machine_start", True),
        ("machine_end_beep", True),
        ("fax", True),
        ("human", False),
        ("", False),  # detection disabled — treat as human
    ]:
        body = client.post(
            "/outbound-twiml",
            data={"CallSid": "CA1", "AnsweredBy": answered_by} if answered_by else {"CallSid": "CA1"},
        ).text
        hung_up = "<Hangup" in body
        check(
            f"AnsweredBy={answered_by or '(none)'} -> {'hang up' if should_hang_up else 'connect agent'}",
            hung_up == should_hang_up,
            body[:120],
        )
    # A human answer still gets the demo persona.
    body = client.post("/outbound-twiml", data={"CallSid": "CA1", "AnsweredBy": "human"}).text
    check("human answer starts the demo relay", 'name="mode" value="demo"' in body, body[:200])


def test_hangup_on_disconnect(client: TestClient) -> None:
    """A dropped socket must not leave a live call; a transfer must survive."""
    print("Hang-up on disconnect")
    import asyncio

    from services import call_session as session_mod

    completed: list[str] = []

    async def fake_ensure(self):
        # Stand in for the Twilio REST call, exercising the real guard clauses.
        if self._deliberate_end:
            return
        completed.append(self.call_sid)

    original = session_mod.CallSession._ensure_call_ended
    session_mod.CallSession._ensure_call_ended = fake_ensure
    try:
        install_mcp(FakeMCP())

        # 1. Caller hangs up mid-call: nothing deliberate, so we make sure.
        main.app.state.claude = FakeClaude(script=SAY)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({**SETUP, "callSid": "CAdrop"})
            ws.send_json({"type": "prompt", "voicePrompt": "Hello?", "last": True})
            drain(ws)
        check("ends the leg when the socket drops", "CAdrop" in completed, str(completed))

        # 2. Agent transfers: Twilio must be left to dial the human.
        completed.clear()
        main.app.state.claude = FakeClaude(script=[
            {"say": ["Putting you through now."],
             "tools": [tool_block("t1", "transfer_to_human", {"reason": "asked for a person"})]},
            {"say": []},
        ])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({**SETUP, "callSid": "CAxfer"})
            ws.send_json({"type": "prompt", "voicePrompt": "Get me a human.", "last": True})
            drain(ws)
            ws.receive_json()  # the end message
        check("leaves a transfer alone", "CAxfer" not in completed, str(completed))

        # 3. Agent says goodbye: Twilio's own hangup handles it.
        completed.clear()
        main.app.state.claude = FakeClaude(script=[
            {"say": ["Goodbye!"], "tools": [tool_block("t2", "end_call", {"reason": "done"})]},
            {"say": []},
        ])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({**SETUP, "callSid": "CAbye"})
            ws.send_json({"type": "prompt", "voicePrompt": "Bye.", "last": True})
            drain(ws)
            ws.receive_json()
        check("leaves a normal goodbye alone", "CAbye" not in completed, str(completed))
    finally:
        session_mod.CallSession._ensure_call_ended = original


def test_outbound_contact_number(client: TestClient) -> None:
    """On an outbound call, 'from' is us and 'to' is them.

    The agent texted the number it was calling *from* because the call context
    labelled that "the caller". This checks the context names the right party.
    """
    print("Outbound number orientation")
    install_mcp(FakeMCP())
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json({
            **SETUP, "callSid": "CAdir", "direction": "outbound-api",
            "from": "+15555550111",   # ours
            "to": "+15555550122",     # the prospect
            "customParameters": {"mode": "demo"},
        })
        ws.send_json({"type": "prompt", "voicePrompt": "Sure.", "last": True})
        drain(ws)

    context = fake.requests[-1]["system"][1]["text"]
    check("names the prospect as who you're speaking to", "+15555550122" in context, context[:400])
    check(
        "says messages go to the prospect",
        "+15555550122" in context.split("must go to")[1][:20] if "must go to" in context else False,
        context[:400],
    )
    check(
        "warns off our own number",
        "Never text or create a contact for that number" in context,
        context[:400],
    )

    # Inbound is the mirror image: 'from' is the caller.
    fake2 = FakeClaude(script=SAY)
    main.app.state.claude = fake2
    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)  # from=+18005550100, to=+18005550101
        ws.send_json({"type": "prompt", "voicePrompt": "Hello?", "last": True})
        drain(ws)
    inbound = fake2.requests[-1]["system"][1]["text"]
    check("inbound names the caller correctly", "THE CALLER is on +18005550100" in inbound, inbound[:300])


def test_demo_mode_isolation(client: TestClient) -> None:
    """A demo prospect gets product tools and never touches the account."""
    print("Demo mode")
    install_mcp(FakeMCP())
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    demo_setup = {
        **SETUP,
        "callSid": "CAdemo",
        "direction": "outbound-api",
        "from": "+15555550111",     # us
        "to": "+16505550123",       # the prospect
        "customParameters": {"mode": "demo"},
    }

    with client.websocket_connect("/ws") as ws:
        ws.send_json(demo_setup)
        ws.send_json({"type": "prompt", "voicePrompt": "Sure, what's this about?", "last": True})
        drain(ws)

    req = fake.requests[-1]
    names = {t["name"] for t in req["tools"]}

    check("no PIN needed on a demo call", bool(fake.requests), "no request was made")
    check("offers the product knowledge search", "search_product_knowledge" in names, str(names))
    check("offers lead capture and booking", {"save_caller_details", "book_demo"} <= names, str(names))
    # A prospect who says "text me the details" needs the agent to be able to.
    check("can text and save the caller", {"salesmsg_send_sms", "salesmsg_create_contact"} <= names, str(names))
    # Every extra schema is ~1.8s of silence across the full set; a demo call
    # has no use for reporting tools.
    check(
        "analytics tools withheld on a demo call",
        not any("analytics" in n for n in names),
        str(names),
    )
    check("keeps transfer and hang-up", {"transfer_to_human", "end_call"} <= names, str(names))
    check(
        "uses the demo persona, not the answering service prompt",
        "calling on behalf of Salesmessage" in req["system"][0]["text"],
        req["system"][0]["text"][:120],
    )
    check("no locked notice on a demo call", not any("access PIN" in b["text"] for b in req["system"]))


def test_demo_memory_and_booking() -> None:
    """Details and bookings persist, and come back on the next call."""
    print("Demo memory")
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from db.store import Store, describe_caller

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(_Path(tmp) / "test.db")
        phone = "+16505550123"

        async def scenario():
            await store.upsert_caller(phone)
            convo = await store.start_conversation(phone, "CA1", "outbound-api", "demo")
            await store.upsert_caller(
                phone, first_name="Dana", last_name="Reed", email="dana@example.com",
                experience="uses HubSpot and a desk phone",
            )
            await store.save_appointment(
                phone, first_name="Dana", last_name="Reed", email="dana@example.com",
                requested_time="Tuesday the 18th at 2pm Eastern", call_sid="CA1",
            )
            await store.finish_conversation(
                convo, [{"role": "agent", "text": "hi"}], "Agreed to a demo Tuesday."
            )
            return await store.get_caller(phone)

        caller = asyncio.run(scenario())

    check("remembers the name", caller["first_name"] == "Dana", str(caller))
    check("remembers the email", caller["email"] == "dana@example.com")
    check("remembers what tools they use", "HubSpot" in (caller["experience"] or ""))
    check("keeps the appointment", len(caller["appointments"]) == 1, str(caller["appointments"]))
    check("keeps the call summary", len(caller["previous_calls"]) == 1)
    check("keeps the call summary", len(caller["previous_calls"]) == 1)

    briefing = describe_caller(caller)
    check("briefs the agent by name", "Dana Reed" in briefing, briefing)
    check("tells the agent not to re-ask", "Do not ask for anything already on file" in briefing)
    check("mentions the existing appointment", "Tuesday the 18th" in briefing, briefing)


def test_searches_are_recorded() -> None:
    """Knowledge lookups land on the conversation record, misses included."""
    print("Search logging")
    import asyncio
    import json as _json
    import sqlite3
    import tempfile
    from pathlib import Path as _Path

    from db.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        db_path = _Path(tmp) / "searches.db"
        store = Store(db_path)

        async def scenario():
            convo = await store.start_conversation("+16505550123", "CA9", "outbound-api", "demo")
            await store.finish_conversation(
                convo,
                [{"role": "caller", "text": "does it do MMS?"}],
                "Asked about MMS.",
                [
                    {"query": "MMS picture messaging", "hits": 3, "best_score": 0.61,
                     "top": "MMS Support", "source": "product features", "answered": True,
                     "at": "2026-08-16T00:00:00+00:00"},
                    {"query": "does it work with SAP", "hits": 0, "best_score": None,
                     "top": None, "source": None, "answered": False,
                     "at": "2026-08-16T00:00:01+00:00"},
                ],
            )

        asyncio.run(scenario())

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT searches FROM conversations WHERE call_sid = 'CA9'").fetchone()
        conn.close()

    searches = _json.loads(row["searches"])
    check("both searches stored", len(searches) == 2, str(searches))
    check("keeps the query the agent wrote", searches[0]["query"] == "MMS picture messaging")
    check("keeps the score", searches[0]["best_score"] == 0.61)
    check("marks the miss", searches[1]["answered"] is False, str(searches[1]))
    check("a miss has no score", searches[1]["best_score"] is None)


def test_schema_migration() -> None:
    """An existing database gains the searches column rather than breaking."""
    print("Schema migration")
    import sqlite3
    import tempfile
    from pathlib import Path as _Path

    from db.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        db_path = _Path(tmp) / "old.db"
        # A database created before searches existed.
        conn = sqlite3.connect(db_path)
        conn.executescript(
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " phone TEXT NOT NULL, call_sid TEXT, direction TEXT, mode TEXT,"
            " started_at TEXT NOT NULL, ended_at TEXT, summary TEXT, transcript TEXT);"
        )
        conn.execute(
            "INSERT INTO conversations (phone, started_at) VALUES ('+15550001111', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        Store(db_path)  # opening it should migrate in place

        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        rows = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()

    check("searches column added", "searches" in cols, str(sorted(cols)))
    check("existing rows survive", rows == 1, f"{rows} rows")


def test_service_groups() -> None:
    """Groups come from the server's real tool names, not a hardcoded list."""
    print("Service grouping")
    from services.salesmsg_mcp import group_for

    cases = [
        # The ordering traps: each of these matches more than one keyword.
        ("salesmsg_get_contacts_analytics", "reporting and analytics"),
        ("salesmsg_get_messages_analytics", "reporting and analytics"),
        ("salesmsg_get_credit_usage_by_source", "reporting and analytics"),
        ("salesmsg_get_contact_conversation", "conversations"),
        ("salesmsg_add_tag_to_contact", "tags"),
        ("salesmsg_remove_tag_from_contact", "tags"),
        ("salesmsg_send_sms", "messages"),
        ("salesmsg_schedule_message", "messages"),
        ("salesmsg_create_contact", "contacts"),
        ("salesmsg_search_contacts", "contacts"),
        ("salesmsg_get_all_inboxes", "inboxes and team"),
        ("salesmsg_server_info", None),
    ]
    for name, expected in cases:
        check(f"{name} -> {expected}", group_for(name) == expected, f"got {group_for(name)}")


def test_handoff_dials_a_human() -> None:
    print("Escalation transfers to a real number")
    with patched_settings(
        public_hostname="example.ngrok-free.app",
        validate_signature=False,
        escalation_number="+15555550122",
    ):
        with TestClient(main.app) as c:
            twiml = c.post("/twiml", data={"CallSid": "CA1"}).text
            check(
                "TwiML gives Twilio somewhere to hand control back",
                'action="https://example.ngrok-free.app/handoff"' in twiml,
                twiml,
            )

            transfer = c.post("/handoff", data={
                "CallSid": "CA1",
                "SessionStatus": "ended",
                "To": "+15555550111",
                "HandoffData": json.dumps({"reasonCode": "live-agent-handoff", "reason": "wants a person"}),
            }).text
            check("dials the escalation number", "<Dial" in transfer and "+15555550122" in transfer, transfer)
            check("presents the dialled number as caller ID", 'callerId="+15555550111"' in transfer, transfer)

            # A normal goodbye must not dial anyone.
            done = c.post("/handoff", data={
                "CallSid": "CA1",
                "SessionStatus": "ended",
                "HandoffData": json.dumps({"reasonCode": "completed", "reason": "said goodbye"}),
            }).text
            check("a completed call just hangs up", "<Hangup" in done and "<Dial" not in done, done)

            # Twilio can call the action URL with no handoff data at all.
            bare = c.post("/handoff", data={"CallSid": "CA1", "SessionStatus": "failed"}).text
            check("survives a missing HandoffData", "<Hangup" in bare, bare)


def test_locked_until_pin(client: TestClient) -> None:
    print("Analytics are withheld before the PIN")
    install_mcp(FakeMCP())
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        ws.send_json({"type": "prompt", "voicePrompt": "How many messages went out last week?", "last": True})
        drain(ws)

    req = fake.requests[0]
    check("no analytics tools offered", not has_mcp(req))
    check("only call-control tools present", {t["name"] for t in req["tools"]} == {"transfer_to_human", "end_call"})
    check("model is told why it cannot look anything up", any("access PIN" in b["text"] for b in req["system"]))
    # The connector is gone: no server-side MCP means no per-request handshake.
    check("no MCP connector parameters", "mcp_servers" not in req and "betas" not in req)


def test_pin_unlocks_analytics(client: TestClient) -> None:
    print("Correct PIN offers the analytics tools")
    install_mcp(FakeMCP())
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        ws.send_json({"type": "prompt", "voicePrompt": "Hi there.", "last": True})
        drain(ws)
        locked = fake.requests[-1]

        enter_pin(ws, "0000")
        drain(ws)
        unlocked = fake.requests[-1]

    check("locked before entry", not has_mcp(locked))
    check("analytics tools offered after entry", has_mcp(unlocked), str(analytics_names(unlocked)))
    check(
        "offers exactly the allowlisted tools",
        analytics_names(unlocked) == {t["name"] for t in ANALYTICS_TOOLS},
        str(analytics_names(unlocked)),
    )
    check("keeps the call-control tools", {"transfer_to_human", "end_call"} <= {t["name"] for t in unlocked["tools"]})
    check("still no MCP connector parameters", "mcp_servers" not in unlocked and "betas" not in unlocked)
    check("drops the locked notice", not any("access PIN" in b["text"] for b in unlocked["system"]))
    check(
        "tells the agent which areas to announce",
        any("contacts, messages" in b["text"] for b in unlocked["system"]),
        unlocked["system"][1]["text"][-200:],
    )


def test_analytics_tool_routing(client: TestClient) -> None:
    print("Analytics calls route to the MCP session")
    mcp = install_mcp(FakeMCP(result=("You sent 45 messages.", False)))
    fake = FakeClaude(
        script=[
            {
                "say": ["Let me pull that up. "],
                "tools": [tool_block("toolu_9", "salesmsg_get_messages_analytics",
                                     {"from_date": "2026-08-03", "to_date": "2026-08-09"})],
            },
            {"say": ["You sent 45 messages last week."]},
        ]
    )
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        enter_pin(ws, "0000")
        drain(ws)
        ws.send_json({"type": "prompt", "voicePrompt": "How many messages last week?", "last": True})
        drain(ws)

    check("called the MCP tool", [name for name, _ in mcp.calls] == ["salesmsg_get_messages_analytics"], str(mcp.calls))
    check("passed the date range through", mcp.calls[0][1]["from_date"] == "2026-08-03", str(mcp.calls[0][1]))

    results = [
        block
        for msg in fake.requests[-1]["messages"]
        if isinstance(msg["content"], list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    check("fed the result back to Claude", len(results) == 1 and "45 messages" in results[0]["content"], str(results))


def test_analytics_outage_is_survivable(client: TestClient) -> None:
    print("An unreachable MCP server doesn't kill the call")
    install_mcp(FakeMCP(definitions=[]))  # server down: no tools advertised
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        enter_pin(ws, "0000")
        out = drain(ws)

    check("the call still runs", any(m["type"] == "text" for m in out))
    check("no analytics tools offered", not has_mcp(fake.requests[-1]))


def test_wrong_pin_stays_locked(client: TestClient) -> None:
    print("Wrong PIN keeps analytics locked")
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        enter_pin(ws, "1111")
        drain(ws)

    req = fake.requests[-1]
    check("still no MCP server", not has_mcp(req))
    check(
        "model is told the PIN was wrong",
        "incorrect PIN" in req["messages"][-1]["content"],
        str(req["messages"][-1]),
    )


def test_lockout_after_repeated_failures(client: TestClient) -> None:
    print("Repeated wrong PINs end the call")
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        enter_pin(ws, "1111")
        drain(ws)
        enter_pin(ws, "2222")
        drain(ws)
        enter_pin(ws, "3333")  # third strike — hangs up without a model turn
        out = drain(ws)
        end = out[-1] if out[-1]["type"] == "end" else ws.receive_json()

    check("hangs up", end["type"] == "end", str(end))
    check("records why", "PIN" in end["handoffData"], end["handoffData"])
    check("never attached MCP", not any(has_mcp(r) for r in fake.requests))


def test_call_context(client: TestClient) -> None:
    print("Call context")
    fake = FakeClaude(script=SAY)
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        ws.send_json({"type": "prompt", "voicePrompt": "Hello?", "last": True})
        drain(ws)

    context = fake.requests[0]["system"][1]["text"]
    # Without a current date the model cannot turn "last week" into the
    # YYYY-MM-DD range the analytics tools require.
    check("includes the current date", "Current date and time:" in context, context)
    check("names the reporting timezone", "Report figures in this timezone" in context)
    check("includes the caller number", "+18005550100" in context)
    check("includes custom parameters", "tier: gold" in context)
    check("caches the stable system prompt", "cache_control" in fake.requests[0]["system"][0])


def test_end_call(client: TestClient) -> None:
    print("Hang-up tool")
    fake = FakeClaude(
        script=[
            {
                "say": ["Happy to help. Goodbye!"],
                "tools": [tool_block("toolu_2", "end_call", {"reason": "Caller said goodbye"})],
            },
            {"say": []},
        ]
    )
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        ws.send_json({"type": "prompt", "voicePrompt": "That's all, thanks. Bye!", "last": True})
        out = drain(ws)
        end = ws.receive_json()

    check("says goodbye before hanging up", any(m["type"] == "text" and m["last"] for m in out))
    check("sends an end message", end["type"] == "end", str(end))
    check("includes handoff data", "Caller said goodbye" in end["handoffData"])


def test_barge_in(client: TestClient) -> None:
    print("Barge-in")
    fake = FakeClaude(
        script=[{"say": [f"word{i} " for i in range(60)]}, {"say": ["Sure, going faster."]}],
        delay=0.02,
    )
    main.app.state.claude = fake

    with client.websocket_connect("/ws") as ws:
        ws.send_json(SETUP)
        ws.send_json({"type": "prompt", "voicePrompt": "Tell me about last month.", "last": True})
        ws.receive_json()  # wait until the agent is actually speaking
        time.sleep(0.15)
        ws.send_json({
            "type": "interrupt",
            "utteranceUntilInterrupt": "word0 word1 word2",
            "durationUntilInterruptMs": 150,
        })
        ws.send_json({"type": "prompt", "voicePrompt": "Just the total, please.", "last": True})
        drain(ws)

    check("cancelled the first generation", len(fake.generated) < 60, f"{len(fake.generated)} tokens")
    history = fake.requests[-1]["messages"]
    assistant = [m for m in history if m["role"] == "assistant"]
    check("recorded only what the caller heard", assistant and assistant[-1]["content"] == "word0 word1 word2", str(assistant))
    check("history ends with the new caller turn", history[-1]["role"] == "user")


if __name__ == "__main__":
    with TestClient(main.app) as client:
        test_token_refresh()
        test_mcp_tool_conversion()
        test_host_normalization()
        test_twiml(client)
        test_greeting_is_not_cut_off(client)
        test_intelligence_service()
        test_twilio_signature_validation()
        test_phone_normalization()
        test_voicemail_is_not_talked_to(client)
        test_hangup_on_disconnect(client)
        test_outbound_contact_number(client)
        test_demo_mode_isolation(client)
        test_demo_memory_and_booking()
        test_searches_are_recorded()
        test_schema_migration()
        test_service_groups()
        test_handoff_dials_a_human()
        test_locked_until_pin(client)
        test_pin_unlocks_analytics(client)
        test_analytics_tool_routing(client)
        test_analytics_outage_is_survivable(client)
        test_wrong_pin_stays_locked(client)
        test_lockout_after_repeated_failures(client)
        test_call_context(client)
        test_end_call(client)
        test_barge_in(client)
    print("\nAll checks passed.")
