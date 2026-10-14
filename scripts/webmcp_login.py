"""One-time login to the Salesmessage web app, saved for the WebMCP fallback.

The web app's WebMCP tools exist only inside a logged-in page, and the login
can need a 2FA code, so a human signs in once in a real browser window. This
saves that session (cookies plus the app's localStorage token) as a Playwright
storage state; the server reuses it headless.

    python scripts/webmcp_login.py
    python scripts/webmcp_login.py --out path/to/state.json

Then set WEBMCP_ENABLED=true. The saved file is a live credential — it is
gitignored; keep it that way. Re-run this when the session expires (the
startup log says "WebMCP unavailable").
"""

from __future__ import annotations

# Run this file directly from anywhere: put the repo root on sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio

from core.config import SETTINGS
from services.webmcp import LIST_JS, READY_JS

try:
    from playwright.async_api import async_playwright
except ModuleNotFoundError:
    sys.exit("Needs Playwright: pip install playwright && playwright install chromium")


async def main(out: Path) -> int:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(SETTINGS.webmcp_url)

        print("Sign in to Salesmessage in the browser window that just opened.")
        print("Waiting until the app registers its WebMCP tools (up to 5 minutes)...")
        try:
            await page.wait_for_function(READY_JS, timeout=300_000)
        except Exception:
            # Two very different failures look identical from here; tell them
            # apart. The app keeps its login token in localStorage.
            try:
                state = await page.evaluate(
                    "() => ({token: !!localStorage.getItem('token'),"
                    " polyfill: !!navigator.modelContextTesting, url: location.href})"
                )
            except Exception:
                state = {"token": False, "polyfill": False, "url": "(page closed)"}
            await browser.close()
            if not state["token"]:
                print(
                    "Not signed in after 5 minutes (the browser window may have been "
                    "closed or hidden). Run this again and finish signing in."
                )
            else:
                print(
                    f"You are signed in ({state['url']}), but the app registered no "
                    "WebMCP tools. It only does that when the account has the in-app "
                    "AI agent enabled — ask Salesmessage to turn it on, then run this again."
                    + ("" if state["polyfill"] else " (The WebMCP layer did not load at all.)")
                )
            return 1

        tools = await page.evaluate(LIST_JS) or []
        out.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(out))
        await browser.close()

    print(f"\nSaved the session to {out}")
    print(f"{len(tools)} tools on this page: " + ", ".join(t.get("name", "?") for t in tools))
    print("Set WEBMCP_ENABLED=true in .env and restart the server.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=SETTINGS.webmcp_storage_state)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.out)))
