"""Subscribe automation: 3 layers then cancel. Assembled from what ran for real.

    .venv/bin/python tasks/subscribe_flow.py <email> [--layers 1,2,3] [--dry]

Sequence, exactly as proven on a live account:

  egress US gate -> login (password + TOTP + captcha hand-over) ->
  layer 1: /plans, "Get started", guard for "12-month free trial" + $0.00 ->
  Subscribe -> settle 45s -> layer 2: /settings, "Upgrade", guard for today+18M ->
  Subscribe -> settle -> layer 3: same, guard for today+24M -> Subscribe -> settle ->
  cancel: Manage, Cancel subscription, assert the end date, confirm, verify "Canceled".

Two habits carried over because they are what made it work:
  * the dialog is read ONLY from the eacquire frame, and only once that frame has
    rendered something recognisable — reading it early returns script noise and a
    guard would then reject a perfectly good dialog;
  * every commit is preceded by a date check, and a mismatch aborts before clicking.

`--dry` stops before each Subscribe. Needs a card already on the account.
"""

from __future__ import annotations

import asyncio
import calendar
import re
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import Page, async_playwright  # noqa: E402

from app import db, egress, secrets, vault  # noqa: E402
from app.config import settings  # noqa: E402
from app.runner import browser, nav  # noqa: E402
from app.sites.google_one import checkout, discover, guard, login  # noqa: E402

WANT = "US"
PLAY = "https://play.google.com/store/account/subscriptions?hl=en"
SETTINGS = "https://one.google.com/settings?expand=upgrade&hl=en"
SETTLE_S = 45

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DATE_RE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\s+(\d{1,2}),\s+(\d{4})\b")
DIALOG_MARKERS = ("upcoming charges", "free trial", "today $0", "add payment method")


