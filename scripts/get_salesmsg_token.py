"""One-time OAuth flow to get a Salesmsg MCP refresh token.

The MCP server at https://mcp.salesmessage.com is an OAuth 2.0 protected
resource, and its authorization server supports only `authorization_code` and
`refresh_token` — there is no client_credentials grant, so a human has to
approve access once in a browser. After that the app holds the refresh token
and mints short-lived access tokens from it.

This script does the one-time part: registers a client (or reuses yours),
runs authorization_code + PKCE against a local callback, and prints the
credentials to put in .env.

    python get_salesmsg_token.py
    python get_salesmsg_token.py --scope "analytics:read teams:read"
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import base64
import hashlib
import http.server
import json
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

import os

RESOURCE = "https://mcp.salesmessage.com"
DISCOVERY = f"{RESOURCE}/.well-known/oauth-protected-resource"
CALLBACK_HOST, CALLBACK_PORT = "127.0.0.1", 8765
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

# Every scope the MCP resource advertises. The answering service exposes the
# server's full tool set, and a tool whose scope is missing fails at call time
# with an authorization error the caller hears as "something went wrong".
# Narrow this if you want a read-only line — the write scopes are what let the
# agent send messages and edit contacts.
DEFAULT_SCOPE = (
    "analytics:read billing:read "
    "contacts:read contacts:write "
    "conversations:read conversations:write "
    "messages:read messages:write "
    "tags:read tags:write "
    "teams:read"
)

PAGE = """<!doctype html><meta charset=utf-8><title>{title}</title>
<body style="font:16px system-ui;padding:3rem;max-width:34rem">
<h2>{title}</h2><p>{body}</p><p>You can close this tab.</p>"""


def discover() -> tuple[str, str, str, list[str]]:
    """Resolve the authorization server's endpoints from the resource metadata."""
    meta = httpx.get(DISCOVERY, timeout=20).json()
    issuer = meta["authorization_servers"][0]
    scopes = meta.get("scopes_supported", [])

    as_meta = httpx.get(
        f"{issuer}/.well-known/oauth-authorization-server", timeout=20
    ).json()
    grants = as_meta.get("grant_types_supported", [])
    if "authorization_code" not in grants:
        sys.exit(f"Authorization server does not support authorization_code: {grants}")

    return (
        as_meta["authorization_endpoint"],
        as_meta["token_endpoint"],
        as_meta.get("registration_endpoint", ""),
        scopes,
    )


