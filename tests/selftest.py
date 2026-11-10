"""Self-test, stdlib only. Run: .venv/bin/python tests/selftest.py"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Jangan sentuh data asli: uji jalan di direktori sementara.
_TMP = Path(tempfile.mkdtemp(prefix="gai-selftest-"))
os.environ["GAI_DATA_DIR"] = str(_TMP)

from app import db, secrets, totp  # noqa: E402
from app.config import _default_data_dir, settings  # noqa: E402
from app.models import AccountCreate  # noqa: E402
from app.runner import browser  # noqa: E402

# RFC 6238 Appendix B: secret "12345678901234567890" (ASCII), SHA-1, 8 digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_totp_rfc_vectors() -> None:
    cases = {59: "287082", 1111111109: "081804", 1234567890: "005924", 2000000000: "279037"}
    for at, expected in cases.items():
        assert totp.code(RFC_SECRET, at=at) == expected, (at, totp.code(RFC_SECRET, at=at))


def test_totp_shapes() -> None:
    assert totp.code("") == ""
    assert totp.code("not-base32!!") == ""
    assert totp.code("jbsw y3dp ehpk 3pxp") != ""
    assert 1 <= totp.seconds_left() <= 30
    assert totp.wait_hint(at=0) == 0


def test_email_normalisation() -> None:
    a = AccountCreate(email="Some.One@Gmail.COM", password="x")
    assert a.email == "some.one@gmail.com"
    for bad in ("nope", "a@b", "@b.com", "a b@c.com"):
        try:
            AccountCreate(email=bad, password="x")
        except Exception:
            continue
        raise AssertionError(f"accepted bad email {bad!r}")


def test_secrets_roundtrip() -> None:
    ref = "selftest:roundtrip"
    secrets.delete(ref)
    assert secrets.get(ref) == ""
    secrets.put(ref, "p@ss-with-symbols-9=X")
    assert secrets.get(ref) == "p@ss-with-symbols-9=X"
    secrets.put(ref, "overwritten")
    assert secrets.get(ref) == "overwritten"
    secrets.delete(ref)
    assert secrets.get(ref) == ""


def test_db_migrate() -> None:
    async def run() -> None:
        await db.migrate()
        await db.migrate()  # idempotent
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM accounts")
        assert row is not None and row["n"] == 0
        cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(accounts)")}
        assert {"email", "secret_ref", "totp_ref", "step", "expiry_date"} <= cols

    asyncio.run(run())


def test_secret_backend_reported() -> None:
    assert secrets.backend() in {"security", "secret-tool", "file"}
    assert secrets.describe()


def test_file_backend_roundtrip_and_mode() -> None:
    """The fallback backend must work and must never be group/world readable."""
    saved = secrets._backend
    try:
        secrets._backend = "file"
        ref = "selftest:file"
        secrets.put(ref, "nilai-rahasia")
        assert secrets.get(ref) == "nilai-rahasia"
        assert secrets.exists(ref)
        path = settings.data_dir / "vault.json"
        assert path.exists(), path
        if sys.platform != "win32":
            # Windows has no POSIX modes; the profile ACL is the protection.
            assert path.stat().st_mode & 0o077 == 0, oct(path.stat().st_mode)
        secrets.delete(ref)
        assert secrets.get(ref) == ""
    finally:
        secrets._backend = saved


def test_data_dir_follows_platform() -> None:
    path = _default_data_dir()
    assert path.name == "GaiConsole"
    if sys.platform == "darwin":
        assert "Library/Application Support" in path.as_posix()
    elif sys.platform == "win32":
        if not os.environ.get("LOCALAPPDATA"):
            assert "AppData/Local" in path.as_posix(), path
    elif not os.environ.get("XDG_DATA_HOME"):
        assert path.as_posix().endswith(".local/share/GaiConsole"), path


def test_headless_resolution() -> None:
    saved_mode, saved_display = settings.headless, os.environ.get("DISPLAY")
    try:
        settings.headless = "1"
        assert browser.resolve_headless(False) is True
        settings.headless = "0"
        assert browser.resolve_headless(None) is False
        settings.headless = "auto"
        if sys.platform.startswith("linux"):
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            # headed was asked for, no display exists: headless is the only option
            assert browser.resolve_headless(False) is True
    finally:
        settings.headless = saved_mode
        if saved_display is not None:
            os.environ["DISPLAY"] = saved_display


def test_discover_today_resolves_zone() -> None:
    """discover loads ZoneInfo("America/Los_Angeles") at import time.

    Windows has no built-in IANA timezone database — without the `tzdata`
    package (pinned in pyproject + lock) this import blows up there and
    every account run dies immediately.
    """
    import tzdata

    from app.sites.google_one import discover

    # On Linux discover.today() still works from /usr/share/zoneinfo even
    # without the package; pin the package's presence so a venv without
    # tzdata fails here, not later on Windows.
    assert tzdata.__file__, "the tzdata package is not installed in this venv"
    assert discover.today().isoformat()


def _patched_win32(env: dict[str, str]):
    """Patch sys.platform + the Windows env for one block, restore after."""
    saved_platform = sys.platform
    saved_env = {k: os.environ.get(k) for k in env}
    sys.platform = "win32"
    os.environ.update(env)
    return saved_platform, saved_env


def _restore_win32(saved: tuple[str, dict[str, str | None]]) -> None:
    platform, env = saved
    sys.platform = platform
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_win32_data_dir_simulation() -> None:
    """On any host: win32 platform + a custom LOCALAPPDATA -> the data dir follows."""
    saved = _patched_win32({"LOCALAPPDATA": r"D:\Profil\Local"})
    try:
        path = _default_data_dir()
        assert path.name == "GaiConsole"
        assert str(path).replace("\\", "/").endswith("Profil/Local/GaiConsole"), path
    finally:
        _restore_win32(saved)


def test_win32_browser_resolution_simulation() -> None:
    """On any host: a chrome.exe in a fake Windows install tree is picked."""
    root = Path(tempfile.mkdtemp(prefix="gai-win32-"))
    chrome_exe = root / "Program Files" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome_exe.parent.mkdir(parents=True)
    chrome_exe.write_bytes(b"")
    chrome_exe.chmod(0o755)
    saved = _patched_win32(
        {
            "PROGRAMFILES": str(root / "Program Files"),
            "PROGRAMFILES(X86)": str(root / "Program Files (x86)"),
            "LOCALAPPDATA": str(root / "Local"),
        }
    )
    # shutil.which reads sys.platform and takes the win32 path, which needs
    # Windows-only modules; on a non-Windows host stub out its PATH search
    # (what is tested here is discovery from the install tree, not PATH).
    saved_which = browser.shutil.which
    browser.shutil.which = lambda _name: None
    try:
        choice = browser.resolve_browser()
        assert choice == {"executable_path": str(chrome_exe)}, choice
        assert browser.has_display() is True
    finally:
        browser.shutil.which = saved_which
        _restore_win32(saved)


def test_win_browser_paths_shape() -> None:
    """Windows discovery is pure path building — shape-check it on any host."""
    chrome, chromium = browser._win_browser_paths()
    assert chrome and chromium
    assert all(p.endswith("chrome.exe") for p in chrome + chromium)
    assert all("Chrome" in p for p in chrome)
    assert all("Chromium" in p for p in chromium)


def test_browser_resolution_shape() -> None:
    choice = browser.resolve_browser()
    assert isinstance(choice, dict)
    assert set(choice) <= {"channel", "executable_path"}
    assert all(isinstance(v, str) and v for v in choice.values())
    assert isinstance(browser.has_display(), bool)
    assert "headless" in browser.describe() or "headed" in browser.describe()


def test_guard_judge_today_scoping() -> None:
    """The money guard is judged by *today's* charge, not a zero anywhere."""
    from app.sites.google_one import guard

    # (a) a valid trial: $0.00 today, a future price named -> passes
    v = guard.judge("Start free trial · Due today $0.00 · Then $19.99/month starting Dec 8, 2026")
    assert v.safe, v
    # (b) today's charge is not zero, despite a $0.00 (tax) -> fails
    v = guard.judge("Subscribe · $199.99 due today + $0.00 tax")
    assert not v.safe, v
    # (c) empty / unreadable text -> fails (fail closed)
    assert not guard.judge("").safe
    assert not guard.judge("   ").safe
    assert not guard.judge("dialog tanpa nominal apa pun").safe
    # (d) today's charge not zero with no zero at all -> fails
    assert not guard.judge("Due today $49.99").safe
    # a non-zero amount with no today label & no future context -> fails
    assert not guard.judge("Total $199.99 · tax $0.00").safe
    # only a future price + a zero -> passes
    assert guard.judge("Due $0.00 · renews at $19.99/month after the trial").safe
    # a negation is not a charge: "won't be charged today" anchors no amount
    v = guard.judge("You won't be charged today · $0.00 due at trial end · then $19.99/month")
    assert v.safe, v
    # the layer 2-4 "Upcoming charges" dialog: no today anchor, and
    # *every* non-zero amount is bound to a future start date -> passes
    v = guard.judge(
        "Google AI Pro (2 TB) · Upcoming charges · Starting May 8, 2027 $13.99/month"
        " · Starting May 8, 2028 $19.99/month · Subscribe"
    )
    assert v.safe, v
    # a plain paid dialog (Tier B, no start marker): the recurring units
    # /month & /year and "renews" are not a deferral -> fails
    assert not guard.judge(
        "Google AI Pro (2 TB) · $19.99/month · Billed monthly · Subscribe"
    ).safe
    assert not guard.judge(
        "Subscribe to Google AI Pro · $199.99/year · Auto-renews on Nov 8, 2027"
    ).safe
    # "Today $X": a bare "today" anchors the amount after it
    assert not guard.judge("Today $19.99").safe
    v = guard.judge("12-month free trial · Today $0.00 · $19.99/month after trial")
    assert v.safe, v

    # The real layer 2/3 dialog (2026-11-09 run): the itemised components
    # are printed AFTER their future total — "$31.98" = "$19.99" + "$11.99" —
    # and there is no charge today -> passes via the itemisation rule.
    l2 = (
        "Google AI Pro (5 TB) + YouTube Premium Individual Add On "
        "Upcoming charges Starting Dec 8, 2026 $11.99/month includes tax of $0.00 "
        "YouTube Premium Individual Add On $11.99 Starting May 8, 2028 $31.98/month "
        "includes tax of $0.00 Google AI Pro (5 TB) $19.99 YouTube Premium "
        "Individual Add On $11.99 Subscribing on Play"
    )
    v = guard.judge(l2)
    assert v.safe, v
    v = guard.judge(l2.replace("May 8, 2028", "Nov 8, 2028"))
    assert v.safe, v
    # total tampered -> the components no longer sum to it -> fails
    assert not guard.judge(l2.replace("$31.98", "$30.98")).safe
    # an extra unexplained amount after the itemisation -> fails
    assert not guard.judge(l2 + " · Activation $5.00").safe
    # the itemisation rule does not rescue a plain paid dialog with an
    # invented breakdown: without a bound future total, it still fails
    assert not guard.judge(
        "Google AI Pro $19.99 · YouTube Premium $11.99 · Total $31.98 · Billed monthly"
    ).safe


