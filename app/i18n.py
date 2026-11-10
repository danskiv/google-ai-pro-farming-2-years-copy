"""UI strings in two languages: English (default) and Indonesian.

The console page is server-rendered, so the language is resolved per request:
an explicit ``?lang=`` wins and is remembered in a cookie, then the cookie,
then the default. ``main.py`` passes a bound ``tr`` into the template and a
JSON subset to the page script; flash messages composed by POST handlers use
the same catalogue with the cookie's language.

Only console chrome lives here. Strings that match text on Google's pages
(needles in the flow and the guard) are functional, stay bilingual in place,
and are not part of this catalogue.
"""

from __future__ import annotations

DEFAULT = "en"
LANGS = ("en", "id")
COOKIE = "gai_lang"

STR: dict[str, dict[str, str]] = {
    "en": {
        # header / queue
        "queue_running": "running: {current}",
        "queue_pending": "{n} queued",
        "queue_idle": "idle",
        "accounts_one": "account",
        "accounts_many": "accounts",
        "cards_one": "card",
        "cards_many": "cards",
        "run_all": "run all",
        "stop": "stop",
        "close": "close",
        # accounts section
        "accounts": "Accounts",
        "accounts_hint": "Paste one per line: <code>email|password|totp-secret</code>. "
                         "The seed is stored in the vault (Keychain/Secret Service/0600 file); "
                         "only the code is shown.",
        "import": "Import",
        "seed_placeholder": "TOTP secret (optional)",
        "add": "Add",
        "col_card": "card",
        "col_status": "status",
        "show": "show",
        "hide": "hide",
        "show_password": "show password for {email}",
        "not_yet": "not yet",
        "run": "run",
        "delete": "delete",
        "confirm_delete_account": "delete {email}?",
        "empty_accounts": "no accounts yet",
        # cards section
        "cards": "Cards",
        "cards_hint": "Paste one per line: <code>number|MM/YY|CVC</code>. "
                      "Number &amp; CVC go to the vault (Keychain/Secret Service/0600 file); "
                      "only the last 4 digits are shown. Rotation round-robins to the "
                      "least recently used active card.",
        "card_number": "card number",
        "col_exp": "exp",
        "col_used": "used",
        "confirm_delete_card": "delete card {label}?",
        "confirm_kill_card": "disable {label}? accounts using it will be released.",
        "disable_card": "disable",
        "enable_card": "enable",
        "empty_cards": "no cards yet",
        # run log panel + page script
        "run_log": "Run log",
        "log_empty": "(no runs yet)",
        "js_disconnected": "connection lost",
        "js_done": "(done)",
        "js_failed": "(failed)",
        # flash messages composed by handlers
        "flash_accounts_imported": "{n} accounts imported",
        "flash_updated": "{n} updated",
        "flash_skipped": "{n} skipped",
        "flash_no_accounts": "0 accounts imported",
        "flash_account_deleted": "account deleted",
        "flash_cards_imported": "{n} cards imported",
        "flash_invalid_card": "invalid card format",
        "flash_card_status": "card status changed",
        "flash_card_deleted": "card deleted, accounts released",
        "flash_queued": "queued",
        "flash_already_queued": "already queued/running",
        "flash_run_all": "{n} accounts queued",
        "flash_stop": "stop sent",
    },
    "id": {
        "queue_running": "jalan: {current}",
        "queue_pending": "{n} antre",
        "queue_idle": "idle",
        "accounts_one": "akun",
        "accounts_many": "akun",
        "cards_one": "kartu",
        "cards_many": "kartu",
        "run_all": "jalankan semua",
        "stop": "stop",
        "close": "tutup",
        "accounts": "Akun",
        "accounts_hint": "Tempel satu per baris: <code>email|password|secret-totp</code>. "
                         "Seed disimpan di vault (Keychain/Secret Service/file 0600), "
                         "yang tampil hanya kodenya.",
        "import": "Impor",
        "seed_placeholder": "secret TOTP (opsional)",
        "add": "Tambah",
        "col_card": "kartu",
        "col_status": "status",
        "show": "lihat",
        "hide": "sembunyi",
        "show_password": "lihat password {email}",
        "not_yet": "belum",
        "run": "jalankan",
        "delete": "hapus",
        "confirm_delete_account": "hapus {email}?",
        "empty_accounts": "belum ada akun",
        "cards": "Kartu",
        "cards_hint": "Tempel satu per baris: <code>nomor|MM/YY|CVC</code>. "
                      "Nomor &amp; CVC ke vault (Keychain/Secret Service/file 0600); "
                      "yang tampil hanya 4 digit terakhir. Rotasi bergilir ke kartu "
                      "aktif yang paling lama tidak dipakai.",
        "card_number": "nomor kartu",
        "col_exp": "exp",
        "col_used": "dipakai",
        "confirm_delete_card": "hapus kartu {label}?",
        "confirm_kill_card": "matikan {label}? akun yang memakainya akan dilepas.",
        "disable_card": "matikan",
        "enable_card": "hidupkan",
        "empty_cards": "belum ada kartu",
        "run_log": "Log run",
        "log_empty": "(belum ada run)",
        "js_disconnected": "koneksi terputus",
        "js_done": "(selesai)",
        "js_failed": "(gagal)",
        "flash_accounts_imported": "{n} akun masuk",
        "flash_updated": "{n} diperbarui",
        "flash_skipped": "{n} dilewati",
        "flash_no_accounts": "0 akun masuk",
        "flash_account_deleted": "akun dihapus",
        "flash_cards_imported": "{n} kartu masuk",
        "flash_invalid_card": "format kartu tidak valid",
        "flash_card_status": "status kartu diubah",
        "flash_card_deleted": "kartu dihapus, akun dilepas",
        "flash_queued": "masuk antrean",
        "flash_already_queued": "sudah antre/running",
        "flash_run_all": "{n} akun masuk antrean",
        "flash_stop": "stop dikirim",
    },
}

# Keys the page script needs at runtime.
JS_KEYS = (
    "queue_running", "queue_pending", "queue_idle", "show", "hide",
    "js_disconnected", "js_done", "js_failed",
)


def resolve(explicit: str | None, cookie: str | None) -> str:
    """Pick the language: explicit choice, then cookie, then the default."""
    if explicit in LANGS:
        return explicit  # type: ignore[return-value]
    if cookie in LANGS:
        return cookie  # type: ignore[return-value]
    return DEFAULT


def t(lang: str, key: str, **fmt: object) -> str:
    text = STR.get(lang, STR[DEFAULT]).get(key) or STR[DEFAULT][key]
    return text.format(**fmt) if fmt else text


def noun(lang: str, base: str, n: int) -> str:
    """A counted noun phrase: "1 account" / "4 accounts" (Indonesian does
    not pluralise, so both forms coincide there)."""
    word = t(lang, f"{base}_{'one' if n == 1 else 'many'}")
    return f"{n} {word}"


def js_catalogue(lang: str) -> dict[str, str]:
    return {key: STR.get(lang, STR[DEFAULT]).get(key, STR[DEFAULT][key]) for key in JS_KEYS}
