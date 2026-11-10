# GaiConsole

A local console (`127.0.0.1`) for multi-account browser automation. Runs on **macOS**,
**Ubuntu/Linux**, and **Windows**: a real Chrome if present, a system Chromium if
not, and Playwright's bundled browser as the last resort.

| platform | status |
|---|---|
| Ubuntu / Linux | tested: Python 3.12.3 aarch64, system + bundled Chromium, `file` and `secret-tool` backends |
| macOS | the legacy path is kept as-is (Keychain, `/Applications/Google Chrome.app`) |
| Windows | new: Python 3.12/3.13, standard-install Chrome/Chromium or bundled, `file` backend in `%LOCALAPPDATA%` — the code path is verified via platform simulation, not yet tested on a real Windows machine |

## Security posture

Direct dependencies are kept to a minimum — **9 packages**, all from
canonical repositories:

| package | maintainer |
|---|---|
| fastapi, uvicorn | Encode / `fastapi/fastapi` |
| python-multipart | `Kludex/python-multipart` (import forms) |
| jinja2 | Pallets |
| pydantic, pydantic-settings | `pydantic/*` |
| aiosqlite | omnilib |
| psutil | Giampaolo Rodola |
| playwright | Microsoft |

Deliberately **not** used:

- `keyring` → the OS's built-in CLI is used instead. macOS: `security` (Keychain), Linux:
  `secret-tool` (libsecret → gnome-keyring/KWallet over D-Bus). Secrets enter
  via stdin; only attributes appear in argv, so values never show up in
  `ps`. When no live keyring exists (headless server, container, SSH without a
  desktop session), the backend falls back to a **0600 file** inside the
  0700 data dir — weaker than a keyring, which is why `run.sh --check`,
  `/healthz`, and the boot log always state the backend that is actually used.
  Windows has no built-in keyring CLI, so there the backend is always **file**
  in `%LOCALAPPDATA%\GaiConsole`; POSIX modes do not apply, protection
  is the user profile ACL (only the owning user + admins can read
  their own LocalAppData), and `describe()` says so plainly.
- `pyotp` → RFC 6238 TOTP written with stdlib `hmac`+`base64`, tested
  against the RFC 6238 Appendix B vectors (`tests/selftest.py`).
- `email-validator` → a simple regex, sufficient for Google addresses.
- `uvicorn[standard]` → plain `uvicorn`, dropping httptools/uvloop/watchfiles/
  websockets/pyyaml.

Other hardening:

- `requirements.lock` is fully pinned **with sha256**, installed via
  `pip install --require-hashes`. The lock contains hashes for **linux
  (manylinux/musllinux x86_64+aarch64), darwin (arm64+x86_64), and windows
  (win_amd64/win_arm64)** all at once —
  one lock, three OSes: pip picks the artifact matching the host, then verifies
  its hash. Each package carries a `# files:` comment stating its coverage.
  Regeneration: `tools/make_lock.py` (from the venv), `--from-lock` (without a venv),
  `--verify` (check the lock against PyPI).
- The console requires a 43-char token (`?t=` → cookie), compared in constant time.
  Without the token, another local process cannot read the account list.
- Data dir `0700`; `db.sqlite3`, `console.token`, and `vault.json` are `0600`
  (on Windows all three are protected by the user profile ACL, not POSIX modes).
- Passwords, TOTP seeds, card numbers, and CVCs live **only** in the vault (Keychain /
  Secret Service / 0600 file). The `accounts` table stores handles
  (`secret_ref`, `totp_ref`), not values.
- `/api` never returns a full card number or CVC.

## Requirements

- **Google Chrome** or **Chromium**. On Linux with neither: `run.sh` installs
  Playwright's bundled Chromium (≈120 MB, once; cancel with
  `GAI_SKIP_BROWSER_INSTALL=1`).