def test_same_origin_guard() -> None:
    """POST lintas origin ditolak; tanpa Origin (curl/API) lolos berbekal token."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.security import require_same_origin

    def req(headers: dict[str, str]):
        return SimpleNamespace(headers=headers)

    async def run() -> None:
        host = {"host": "127.0.0.1:8765"}
        # no Origin/Referer -> passes
        await require_same_origin(req(host))
        # Origin / Referer on the same host as the Host header -> passes
        await require_same_origin(req({**host, "origin": "http://127.0.0.1:8765"}))
        await require_same_origin(req({**host, "referer": "http://127.0.0.1:8765/"}))
        # another host/port, or Origin "null" (sandboxed iframe) -> 403
        for bad in ("http://127.0.0.1:9999", "https://evil.example", "null"):
            try:
                await require_same_origin(req({**host, "origin": bad}))
            except HTTPException as exc:
                assert exc.status_code == 403, exc.status_code
                continue
            raise AssertionError(f"origin {bad!r} passed")

    asyncio.run(run())


def test_latest_log_rejects_bad_ids() -> None:
    """A raw acc_id from the URL must not slip through as a glob pattern."""
    from app.runner import queue

    assert queue.latest_log("../etc/passwd") is None
    assert queue.latest_log("acc_xyz") is None
    assert queue.latest_log("*") is None
    # a valid pattern but no log for it yet -> None as well
    assert queue.latest_log("acc_0123456789ab") is None


def test_vault_card_rotation_and_reimport() -> None:
    """kill_card rotates the account to an active card; re-import refreshes it."""
    from app import vault

    async def run() -> None:
        await db.migrate()
        ok, note = await vault.add_card("4242424242424242", "12", "30", "123")
        assert ok, note
        ok, note = await vault.add_card("5555555555554444", "12", "30", "123")
        assert ok, note
        ok, note = await vault.add_account("test@example.com", "pw-old", "")
        assert ok, note
        acc = await db.fetch_one("SELECT * FROM accounts WHERE email = 'test@example.com'")
        assert acc is not None and acc["card_id"], acc
        dead = acc["card_id"]
        others = [c["id"] for c in await db.fetch_all("SELECT id FROM cards") if c["id"] != dead]
        assert len(others) == 1

        await vault.kill_card(dead, dead=True)
        acc = await db.fetch_one("SELECT * FROM accounts WHERE email = 'test@example.com'")
        assert acc["card_id"] == others[0], acc["card_id"]

        # revive it: backfill does not steal an account that already has a card
        await vault.kill_card(dead, dead=False)
        acc = await db.fetch_one("SELECT * FROM accounts WHERE email = 'test@example.com'")
        assert acc["card_id"] == others[0], acc["card_id"]

        # re-import: the seed that was empty is now written, failed -> pending,
        # and the card assignment stays valid
        await db.execute("UPDATE accounts SET status = 'failed' WHERE id = ?", (acc["id"],))
        ok, note = await vault.add_account("test@example.com", "pw-new", "JBSWY3DPEHPK3PXP")
        assert not ok and note.endswith("updated"), note
        acc = await db.fetch_one("SELECT * FROM accounts WHERE email = 'test@example.com'")
        assert acc["status"] == "pending", acc["status"]
        assert acc["totp_ref"] == f"{acc['id']}:totp", acc["totp_ref"]
        assert acc["card_id"] == others[0], acc["card_id"]
        assert vault.secret_get(acc["totp_ref"])

    asyncio.run(run())


def test_lock_covers_platforms() -> None:
    """A darwin-only lock is what broke Linux installs; keep all platforms in it."""
    lock = Path(__file__).resolve().parent.parent / "requirements.lock"
    text = lock.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    tags = re.findall(r"^# files: (.+)$", text, re.MULTILINE)
    assert len(tags) >= 20, f"{len(tags)} package tags"
    combined = " ".join(tags)
    assert "linux=" in combined, "lock pinned no linux wheels"
    assert "macos=" in combined, "lock pinned no macOS wheels"
    assert "windows=" in combined, "lock pinned no windows wheels"


def test_guard_rejects_unclassified_positive_charge() -> None:
    from app.sites.google_one import guard

    verdict = guard.judge("Due today $0.00 · Activation fee $5.00")
    assert verdict.safe is False, verdict


def test_guard_rejects_trial_without_verifiable_amount() -> None:
    from app.sites.google_one import guard

    verdict = guard.judge("Start your free trial")
    assert verdict.safe is False, verdict


def test_guard_checkout_price_boundaries() -> None:
    from app.sites.google_one import guard

    cases = [
        ("Due today $0.00", True),
        ("Due today $19.99", False),
        ("Due today $19.99 · Tax $0.00", False),
        ("Due today $0.00 · Then $19.99/month starting Jan 2028", True),
        ("", False),
        (
            "Due today $0.00 · Activation fee $5.00 · Then $19.99/month starting Jan 2028",
            False,
        ),
    ]
    for text, expected in cases:
        verdict = guard.judge(text)
        assert verdict.safe is expected, (text, verdict)


def test_guard_future_suffix_does_not_scope_later_tax() -> None:
    from app.sites.google_one import guard

    text = "Due today $0.00 · Then $19.99/month starting Jan 2028 · Tax $5.00"
    verdict = guard.judge(text)
    assert verdict.safe is False, verdict


def test_guard_future_suffix_does_not_scope_later_fee() -> None:
    from app.sites.google_one import guard

    text = "Due today $0.00 · Then $19.99/month starting Jan 2028 · Activation fee $5.00"
    verdict = guard.judge(text)
    assert verdict.safe is False, verdict


def test_guard_rejects_unpriced_charge_label() -> None:
    from app.sites.google_one import guard

    verdict = guard.judge("Due today $0.00 · Activation fee")
    assert verdict.safe is False, verdict


def test_guard_future_context_boundaries() -> None:
    from app.sites.google_one import guard

    cases = [
        ("Activation fee $5.00 · Then $19.99/month starting Jan 2028", False),
        ("Due today $0.00 · Tax $0.00 · Then $19.99/month starting Jan 2028", True),
        ("Due today $0.00 · Then $19.99/month starting Jan 2028 · Tax $0.00", True),
        ("Due today $0.00 · Then $19.99/month starting Jan 2028. Tax $5.00", False),
        (
            "Due today $0.00 · Then $19.99/month starting Jan 2028"
            " · Then €29,99/month starting Jan 2029",
            True,
        ),
        ("Activation fee · Due today $0.00", False),
        ("$0.00 · Activation fee · Then $19.99/month starting Jan 2028", False),
    ]
    for text, expected in cases:
        verdict = guard.judge(text)
        assert verdict.safe is expected, (text, verdict)


def test_guard_currency_decimal_boundaries() -> None:
    from app.sites.google_one import guard

    cases = [
        ("Due today €0,00", True),
        ("Due today €19,99", False),
        ("Due today Rp0,00", True),
        ("Due today $0.00 · Then €19,99/month starting Jan 2028", True),
        ("Due today $0.00 · Tax €5,00", False),
        ("Due today $0.00 · Tax €0,00", True),
    ]
    for text, expected in cases:
        verdict = guard.judge(text)
        assert verdict.safe is expected, (text, verdict)


def test_i18n_catalogues() -> None:
    """Both UI languages carry the same keys; the default is English."""
    from app import i18n

    assert i18n.DEFAULT == "en"
    assert set(i18n.STR["en"]) == set(i18n.STR["id"])
    assert i18n.resolve(None, None) == "en"
    assert i18n.resolve("id", None) == "id"
    assert i18n.resolve("xx", "id") == "id"
    assert i18n.resolve(None, "id") == "id"
    assert i18n.t("en", "accounts") == "Accounts"
    assert i18n.t("id", "accounts") == "Akun"
    assert i18n.t("en", "queue_pending", n=3) == "3 queued"
    assert i18n.noun("en", "cards", 1) == "1 card"
    assert i18n.noun("en", "cards", 4) == "4 cards"
    assert i18n.noun("id", "cards", 1) == "1 kartu"
    assert i18n.noun("id", "cards", 4) == "4 kartu"
    assert set(i18n.js_catalogue("id")) == set(i18n.JS_KEYS)


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print("---", "all passed" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