def say(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {tag:6} {msg}", flush=True)


def add_months(base: date, months: int) -> date:
    year = base.year + (base.month - 1 + months) // 12
    month = (base.month - 1 + months) % 12 + 1
    return date(year, month, min(base.day, calendar.monthrange(year, month)[1]))

def today() -> date:
    """Google bills in the browser's timezone, which is pinned to LA. Using the
    machine clock (WIB) is a day ahead around midnight."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def near(got: list[date], expected: date, slack: int = 5) -> bool:
    # 5 days of slack: layers sit ±180 days apart, so it stays unambiguous,
    # but a weekend / timezone shift in the projected date does not fail it.
    return any(abs((d - expected).days) <= slack for d in got)


def dates_in(text: str) -> list[date]:
    out = []
    for mon, day, year in DATE_RE.findall(text):
        month = MONTHS.get(mon.lower())
        if month:
            out.append(date(int(year), month, int(day)))
    return out


async def eacq_frames(page: Page) -> list:
    return [f for f in page.frames if "eacquire" in f.url]


async def frame_text(page: Page) -> str:
    for f in await eacq_frames(page):
        try:
            return " ".join((await f.inner_text("body", timeout=3000)).split())
        except Exception:
            continue
    return ""


async def body(page: Page) -> str:
    try:
        return " ".join((await page.inner_text("body", timeout=4000)).split())
    except Exception:
        return ""


async def await_dialog(page: Page, seconds: int = 40) -> str:
    """Wait for the eacquire frame to exist and read like a checkout dialog."""
    best = ""
    for _ in range(seconds):
        await asyncio.sleep(1.0)
        text = await frame_text(page)
        if not text:
            continue
        # Local patch: through this VM's relay the unrendered eacquire frame
        # can report its own inline script source (tens of KB) as body text,
        # which false-matches the markers below. A rendered dialog is small;
        # keep waiting instead of accepting raw source.
        if len(text) > 20000:
            continue
        if any(m in text.lower() for m in DIALOG_MARKERS):
            return text
        best = text
    return best


async def click_visible(page: Page, selector: str) -> bool:
    """Click the first *visible* match. The plans page pre-mounts hidden copies of
    the same button, and `.first` lands on a 0x0 node that never becomes visible."""
    loc = page.locator(selector)
    for i in range(await loc.count()):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                await el.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


async def click_pro_discount(page: Page) -> bool:
    """Local patch: this account's plans page labels the AI Pro CTA
    "Get discount" (Annual view) instead of "Get started", and the generic
    "Upgrade" selector only hits the sidebar nav. Click the discount button
    that belongs to the Google AI Pro card specifically."""
    js = """() => {
      // Visible "Get discount" buttons run left->right: Storage, AI Plus,
      // AI Pro. Ultra has no discount button, so the last visible one is
      // the AI Pro card's. (Card headings are not ancestors of the button,
      // so ancestry matching fails; geometry is the reliable key here.)
      const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter((e) => {
          const r = e.getBoundingClientRect();
          return (e.innerText || '').trim() === 'Get discount' && r.width > 15 && r.height > 15;
        })
        .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
      if (!btns.length) return false;
      btns[btns.length - 1].click();
      return true;
    }"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def open_dialog(page: Page, layer: int) -> str:
    """Navigate, arm, click the action. Retries once if nothing opens."""
    url = checkout.PLANS if layer == 1 else SETTINGS
    # An account that already holds a plan is redirected to the settings page even
    # for layer 1, where the action is "Upgrade" — so both labels are candidates.
    sels = ['button:has-text("Get started")', 'button:has-text("Upgrade")'] if layer == 1 \
        else ['button:has-text("Upgrade")']

    for attempt in range(2):
        await nav.goto(page, url)
        for _ in range(15):
            await asyncio.sleep(1.0)
            if await page.evaluate("() => location.hostname.includes('one.google.com')"):
                break
        await asyncio.sleep(2.5)
        await page.evaluate(checkout.HOOK_JS)
        await checkout.arm(page, layer, await checkout.capture_sub_id(page))

        clicked = False
        if layer == 1:
            clicked = await click_pro_discount(page)
        if not clicked:
            for sel in sels:
                if await click_visible(page, sel):
                    clicked = True
                    break
        if not clicked:
            say("DIALOG", f"no action button visible (attempt {attempt + 1})")
            continue
        text = await await_dialog(page)
        if text:
            return text
        say("DIALOG", f"dialog did not open (attempt {attempt + 1})")
    return ""


async def click_in_frame(page: Page, selectors: list[str]) -> bool:
    for f in await eacq_frames(page):
        for sel in selectors:
            try:
                el = f.locator(sel).last
                if await el.count():
                    await el.click(timeout=6000)
                    return True
            except Exception:
                continue
    return False


async def find_button(page: Page, selectors: list[str], budget_s: float = 60.0, frames=None):
    """Return the first visible *and enabled* match, waiting for it to become live.

    The Subscribe button exists before the quote settles and swallows clicks while
    disabled, which reads as "cannot be pressed". `frames` narrows the search to the
    dialog, otherwise the page's own Subscribe behind the modal gets picked.
    """
    for _ in range(int(budget_s / 0.5)):
        for f in (frames if frames is not None else page.frames):
            for sel in selectors:
                try:
                    loc = f.locator(sel)
                    for i in range(min(await loc.count(), 3)):
                        el = loc.nth(i)
                        try:
                            await el.scroll_into_view_if_needed(timeout=1500)
                        except Exception:
                            pass
                        if await el.is_visible() and await el.is_enabled():
                            return el
                except Exception:
                    continue
        await asyncio.sleep(0.5)
    return None


async def commit(page: Page) -> bool:
    sels = ['button:text-is("Subscribe")', 'button:has-text("Subscribe")']
    for attempt in range(2):
        frames = await eacq_frames(page) or list(page.frames)
        for f in frames:                          # long dialog: scroll to the bottom first
            try:
                await f.evaluate("() => { for (const e of document.querySelectorAll('*'))"
                                 " { if (e.scrollHeight > e.clientHeight + 40) e.scrollTop = e.scrollHeight; }"
                                 " window.scrollTo(0, document.body.scrollHeight); }")
            except Exception:
                pass
        await asyncio.sleep(0.6)
        btn = await find_button(page, sels, 25.0 if attempt else 60.0, frames)
        if btn is None:
            say("LAYER", "the Subscribe button never became active")
            return False
        try:
            await btn.click(timeout=8000)
        except Exception as exc:
            say("LAYER", f"Subscribe click failed: {type(exc).__name__}")
            continue
        for _ in range(20):
            await asyncio.sleep(1.5)
            if not await eacq_frames(page):
                return True
            if "something went wrong" in (await frame_text(page)).lower():
                say("LAYER", "Google refused: Something went wrong")
                return False
            await click_in_frame(page, ['button:has-text("Not now")', 'button:has-text("Got it")',
                                        'button:has-text("Done")', 'button:has-text("Continue")'])
        if attempt == 0:
            say("LAYER", "dialog not moving — pressing once more")
    say("LAYER", "dialog still open after two clicks — continuing to verification")
    return True


async def click_any_frame(page: Page, selectors: list[str]) -> bool:
    """Click inside whichever frame holds the control. Card entry lives in the
    instrumentmanager frame, not the eacquire one."""
    for f in page.frames:
        for sel in selectors:
            try:
                loc = f.locator(sel)
                for i in range(await loc.count()):
                    el = loc.nth(i)
                    if await el.is_visible():
                        await el.click(timeout=6000)
                        return True
            except Exception:
                continue
    return False


async def ensure_card(page: Page, acc_id: str) -> bool:
    """Add the account's assigned card to its Google payment profile.

    The vault only records *which* card to use; nothing is on Google's side until
    this runs. Field order in the card form is number, expiry, CVC, holder, ZIP.
    """
    row = await db.fetch_one("SELECT card_id FROM accounts WHERE id = ?", (acc_id,))
    card = await db.fetch_one("SELECT * FROM cards WHERE id = ?", (row["card_id"],)) if row else None
    if not card:
        say("CARD", "the account has no card in the vault")
        return False
    if card["status"] != "active":
        # A dead card must not be attached even if kill_card has not rotated
        # the assignment yet — attaching it only wastes a run.
        say("CARD", f"card {card['brand']}-{card['last4']} has status {card['status']}, "
                    "not active — stopping")
        return False

    number = vault.secret_get(card["number_ref"])
    cvc = vault.secret_get(card["cvc_ref"])
    if not number or not cvc:
        say("CARD", "number/CVC unreadable from the vault")
        return False
    exp = f"{card['exp_month']}{card['exp_year']}"
    say("CARD", f"attaching {card['brand']}-{card['last4']} to the account")

    if not await click_any_frame(page, ['button:has-text("Add card")', 'text="Add card"',
                                        '[role="button"]:has-text("Add card")']):
        say("CARD", "the Add card button was not found")
        return False

    for _ in range(25):
        await asyncio.sleep(1.0)
        frame = None
        for f in page.frames:
            try:
                if await f.locator("input:visible").count() >= 3:
                    frame = f
                    break
            except Exception:
                continue
        if frame is None:
            continue
        say("FRAME", f"card form in {frame.url[:70]}")
        inputs = frame.locator("input:visible")
        values = [number, exp, cvc]
        saved = False
        for attempt in range(2):
            if attempt:
                say("CARD", "form not complete — refilling slowly")
                await asyncio.sleep(2.0)
                frame = None
                for f in page.frames:
                    try:
                        if await f.locator("input:visible").count() >= 3:
                            frame = f
                            break
                    except Exception:
                        continue
                if frame is None:
                    break
                inputs = frame.locator("input:visible")
            await asyncio.sleep(0.4)

            # A freshly opened modal is not yet stable for typing: the
            # 2026-11-09 run lost almost every digit of the number (the field
            # ended up holding 2 digits) because typing began too early.
            await asyncio.sleep(2.5)
            for idx, value in enumerate(values):
                field = inputs.nth(idx)
                want = re.sub(r"\D", "", value)
                for _try in range(3):
                    await field.click()
                    await field.fill("")
                    # Google reformats the card number while it is typed, so
                    # type slowly with pauses so the next field does not shift —
                    # then *read it back*: digits that did not stick are retyped,
                    # not carried into Save.
                    await field.press_sequentially(value, delay=110 if idx == 0 else 60)
                    await asyncio.sleep(0.6)
                    try:
                        got = re.sub(r"\D", "", await field.input_value())
                    except Exception:
                        got = ""
                    if got == want:
                        break
                    say("CARD", f"field {idx + 1} did not stick "
                                 f"({len(got)}/{len(want)} digits) — retyping")
                await asyncio.sleep(0.9 if idx == 0 else 0.45)
            if await inputs.count() >= 5:
                try:
                    if not (await inputs.nth(3).input_value()).strip():
                        await inputs.nth(3).fill("CARD HOLDER")
                    await inputs.nth(4).fill("97201")
                except Exception:
                    pass
            await asyncio.sleep(0.8)

            save = 'button:has-text("Save card")'
            for _ in range(12):                   # wait for the save button to enable
                try:
                    loc = frame.locator(save).first
                    if not await loc.count():
                        break
                    if not await loc.is_disabled():
                        break
                except Exception:
                    break
                await asyncio.sleep(0.4)

            say("CARD", "data filled in, saving")
            if not await click_any_frame(page, ['button:has-text("Save card")',
                                                'button:has-text("Save")', 'text=/^Save$/']):
                say("CARD", "the save button was not found")
                continue

            stuck = 0
            for _ in range(25):
                await asyncio.sleep(1.5)
                text = await frame_text(page)
                low = text.lower()
                if "add payment method" not in low and (
                    "today" in low or "free trial" in low
                ):
                    say("CARD", "card saved, dialog back at checkout")
                    return True
                if "add credit or debit card" in low or "all fields are required" in low:
                    # Save did not take the form (digits lost etc.) — do not burn
                    # the rest of the wait window; go straight to a refill attempt.
                    stuck += 1
                    if stuck >= 7:
                        say("CARD", "form still intact after Save — refilling")
                        break
            saved = True
        say("CARD", "dialog did not return after saving" if saved
            else "card failed to fill")
        return False
    say("CARD", "the card form never appeared")
    return False


async def run_layers(page: Page, email: str, acc_id: str, layers: list[int], dry: bool) -> bool:
    """Drive the requested layers, verifying by date before every commit.

    Nothing here trusts the caller's layer list blindly: each iteration re-reads
    the dialog and, if the account is already past that layer, skips it instead of
    submitting a duplicate.
    """
    end = await current_end_date(page)
    have = discover.months_from(discover.today(), end) if end else 0
    stage = discover.stage_for(have)
    say("DISC", f"current term {end or 'no subscription'} → {stage.label} ({have} months)")
    capped = False
    if stage.done:
        # Already at the cap. Try the spare token once: if Google takes it the term
        # moves, if it answers "Something went wrong" the cap is real and we finish.
        say("LAYER", "already at 24 months — trying layer 4 once")
        layers = [4]
        capped = True
    else:
        if have:
            await resubscribe(page)
        layers = [n for n in layers if n >= stage.start_layer]
        if not layers:
            say("LAYER", "no layers left")
            return True
        if layers[0] > 1:
            say("LAYER", f"starting from layer {layers[0]} (already {have} months)")

    for index, layer in enumerate(layers):
        await db.execute("UPDATE accounts SET step = ?, status = 'running' WHERE email = ?",
                         (layer, email))
        say("LAYER", f"{layer} — opening dialog")
        text = await open_dialog(page, layer)
        shot = settings.artifacts_dir / f"flow_L{layer}_{int(time.time())}.png"
        await page.screenshot(path=str(shot))
        say("SHOT", str(shot))

        if not text:
            if capped and layer == 4:
                say("LAYER", "layer 4 refused — the 24-month cap is real")
                return True
            say("LAYER", "empty dialog — stopping")
            return False
        if "add payment method" in text.lower():
            if dry:
                say("DRY", "no card on the account yet — fill it in manually if needed")
            elif not await ensure_card(page, acc_id):
                say("LAYER", "card entry failed — stopping before any commit")
                return False
            else:
                text = await await_dialog(page, 40)
                if not text:
                    say("LAYER", "dialog did not return after the card was saved")
                    return False

        got = dates_in(text)

        # Combined guard on every layer: today's price must be zero
        # (guard.judge, anchored to the "due today" label) AND the end date
        # must match. Layer 1 used to check only the "$0.00" string and
        # layers 2-4 only the date — a dialog that really charges could slip
        # through either side.
        verdict = guard.judge(text)
        if layer == 4:
            expected = add_months(discover.today(), 24)
            date_ok = near(got, expected) or near(got, add_months(discover.today(), 30))
        elif layer == 1:
            expected = add_months(discover.today(), 12)
            # A first-time account sometimes advertises its trial by name
            # only ("12-month free trial") with no calendar date at all;
            # that name is distinctive enough as date evidence.
            date_ok = near(got, expected) or (not got and "12-month" in text.lower())
        else:
            expected = add_months(discover.today(), {1: 12, 2: 18, 3: 24}[layer])
            date_ok = near(got, expected)
        ok = verdict.safe and date_ok
        say("GUARD", f"layer {layer}: price {'ok' if verdict.safe else 'FAILED'} "
                     f"({verdict.reason}) · expected date ±{expected} · read {got or '—'}"
                     f" → {'passed' if ok else 'FAILED'}")
        if not ok:
            print("        " + text[:280])
            if capped and layer == 4:
                say("LAYER", "layer 4 refused — the 24-month cap is real, continuing to cancel")
                return True
            return False

        if dry:
            say("DRY", f"layer {layer} ready, not submitted")
            continue

        if not await commit(page):
            say("LAYER", "the Subscribe button could not be pressed — stopping")
            return False
        say("LAYER", f"{layer} submitted · settlement {SETTLE_S}s")
        await asyncio.sleep(SETTLE_S)
        await db.execute("UPDATE accounts SET step = ? WHERE email = ?", (layer + 1, email))

        if index < len(layers) - 1:
            nxt = layers[index + 1]
            verify_text = await open_dialog(page, nxt)
            expected = add_months(discover.today(), 18 if nxt == 2 else 24)
            vgot = dates_in(verify_text)
            good = near(vgot, expected)
            say("VERIF", f"after layer {layer}: expected ±{expected} · read {vgot or '—'} → "
                         f"{'match' if good else 'MISMATCH'}")
            if not good:
                return False
    return True


async def resubscribe(page: Page) -> None:
    """A cancelled plan cannot take a plan change; Play offers "Resubscribe"."""
    txt = await body(page)
    if "Canceled" not in txt or "Resubscribe" not in txt:
        return
    say("ACTIVE", "subscription is Canceled — resubscribing first")
    try:
        await page.locator('button:has-text("Resubscribe")').first.click(timeout=8000)
        await asyncio.sleep(4)
        for sel in ('button:has-text("Subscribe")', 'button:has-text("Confirm")',
                    'button:has-text("Resubscribe")'):
            try:
                await page.locator(sel).last.click(timeout=4000)
                break
            except Exception:
                continue
        await asyncio.sleep(5)
    except Exception as exc:
        say("ACTIVE", f"resubscribe failed: {type(exc).__name__}")
        return
    after = await body(page)
    say("ACTIVE", "active again" if "Canceled" not in after else "still Canceled")


async def current_end_date(page: Page) -> "date | None":
    """True term end, read from the cancel dialog without committing.

    The offers dialog projects the term *after* the armed layer, so it cannot say
    where the account is now. The cancel dialog states it outright.
    """
    await nav.goto(page, PLAY)
    await asyncio.sleep(4)
    txt = await body(page)
    if "Canceled" in txt and "will end on" in txt:
        hit = re.search(r"will end on ([A-Z][a-z]{2} \d{1,2}, \d{4})", txt)
        return parse_date(hit.group(1)) if hit else None
    if "Active" not in txt:
        return None
    try:
        await page.locator('button:has-text("Manage")').first.click(timeout=8000)
        await asyncio.sleep(3)
        await page.locator('button:has-text("Cancel subscription")').first.click(timeout=8000)
        await asyncio.sleep(3)
    except Exception:
        return None
    dialog = await body(page)
    hit = re.search(r"canceled at the end of your billing period on ([A-Z][a-z]{2} \d{1,2}, \d{4})", dialog)
    try:
        await page.locator('button:has-text("Keep subscription")').first.click(timeout=4000)
    except Exception:
        pass
    return parse_date(hit.group(1)) if hit else None


def parse_date(text: str) -> "date | None":
    m = re.match(r"([A-Z][a-z]{2})[a-z]* (\d{1,2}), (\d{4})", text.strip())
    if not m:
        return None
    month = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}.get(m.group(1).lower())
    if not month:
        return None
    return date(int(m.group(3)), month, int(m.group(2)))