- **US** egress (VPN). The flow waits up to 120 seconds, then stops if not US.
- Python 3.12+ (`python3.13` first, then `python3.12`).
- An account stock (`email|password|TOTP seed`) and cards (`number|MM/YY|CVC`) — both
  are entered via the UI, and their values are stored in the vault.

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv libsecret-tools git
# optional — a real keyring in a desktop session:
#   sudo apt install -y gnome-keyring
# optional — a headless server that wants a headed browser/screenshots:
#   sudo apt install -y xvfb
git clone https://github.com/danskiv/google-ai-pro-farming-2-years-copy
cd google-ai-pro-farming-2-years-copy
./run.sh --check   # python, data dir, secret backend, browser (creates the venv first if missing)
./run.sh
```

Ubuntu 24.04 already ships Python 3.12. For 22.04 (which ships 3.10, below
`requires-python`), get 3.12 from deadsnakes:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.12 python3.12-venv
```

Real Chrome (optional, GaiConsole finds it itself):

```bash
wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
  | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
sudo apt install -y google-chrome-stable   # after adding the Google repo
```

`libsecret-tools` is only useful when a live Secret Service exists. On a
headless server without a desktop session, `secret-tool` exists but every call fails
(`Cannot autolaunch D-Bus without X11 $DISPLAY`, or
`Object does not exist at path /org/freedesktop/secrets/collection/login`).
GaiConsole detects that via a round-trip probe and moves to the file backend
rather than failing when saving the first password.

### Windows

