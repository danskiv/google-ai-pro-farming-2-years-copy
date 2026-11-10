"""Step 5: cancel auto-renew, then read the resulting end date.

Play's current UI is a single confirm dialog (no retention screen, no reason
radio), so the flow is: Manage -> Cancel subscription -> assert the dialog names
an end date -> confirm -> verify the page reports the cancellation.

Reversible: "Resubscribe" restores auto-renew; access runs to the term end.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app import db, vault  # noqa: E402
from app.config import settings  # noqa: E402
from app.runner import browser  # noqa: E402

PLAY = "https://play.google.com/store/account/subscriptions?hl=en"


def say(tag: str, msg: str) -> None:
    print(f"[{tag:5}] {msg}", flush=True)


async def body(page) -> str:
    try:
        return " ".join((await page.inner_text("body", timeout=4000)).split())
    except Exception:
        return ""


async def _acc_id_for(email: str) -> str:
    """Resolve the acc_id for an email via the vault (account ids are random, not derived from the email)."""
    settings.ensure_dirs()
    await db.migrate()
    creds = await vault.account_creds(email)
    if not creds:
        print(f"account not in database: {email}", flush=True)
        raise SystemExit(2)
    return creds[0]


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: .venv/bin/python tasks/cancel.py <email>")
        return 2
    acc_id = await _acc_id_for(sys.argv[1])
    async with async_playwright() as p:
        ctx = await browser.launch(p, settings.profiles_dir / f"profile_{acc_id}", headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(PLAY, wait_until="domcontentloaded")
        await asyncio.sleep(4)

        await page.locator('button:has-text("Manage")').first.click(timeout=10000)
        await asyncio.sleep(4)
        say("CLICK", "Manage")

        await page.locator('button:has-text("Cancel subscription")').first.click(timeout=10000)
        await asyncio.sleep(3)

        dialog = await body(page)
        say("DIALOG", dialog[-400:])
        m = re.search(r"canceled at the end of your billing period on ([A-Z][a-z]{2} \d{1,2}, \d{4})", dialog)
        if not m:
            say("FAILED", "dialog does not name an end date — not confirmed")
            await page.screenshot(path=str(settings.artifacts_dir / "cancel_nodate.png"))
            await asyncio.sleep(4)
            await ctx.close()
            return 1
        say("DATE", m.group(1))

        # the confirm button inside the dialog (the last one, not the trigger)
        await page.locator('button:has-text("Cancel subscription")').last.click(timeout=10000)
        await asyncio.sleep(6)

        after = await body(page)
        say("AFTER", after[:400])
        dates = re.findall(r"(?:Ends on|Access until|Ends)[^.]{0,40}", after, re.IGNORECASE)
        say("MARKS", str(dates) or "none")
        await page.screenshot(path=str(settings.artifacts_dir / "cancel_result.png"))
        say("SHOT", str(settings.artifacts_dir / "cancel_result.png"))
        await asyncio.sleep(4)
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