async def any_text(page: Page, needle: str) -> bool:
    """payments.google.com renders the card list inside a frame, so the main body
    alone never shows it."""
    for f in page.frames:
        try:
            if needle in " ".join((await f.inner_text("body", timeout=3000)).split()):
                return True
        except Exception:
            continue
    return False


async def click_text(page: Page, pattern: str) -> bool:
    for f in page.frames:
        try:
            loc = f.locator(pattern).first
            if await loc.count():
                await loc.click(timeout=6000)
                return True
        except Exception:
            continue
    return False


async def click_near(page: Page, needle: str, pattern: str, also: str | None = None) -> bool:
    """Click inside the frame that owns `needle` (and `also`, if given), so a plain
    "Remove" hits the right card instead of a row that merely mentions it."""
    for f in page.frames:
        try:
            txt = await f.inner_text("body", timeout=2000)
            if needle not in txt or (also and also not in txt):
                continue
            loc = f.locator(pattern).first
            if await loc.count():
                await loc.click(timeout=2500)
                return True
        except Exception:
            continue
    return False


async def has_text(page: Page, pattern: str) -> bool:
    for f in page.frames:
        try:
            if await f.locator(pattern).count():
                return True
        except Exception:
            continue
    return False


async def detach_on_google(page: Page, last4: str) -> bool:
    """Best effort: remove the card from the account's payment profile.

    Play hands off to payments.google.com, which demands a re-authorisation that only
    offers "tap Yes on your phone" — no password path. So we wait for the human, then
    remove. Returns False rather than raising so the run still counts as done.
    """
    try:
        await nav.goto(page, "https://play.google.com/store/paymentmethods")
        asked = saved = tried_edit = False
        absent = 0
        # The Play/payments pages follow the account's language, not ?hl=en —
        # an Indonesian account shows Indonesian labels. All text matching
        # here is bilingual on purpose; the verify button itself has two
        # variants ("Verify that it's you" on Play, "Verify it's you" in the
        # payments centre).
        for _ in range(750):                      # ±300s, no fixed waits
            if await any_text(page, "sure that it's you") or \
                    await any_text(page, "make sure it's you") or \
                    await any_text(page, "verify to see") or \
                    await any_text(page, "memastikan bahwa ini memang anda"):
                # The re-auth is only sent when its button is pressed; once the
                # session is verified the button no longer appears. This is
                # checked first because while verification runs nothing else
                # can be clicked.
                if not asked and (
                    await click_text(page, 'text="Verify that it\'s you"')
                    or await click_text(page, 'text="Verify it\'s you"')
                    or await click_text(page, 'text="Verifikasi diri Anda"')
                ):
                    asked = True
                    say("CARD", "request sent to the phone — approve it there (max 300s)")
            # Page-text needles stay bilingual on purpose: the payments page
            # follows the account's language, so both spellings are matched.
            elif await any_text(page, "will be removed") or \
                    await any_text(page, "akan dihapus"):
                if not saved and (
                    await click_text(page, 'text="Remove and save"')
                    or await click_text(page, 'text="Hapus dan simpan"')
                ):
                    saved = True
                    await asyncio.sleep(1.2)
                if not await any_text(page, last4):
                    return True
            elif await any_text(page, last4):
                absent = 0
                if not saved:
                    if await click_near(page, last4, 'text="Remove"', also="Remove") or \
                            await click_near(page, last4, 'text="Hapus"', also="Hapus"):
                        await asyncio.sleep(1.0)
                    elif not tried_edit and (
                        await has_text(page, 'text="Edit payment methods"')
                        or await has_text(page, 'text="Edit metode pembayaran"')
                    ):
                        # On the Play page the card shows without a Remove
                        # button — Remove lives in the payments centre behind
                        # the Edit link. Without this fall-through the loop
                        # spins until timeout (the 2026-11-09 run incident).
                        tried_edit = True
                        await click_text(page, 'text="Edit payment methods"')
                        await click_text(page, 'text="Edit metode pembayaran"')
                        await asyncio.sleep(2.0)
            elif await any_text(page, "Add a payment") or \
                    await any_text(page, "Add payment method") or \
                    await any_text(page, "No payment methods yet") or \
                    await any_text(page, "Tambahkan metode pembayaran"):
                # The payment-method list is shown (wallet or Play page) but
                # the card is not in it — with zero cards the edit link is gone too.
                absent += 1
                if absent >= 3:
                    say("CARD", "the card is no longer on the account")
                    return False
            elif not tried_edit and (
                await has_text(page, 'text="Edit payment methods"')
                or await has_text(page, 'text="Edit metode pembayaran"')
            ):
                tried_edit = True
                await click_text(page, 'text="Edit payment methods"')
                await click_text(page, 'text="Edit metode pembayaran"')
            await asyncio.sleep(0.4)
        say("CARD", "verification did not finish — card released in the vault only")
        return False
    except Exception as exc:
        say("CARD", f"removal on Google failed: {type(exc).__name__}")
        return False