Prerequisites: **Python 3.12+** from [python.org](https://www.python.org/downloads/)
(check *py launcher* during install) and **Google Chrome** (or Chromium).
No Git Bash, WSL, or Xvfb needed — the browser runs headed on the native
Windows desktop, and there is no keyring CLI, so secrets use the file backend (see
Security posture).

```powershell
git clone https://github.com/danskiv/google-ai-pro-farming-2-years-copy
cd google-ai-pro-farming-2-years-copy
.\run.ps1 --check   # python, data dir, secret backend, browser (creates the venv first if missing)
.\run.ps1
```

`run.ps1` is the counterpart of `run.sh`: it creates `.venv`, installs the
hash-pinned dependencies from the same `requirements.lock`, then serves the console on
`127.0.0.1:8765` (`$env:PORT=9000; .\run.ps1` for another port). `run.bat`
is only a thin wrapper that calls `run.ps1` (with an ExecutionPolicy
bypass scoped to that process) for those who prefer clicking/Command
Prompt. Without a system Chrome/Chromium, `run.ps1` installs Playwright's bundled
Chromium, same as on Linux (`$env:GAI_SKIP_BROWSER_INSTALL=1` to
forbid that). Windows data dir: `%LOCALAPPDATA%\GaiConsole`.

Running as a Windows service (Task Scheduler / NSSM) is not wrapped by
this repo; run `run.ps1` from a logged-in user session so the headed browser
has a desktop.

## Running

```bash
./run.sh
```

`run.sh` creates the venv, installs the hash-pinned dependencies if missing, then
serves the console on `127.0.0.1:8765` (`PORT=9000 ./run.sh` for another port).
Open:

```
http://127.0.0.1:8765/?t=<token contents>
```

| OS | token location |
|---|---|
| macOS | `~/Library/Application Support/GaiConsole/console.token` |
| Linux | `~/.local/share/GaiConsole/console.token` (follows `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\GaiConsole\console.token` |

The token is also printed in the boot log. Without the token, the account list cannot be read.

As a service (Ubuntu, without login):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/gai-console.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gai-console
loginctl enable-linger "$USER"          # stays alive after logout
journalctl --user -u gai-console -f
```

That unit uses `WorkingDirectory=%h/google-ai-pro-farming-2-years-copy` —
adjust it if the repo is cloned under a different name.

## Environment variables

All `GAI_`-prefixed variables are read by `app/config.py`; `PORT` and
`GAI_SKIP_BROWSER_INSTALL` are read by `run.sh` / `run.ps1`.

| variable | meaning |
|---|---|
| `PORT` | console port (default 8765) |
| `GAI_BIND_HOST` | bind address (default `127.0.0.1` — do not expose it to the LAN) |
| `GAI_BIND_PORT` | same as `PORT`, wins if both are set |
| `GAI_DATA_DIR` | relocate the data dir (default: the per-OS path above) |
| `GAI_SECRET_BACKEND` | `auto` (default) / `security` / `secret-tool` / `file` |
| `GAI_BROWSER_CHANNEL` | `auto` (default) / `chrome` / `chromium` / `bundled` |
| `GAI_BROWSER_EXECUTABLE` | path to a specific browser binary, e.g. snap Chromium |
| `GAI_HEADLESS` | `auto` (default) / `1` / `0` |
| `GAI_NO_SANDBOX` | `1` if Chromium cannot use its sandbox (containers) |
| `GAI_BROWSER_PROXY` | `host:port` or `http://user:pass@host:port` — a Chromium-only proxy (restricted egress, see the next section) |
| `GAI_IGNORE_CERT_ERRORS` | `1` if the egress proxy re-signs TLS with its own CA |
| `GAI_MIRROR_CLIENTS2` | `1` if the egress blocks `clients2.google.com` — the payments script is fetched from a Google Translate mirror host |
| `GAI_MAX_CONCURRENT` | number of parallel runs (default 1 — do not raise without measuring) |
| `GAI_RUN_TIMEOUT` | per-run time limit in seconds (default 900) |
| `GAI_SKIP_BROWSER_INSTALL` | `1` to forbid `run.sh`/`run.ps1` from downloading Chromium |

`auto` for the browser means: Chrome in `/Applications` (macOS) or
`google-chrome`/`google-chrome-stable` in PATH → `channel="chrome"`; a system
Chromium/snap/`/opt` → `executable_path`; on Windows, `chrome.exe` is searched in
the standard install locations (`%ProgramFiles%`, `%ProgramFiles(x86)%`,
`%LocalAppData%`) → `executable_path`; if neither exists → Playwright's bundled
Chromium (with automatic fallback if the requested channel is not found).

`auto` for headless means: a `DISPLAY`/`WAYLAND_DISPLAY` exists → headed, none
→ headless. A headed request on a display-less machine is **diverted** to headless
with a warning in the log, not a crash. To force headed: `GAI_HEADLESS=0`
(and provide `xvfb-run`).

A secret backend that is explicitly selected but unusable will **fail
hard** on write (`RuntimeError: secret-tool write failed…`) rather than silently
downgrading to a file — so there is no false impression of protection.

## Restricted egress (VM / sandbox / corporate proxy)

Some VMs and sandboxes route all outbound traffic through a single HTTP proxy
with its own policies. Its three typical symptoms, and the cures:

1. **Chromium cannot open any page** (`ERR_SSL_PROTOCOL_ERROR`,
   or the CONNECT tunnel is refused with 403) while `curl` is fine. Such a proxy
   often only accepts certain clients. Run a small local relay that
   opens CONNECT from an ordinary process and forwards it, and point only
   Chromium at that relay:

   ```
   GAI_BROWSER_PROXY=127.0.0.1:18080
   ```

2. **Certificates are rejected on every site.** The proxy re-signs TLS
   with an internal CA that Chromium does not know:

   ```
   GAI_IGNORE_CERT_ERRORS=1
   ```

3. **The checkout dialog spins on “Loading…” forever.** Check the network log:
   `clients2.google.com` fails (`ERR_TUNNEL_CONNECTION_FAILED` / 403
   `policy_denied`). That host serves the Google Payments scripts
   (`gr/gr_sync.js`, `gr/gr_full_*.js`) that the dialog must load. The exact
   same files are served publicly by the Google Translate proxy host
   (`clients2-google-com.translate.goog`), so mirror mode fetches the
   bytes from there and hands them to the dialog:

   ```
   GAI_MIRROR_CLIENTS2=1
   ```

All three are opt-in and change nothing when unset. The combination
proven on a headless VM: a local relay + `GAI_BROWSER_PROXY` +
`GAI_IGNORE_CERT_ERRORS=1` + `GAI_MIRROR_CLIENTS2=1` + a **headed** browser on
Xvfb (`DISPLAY=:99`, `GAI_HEADLESS=0`) — headless mode is rejected by Google at the
login step (`signin/rejected`).

## Verification

```bash
.venv/bin/python tests/selftest.py     # 28 tests: TOTP RFC, DB, vault, guard, browser, lock, i18n
python3 tools/make_lock.py --verify    # lock hashes vs PyPI, without a venv
./run.sh --check                       # python, data dir, secret backend, browser
curl -s localhost:8765/healthz         # {"ok":true,"secret_backend":…,"browser":…}
```

`/healthz` deliberately names the live backend and browser: that is how you
make sure secrets really are in the keyring and not in a file.

## Troubleshooting

| symptom | cause & action |
|---|---|
| `THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE` | the lock was created on another platform. Regenerate: `python3 tools/make_lock.py --from-lock` |
| `secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY` | there is no Secret Service. Leave `auto` (it falls back to a 0600 file) or run in a desktop session |
| `Object does not exist at path /org/freedesktop/secrets/collection/login` | the login collection has not been created; `gnome-keyring` needs a prompter with a display. Use a desktop session or `xvfb-run` |
| `Missing X server or $DISPLAY` | `GAI_HEADLESS=1`, or `xvfb-run -a ./run.sh` |
| `Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome` | GaiConsole automatically falls back to the bundled Chromium; if the bundled one is missing too: `.venv/bin/python -m playwright install chromium` |
| `No usable sandbox!` / `clone3` | a container without user namespaces: `GAI_NO_SANDBOX=1 ./run.sh` |
| the browser dies when opening many tabs | a small `/dev/shm` — already handled with `--disable-dev-shm-usage` on Linux |
| `token required` (401) | take `?t=` from `console.token` (locations in the table above) or from the boot log |
| the checkout dialog keeps showing “Loading…” | `clients2.google.com` is blocked by the egress — enable `GAI_MIRROR_CLIENTS2=1` (Restricted egress section) |
| the plan page only contains “Get discount” buttons | a new page variant; for layer 1 the flow already clicks the rightmost “Get discount” button (the Google AI Pro card) |
| saving the card stalls, the dialog does not return | possibly a reCAPTCHA at the Save card step. Re-run (the same profile often passes without a challenge), or supervise the run and solve the challenge in the browser window. The challenge can be multi-round (images change each round) — keep answering until it completes |
| card removal stalls, no button is clicked | the payments page follows the account's language (e.g. Indonesian: “Edit metode pembayaran”, “Verifikasi diri Anda”); the card-removal text matching is already bilingual (EN+ID) |
| login `signin/rejected` | Google rejects headless mode — use headed: `GAI_HEADLESS=0` + Xvfb on the server |

## How to use

1. Import cards: `number|MM/YY|CVC` (one per line or one at a time).
2. Import accounts: `email|password|totp_seed` (same format).
3. Press **run** on an account, or **run-all** for a sequential queue.

Per-account flow: login → discovery of the running term → layers 1–4 → date guard →
cancel → remove the card from the account (the card stays in the vault for the next account).

Discovery picks the starting point itself: 0 months → layer 1, 12 → layer 2, 18 → layer 3,
24 → only tries layer 4, then cancels. So an interrupted run is safe to repeat.

Two points still need a human hand on the phone, and both are waited on (5 minutes for login,
300 seconds for card removal): the device prompt at login, and the verification approval when
removing the card. If not approved, the subscription stays active for 24 months and the cancel
still goes through — only the card is left behind on the account.

On a display-less server, those two points still work (the prompts are on the phone). If you
need to see the browser screen: `xvfb-run -a ./run.sh`; logs and screenshots are still
saved in `artifacts/`.

## Structure

```
app/
  main.py          page routes + queue (+ /healthz: backend & browser)
  config.py        settings, per-OS data dir, console token
  db.py            SQLite schema + idempotent migrations
  vault.py         accounts & cards (values in the vault)
  secrets.py       secret backends: security / secret-tool / 0600 file (Windows: file, user profile ACL)
  totp.py          stdlib TOTP
  egress.py        egress country check
  runner/          browser (channel/headless resolution), navigation, queue
  sites/google_one/
    login.py       login + TOTP + wait for the device prompt
    checkout.py    hook, arm, read the dialog
    discover.py    term → layer map
    tokens.py      per-layer tokens
    hook.js        request rewriting
tasks/
  subscribe_flow.py  main flow (login → layers → cancel → remove card)
  cancel.py          manual cancel
  probe_pay.py       payments page checker
deploy/
  gai-console.service  systemd --user unit for Ubuntu
tests/selftest.py
tools/make_lock.py     multi-platform lock (linux+darwin+windows) from the PyPI JSON API
```
