#!/usr/bin/env bash
# Run the console on macOS or Ubuntu/Linux. Safe to re-run: use the venv if
# it exists, otherwise create it first. `./run.sh --check` only checks the environment.
set -euo pipefail
cd "$(dirname "$0")"

OS=$(uname -s)
say() { printf '%s\n' "$*" >&2; }
die() { printf 'failed: %s\n' "$*" >&2; exit 1; }

# ── Python 3.12+ ────────────────────────────────────────────────────────────
# pyproject requires >=3.12. 3.13 first because that is what macOS uses,
# then 3.12, which ships by default on Ubuntu 24.04.
find_python() {
  for cand in python3.13 python3.12; do
    if command -v "$cand" >/dev/null 2>&1; then command -v "$cand"; return 0; fi
  done
  if [ -x /opt/homebrew/bin/python3.13 ]; then echo /opt/homebrew/bin/python3.13; return 0; fi
  return 1
}
PY=$(find_python) || true
if [ -z "${PY:-}" ]; then
  case "$OS" in
    Darwin) die "python3.13 not found. Install it first: brew install python@3.13" ;;
    *)      die "python3.13/3.12 not found. Ubuntu 24.04: sudo apt install -y python3.12 python3.12-venv"
            ;;
  esac
fi

if [ ! -x .venv/bin/python ]; then
  say "creating .venv with $("$PY" -V 2>&1)"
  "$PY" -m venv .venv || die "venv failed — is the python3-venv package installed? (sudo apt install python3.12-venv)"
fi

if ! .venv/bin/python -c 'import fastapi, playwright, tzdata' >/dev/null 2>&1; then
  say "installing hash-pinned dependencies from requirements.lock"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --require-hashes --no-input -r requirements.lock
fi

# ── browser ─────────────────────────────────────────────────────────────────
browser_present() {
  [ "$OS" = "Darwin" ] && [ -d "/Applications/Google Chrome.app" ] && return 0
  for name in google-chrome google-chrome-stable chromium chromium-browser; do
    command -v "$name" >/dev/null 2>&1 && return 0
  done
  for path in /snap/bin/chromium /opt/google/chrome/chrome /usr/lib/chromium/chromium; do
    [ -x "$path" ] && return 0
  done
  return 1
}

if ! browser_present; then
  if [ "${GAI_SKIP_BROWSER_INSTALL:-0}" = "1" ]; then
    say "warning: no Chrome/Chromium, and GAI_SKIP_BROWSER_INSTALL=1 — launch will fail"
  else
    say "no system Chrome/Chromium — installing Playwright's bundled chromium"
    .venv/bin/python -m playwright install chromium \
      || say "warning: browser download failed; run manually: .venv/bin/python -m playwright install --with-deps chromium"
  fi
fi

# ── environment ─────────────────────────────────────────────────────────────
# PORT is the short name; GAI_BIND_PORT wins when both are set.
export GAI_BIND_PORT="${GAI_BIND_PORT:-${PORT:-8765}}"

if [ "$OS" = "Darwin" ]; then
  [ -x /usr/bin/security ] || say "warning: security CLI not found — secrets fall back to the file backend"
else
  command -v secret-tool >/dev/null 2>&1 \
    || say "note: libsecret-tools not found — secrets are stored in vault.json 0600 (not a keyring)"
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    say "note: no DISPLAY — headless browser. For headed: xvfb-run -a ./run.sh"
  fi
fi

if [ "${1:-}" = "--check" ]; then
  say "python   : $(.venv/bin/python -V 2>&1)"
  say "data dir : $(.venv/bin/python -c 'from app.config import settings; print(settings.data_dir)')"
  say "secret   : $(.venv/bin/python -c 'from app.secrets import describe; print(describe())')"
  say "browser  : $(.venv/bin/python -c 'from app.runner.browser import describe; print(describe())')"
  say "token    : $(.venv/bin/python -c 'from app.config import settings; print(settings.data_dir / "console.token")')"
  exit 0
fi

exec .venv/bin/python -m uvicorn app.main:app \
  --host "${GAI_BIND_HOST:-127.0.0.1}" --port "$GAI_BIND_PORT" --log-level warning