async def release_card(page: Page, acc_id: str) -> None:
    """Drop the card from the account, but keep it in the vault for the next one.

    Google side is best effort (the payments page often renders blank under
    automation); the vault assignment is always cleared so the card is not left
    pinned to a finished account. The card row itself is never deleted.
    """
    row = await db.fetch_one(
        "SELECT a.card_id, c.last4 FROM accounts a JOIN cards c ON c.id = a.card_id WHERE a.id = ?",
        (acc_id,))
    card_id = row["card_id"] if row else ""
    if not card_id:
        return
    removed = await detach_on_google(page, row["last4"])
    say("CARD", "removed from the account" if removed else "removal on Google not proven")
    # The vault assignment is always cleared (by design), but a Google-side
    # failure must be visible in the UI via step_message — not only in the run log.
    await db.execute(
        "UPDATE accounts SET step_message = ? WHERE id = ?",
        ("" if removed else "step_card_not_yet_removed", acc_id),
    )
    await db.execute("UPDATE accounts SET card_id = '' WHERE id = ?", (acc_id,))
    say("CARD", "assignment released in the vault — the card itself is kept")


async def cancel(page: Page) -> bool:
    await nav.goto(page, PLAY)
    await asyncio.sleep(4)
    state = await body(page)
    if "Canceled" in state and "will end on" in state:
        say("CANCEL", "already Canceled — skipped")
        return True
    try:
        await page.locator('button:has-text("Manage")').first.click(timeout=10000)
    except Exception:
        say("CANCEL", "the Manage button is absent")
        return False
    await asyncio.sleep(4)
    try:
        await page.locator('button:has-text("Cancel subscription")').first.click(timeout=10000)
    except Exception:
        say("CANCEL", "the Cancel subscription button is absent")
        return False
    await asyncio.sleep(3)

    dialog = await body(page)
    m = re.search(r"canceled at the end of your billing period on ([A-Z][a-z]{2} \d{1,2}, \d{4})", dialog)
    if not m:
        say("CANCEL", "the dialog names no date — not confirmed")
        return False
    say("CANCEL", f"ends {m.group(1)} · confirming")
    await page.locator('button:has-text("Cancel subscription")').last.click(timeout=10000)
    await asyncio.sleep(6)

    after = await body(page)
    ok = "Canceled" in after and "will end on" in after
    say("CANCEL", "verified: Canceled + end date" if ok
        else "'Canceled' status not read yet")
    return ok


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    email = sys.argv[1]
    layers = [1, 2, 3]
    dry = "--dry" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--layers" and i + 1 < len(sys.argv):
            layers = [int(x) for x in sys.argv[i + 1].split(",")]
        if a == "--from" and i + 1 < len(sys.argv):
            start = max(1, min(3, int(sys.argv[i + 1])))
            layers = [n for n in (1, 2, 3) if n >= start]

    settings.ensure_dirs()
    await db.migrate()

    creds = await vault.account_creds(email)
    if not creds:
        print(f"account not in the database: {email}", flush=True)
        return 2
    acc_id, password, secret = creds
    if not password:
        print(f"empty password in the vault for {email} ({acc_id}) — re-import the account",
              flush=True)
        return 2
    print(f"account {email} · {acc_id} · password {len(password)} chars"
          f" · totp {'present' if secret else 'absent'}", flush=True)

    async with async_playwright() as p:
        ctx = await browser.launch(p, settings.profiles_dir / f"profile_{acc_id}", headless=False)
        await ctx.add_init_script(await checkout.init_script())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await nav.goto(page, egress.ECHO)
        seen = await egress.observe(page)
        waited = 0
        while not egress.matches(WANT, seen["country"]) and waited < 120:
            say("EGRESS", f"{seen['country']} ≠ {WANT} · waiting {waited}/120s")
            await asyncio.sleep(10)
            waited += 10
            await nav.goto(page, egress.ECHO)
            seen = await egress.observe(page)
        say("EGRESS", f"{seen['ip']} · {seen['country']} · {seen['org']}")
        if not egress.matches(WANT, seen["country"]):
            await ctx.close()
            return 1

        if not await login.ensure_signed_in(page, email, password, secret):
            say("LOGIN", "FAILED — finish it manually in the Chrome window")
            await asyncio.sleep(300)
            await ctx.close()
            return 1
        say("LOGIN", "ready")

        finished = await run_layers(page, email, acc_id, layers, dry)
        if not finished:
            say("STOP", "layers not complete — cancel not run")
            await page.screenshot(path=str(settings.artifacts_dir / f"flow_stop_{int(time.time())}.png"))
            await asyncio.sleep(10)
            await ctx.close()
            return 1

        if dry:
            say("DRY", "stopping before any commit")
            await asyncio.sleep(10)
            await ctx.close()
            return 0

        done = await cancel(page)
        if done:
            await release_card(page, acc_id)
        await db.execute("UPDATE accounts SET status = ? WHERE email = ?",
                         ("subscribed" if done else "failed", email))
        await page.screenshot(path=str(settings.artifacts_dir / f"flow_final_{int(time.time())}.png"))
        say("DONE", "cancel verified" if done else "cancel not verified")
        await asyncio.sleep(10)
        await ctx.close()
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
