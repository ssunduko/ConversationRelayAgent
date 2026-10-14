"""Keeps a usable Salesmsg MCP access token on hand.

The authorization server issues short-lived access tokens and offers no
client_credentials grant, so the only way to stay authenticated without a human
in the loop is to hold a refresh token and exchange it as needed.

Two wrinkles this handles:

Rotation. If the server returns a new refresh token, the old one may stop
working. Losing it means redoing the browser flow, so a rotated token is written
to a state file immediately and that file wins over `.env` on the next start.

Failure. If a refresh fails we return None rather than raising. The caller then
runs the turn without analytics tools attached, so the caller hears "I can't
reach analytics right now" instead of the call dying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from core.config import PROJECT_ROOT, SETTINGS

log = logging.getLogger("relay.auth")

# Refresh this far ahead of expiry so a token can't lapse mid-request.
EXPIRY_LEEWAY_SECONDS = 120
# Used when the server doesn't say how long the token lasts.
ASSUMED_LIFETIME_SECONDS = 3600

# Repo root, not next to this module: the token script and .env live there, and
# a state file hidden inside services/ is one nobody would think to look for.
STATE_FILE = PROJECT_ROOT / ".salesmsg_tokens.json"


class SalesmsgTokens:
    def __init__(
        self,
        *,
        token_url: str,
        client_id: str | None,
        client_secret: str | None,
        refresh_token: str | None,
        static_token: str | None,
        state_file: Path | None = STATE_FILE,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._static_token = static_token
        self._state_file = state_file
        self._http = http

        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

        if state_file is not None:
            self._load_state()

    @classmethod
    def from_settings(cls) -> "SalesmsgTokens":
        return cls(
            token_url=SETTINGS.salesmsg_token_url,
            client_id=SETTINGS.salesmsg_client_id,
            client_secret=SETTINGS.salesmsg_client_secret,
            refresh_token=SETTINGS.salesmsg_refresh_token,
            static_token=SETTINGS.salesmsg_mcp_token,
        )

    # --- state ----------------------------------------------------------

    def _load_state(self) -> None:
        """A rotated refresh token outranks the one in .env, which is stale."""
        try:
            saved = json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self._state_file.name, exc)
            return

        if saved.get("refresh_token"):
            self._refresh_token = saved["refresh_token"]
            log.info("using rotated refresh token from %s", self._state_file.name)

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.write_text(json.dumps({"refresh_token": self._refresh_token}))
            # Best effort: not every filesystem honours this.
            try:
                self._state_file.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            log.error(
                "could not persist the rotated refresh token to %s: %s — "
                "you may have to re-run get_salesmsg_token.py after a restart",
                self._state_file.name,
                exc,
            )

    # --- tokens ---------------------------------------------------------

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id)

    def describe(self) -> str:
        if self.can_refresh:
            return "refresh token (auto-renewing)"
        if self._static_token:
            return "static access token (will expire; no refresh configured)"
        return "none configured"

    async def access_token(self) -> str | None:
        """A token good for at least the next couple of minutes, or None."""
        if not self.can_refresh:
            return self._static_token

        async with self._lock:
            if self._access and self._clock() < self._expires_at:
                return self._access
            try:
                await self._refresh()
            except Exception as exc:
                log.error("Salesmsg token refresh failed: %s", exc)
                # A previously-minted token may still have life in it.
                return self._access or self._static_token
            return self._access

    async def _refresh(self) -> None:
        payload: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20)

        resp = await self._http.post(self._token_url, data=payload)
        if resp.status_code >= 400:
            body = resp.text[:300]
            if resp.status_code in (400, 401):
                # invalid_grant here means the refresh token is spent or revoked
                # and no amount of retrying will help.
                log.error(
                    "refresh token rejected (%s) — re-run get_salesmsg_token.py",
                    resp.status_code,
                )
            raise RuntimeError(f"token endpoint returned {resp.status_code}: {body}")

        data = resp.json()
        access = data.get("access_token")
        if not access:
            raise RuntimeError(f"token response had no access_token: {str(data)[:200]}")

        self._access = access
        lifetime = int(data.get("expires_in") or ASSUMED_LIFETIME_SECONDS)
        self._expires_at = self._clock() + max(lifetime - EXPIRY_LEEWAY_SECONDS, 30)

        rotated = data.get("refresh_token")
        if rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            self._save_state()
            log.info("refresh token rotated; saved to %s", STATE_FILE.name)

        log.info("Salesmsg access token refreshed (valid ~%ds)", lifetime)

    async def aclose(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as exc:
            # Shutdown-only cleanup: a client created under a different event
            # loop raises "Event loop is closed" here. Never worth propagating.
            log.debug("ignoring error closing the token HTTP client: %s", exc)
        finally:
            self._http = None


TOKENS = SalesmsgTokens.from_settings()