def register_client(registration_endpoint: str, scope: str) -> tuple[str, str | None]:
    """Dynamic client registration (RFC 7591)."""
    if not registration_endpoint:
        sys.exit(
            "No registration endpoint, and no SALESMSG_CLIENT_ID in .env.\n"
            "Create an OAuth app in Salesmessage and set SALESMSG_CLIENT_ID "
            "(and SALESMSG_CLIENT_SECRET if it has one)."
        )

    resp = httpx.post(
        registration_endpoint,
        json={
            "client_name": "ConversationRelay analytics phone agent",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # public client + PKCE
            "scope": scope,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        sys.exit(f"Client registration failed ({resp.status_code}):\n{resp.text[:600]}")

    data = resp.json()
    print(f"  registered client_id {data['client_id']}")
    return data["client_id"], data.get("client_secret")


def wait_for_code(expected_state: str) -> str:
    """Serve one request on the callback URL and return the authorization code."""
    box: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "error" in query:
                box["error"] = query.get("error_description", query["error"])[0]
                page = PAGE.format(title="Authorization failed", body=box["error"])
            elif query.get("state", [None])[0] != expected_state:
                box["error"] = "state mismatch — possible CSRF, discarded"
                page = PAGE.format(title="Authorization failed", body=box["error"])
            elif "code" in query:
                box["code"] = query["code"][0]
                page = PAGE.format(
                    title="Authorized", body="Token exchange is happening in your terminal."
                )
            else:
                page = PAGE.format(title="Unexpected callback", body=self.path)

            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the default access log
            pass

    try:
        server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    except OSError as exc:
        sys.exit(f"Cannot listen on {REDIRECT_URI}: {exc}\nIs something else using port {CALLBACK_PORT}?")

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()

    if "error" in box:
        sys.exit(f"Authorization failed: {box['error']}")
    if "code" not in box:
        sys.exit("Timed out waiting for the browser callback (5 minutes).")
    return box["code"]


def granted_scopes(access_token: str) -> list[str] | None:
    """Read the scopes claim out of the access token.

    The token endpoint doesn't report `scope`, so this is the only way to see
    what was actually granted rather than what was asked for. Decode only — no
    signature check, since we're inspecting our own freshly-issued token.
    """
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    scopes = claims.get("scopes") or claims.get("scope") or claims.get("scp")
    if isinstance(scopes, str):
        return scopes.split()
    return list(scopes) if scopes else None


def write_env(values: dict[str, str], path: Path = Path(".env")) -> None:
    """Set these keys in .env, replacing existing or commented-out lines.

    Writing them beats printing them: credentials that go to a terminal end up
    in scrollback, shell history, and anything capturing the session.
    """
    if not path.exists():
        sys.exit(f"{path} not found — run this from the project directory.")

    pattern = re.compile(r"^\s*#?\s*(" + "|".join(map(re.escape, values)) + r")\s*=")
    lines, seen = path.read_text(encoding="utf-8").splitlines(), set()
    out = []
    for line in lines:
        match = pattern.match(line)
        key = match.group(1) if match else None
        if key and key not in seen:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help=f"default: {DEFAULT_SCOPE!r}")
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write the credentials into .env instead of printing them.",
    )
    parser.add_argument(
        "--new-client",
        action="store_true",
        help="Register a fresh OAuth client instead of reusing SALESMSG_CLIENT_ID. "
        "Needed when the existing client was registered for narrower scopes.",
    )
    parser.add_argument(
        "--show-scopes",
        action="store_true",
        help="Print the scopes on the current token and exit, without re-authorizing.",
    )
    args = parser.parse_args()

    if args.show_scopes:
        import asyncio

        from services.salesmsg_auth import TOKENS

        token = asyncio.run(TOKENS.access_token())
        if not token:
            sys.exit("No usable token — run this script without --show-scopes first.")
        scopes = granted_scopes(token)
        print("Scopes on the current access token:")
        for scope in sorted(scopes or []):
            print(f"  {scope}")
        writes = [s for s in (scopes or []) if s.endswith(":write")]
        print(f"\n{'Write access: ' + ', '.join(writes) if writes else 'Read-only — no :write scopes.'}")
        return 0

    print("1. Discovering endpoints")
    authorize_url, token_url, registration_url, resource_scopes = discover()
    print(f"  authorize {authorize_url}")
    print(f"  token     {token_url}")

    unsupported = [s for s in args.scope.split() if resource_scopes and s not in resource_scopes]
    if unsupported:
        print(f"  note: the MCP resource does not list {unsupported} — requesting anyway")

    print("\n2. Client credentials")
    client_id = None if args.new_client else os.getenv("SALESMSG_CLIENT_ID")
    client_secret = None if args.new_client else os.getenv("SALESMSG_CLIENT_SECRET")
    if client_id:
        print(f"  using SALESMSG_CLIENT_ID from .env ({client_id})")
    else:
        client_id, client_secret = register_client(registration_url, args.scope)

    # PKCE: the verifier never leaves this process, so an intercepted
    # authorization code is not enough to redeem a token.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": args.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    url = f"{authorize_url}?{urllib.parse.urlencode(params)}"

    print(f"\n3. Approve access in your browser (scope: {args.scope})")
    print(f"  {url}\n")
    webbrowser.open(url)
    print(f"  waiting for the callback on {REDIRECT_URI} ...")
    code = wait_for_code(state)
    print("  got the authorization code")

    print("\n4. Exchanging it for tokens")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    resp = httpx.post(token_url, data=payload, timeout=30)
    if resp.status_code >= 400:
        sys.exit(f"Token exchange failed ({resp.status_code}):\n{resp.text[:800]}")

    tokens = resp.json()
    access = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token")
    expires = tokens.get("expires_in")

    print(f"  access_token   {access[:12]}... ({len(access)} chars)")
    print(f"  expires_in     {expires} seconds" if expires else "  expires_in     (not reported)")
    print(f"  refresh_token  {'yes' if refresh else 'NONE RETURNED'}")
    print(f"  scope          {tokens.get('scope', '(not reported)')}")

    print("\n5. Verifying the token against the MCP server")
    probe = httpx.post(
        RESOURCE + "/",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "get_salesmsg_token", "version": "1"},
            },
        },
        timeout=30,
    )
    print(f"  MCP initialize -> HTTP {probe.status_code}")
    if probe.status_code >= 400:
        print(f"  {probe.text[:300]}")

    values = {"SALESMSG_MCP_URL": RESOURCE, "SALESMSG_CLIENT_ID": client_id}
    if client_secret:
        values["SALESMSG_CLIENT_SECRET"] = client_secret
    if refresh:
        values["SALESMSG_REFRESH_TOKEN"] = refresh
    else:
        # Nothing to renew from, so the access token is all there is.
        values["SALESMSG_MCP_TOKEN"] = access

    scopes = granted_scopes(access)
    if scopes:
        print(f"\n  granted scopes ({len(scopes)}):")
        for scope in sorted(scopes):
            print(f"    {scope}")
        missing = [s for s in args.scope.split() if s not in scopes]
        if missing:
            print(f"\n  NOT granted: {', '.join(missing)}")
            print("  The account may lack these, or the OAuth client is registered")
            print("  for a narrower set — try again with --new-client.")

    if args.write_env:
        # A refresh token rotated under the previous grant outranks .env, so a
        # stale state file would silently keep the old (narrower) scopes.
        state = Path(".salesmsg_tokens.json")
        if state.exists():
            state.unlink()
            print(f"\nRemoved {state.name} (superseded by this authorization)")
        write_env(values)
        print("\nWritten to .env:")
        for key in values:
            print(f"  {key}")
    else:
        print("\nAdd to .env:\n")
        for key, value in values.items():
            print(f"{key}={value}")

    if expires and int(expires) < 86400:
        hours = int(expires) / 3600
        print(
            f"\nNote: that access token expires in about {hours:.1f} hour(s), so "
            "SALESMSG_MCP_TOKEN alone will stop working.\nKeep the refresh token "
            "set and let the app mint fresh access tokens from it."
        )
    if not refresh:
        print(
            "\nWarning: no refresh token was issued. Re-run with an offline_access "
            "scope if the server supports it, or you will have to redo this by hand."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
