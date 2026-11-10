"""App factory. Boot order: dirs, token, migrations, routes."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from urllib.parse import quote

from . import db, i18n, secrets, vault
from .config import settings
from .runner import browser, queue
from .security import COOKIE_NAME, require_same_origin, require_token

log = logging.getLogger("gai")


def _lang(request: Request) -> str:
    """Language for handler-composed messages: the UI cookie, else English."""
    return i18n.resolve(None, request.cookies.get(i18n.COOKIE))


def _to(dest: str = "/", **params: str) -> RedirectResponse:
    """303 back to the page with the message URL-encoded.

    Notes carry user input (an email, a card brand), so they must be quoted —
    an unescaped `&` or `#` in a malformed import line would truncate the query
    string and drop the message entirely.
    """
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items() if v)
    return RedirectResponse(f"{dest}?{query}" if query else dest, status_code=303)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()
    token = settings.load_or_create_token()
    await db.migrate()
    # A process that died mid-run leaves its account at 'running' forever;
    # no run is alive at boot, so reset them.
    await db.execute("UPDATE accounts SET status = 'pending' WHERE status = 'running'")
    queue.runner.start()
    log.warning("secret backend: %s", secrets.describe())
    log.warning("browser: %s", browser.describe())
    log.warning("console: http://%s:%d/?t=%s", settings.bind_host, settings.bind_port, token)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GaiConsole", lifespan=lifespan, docs_url=None, redoc_url=None)
    guard = [Depends(require_token)]
    # POSTs also check Origin: the token cookie rides along on cross-site
    # form POSTs, so the token alone does not stop CSRF.
    post_guard = [Depends(require_token), Depends(require_same_origin)]

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "secret_backend": secrets.backend(),
                "browser": browser.describe(),
            }
        )

    @app.get("/", dependencies=guard)
    async def index(request: Request, msg: str = "", err: str = "", lang: str = ""):
        lang = i18n.resolve(lang or None, request.cookies.get(i18n.COOKIE))
        if getattr(request.state, "set_cookie_token", None):
            # First visit via ?t=: trade the URL token for a cookie, then
            # redirect to a clean URL so the token does not linger in the
            # address bar, history, or proxy logs.
            parts = [(k, v) for k, v in (("msg", msg), ("err", err)) if v]
            if "lang" in request.query_params and lang in i18n.LANGS:
                parts.append(("lang", lang))
            dest = "/?" + "&".join(f"{k}={quote(v)}" for k, v in parts)
            resp = RedirectResponse(dest if len(dest) > 2 else "/", status_code=303)
            resp.set_cookie(COOKIE_NAME, request.state.set_cookie_token,
                            httponly=True, samesite="strict")
            if parts and parts[-1][0] == "lang":
                resp.set_cookie(i18n.COOKIE, lang, max_age=31536000,
                                httponly=False, samesite="strict")
            return resp
        resp = templates.TemplateResponse(
            request,
            "index.html",
            {
                "accounts": await vault.accounts_view(lang),
                "cards": await vault.cards_view(),
                "msg": msg,
                "err": err,
                "pending": queue.runner.pending,
                "current": queue.runner.current,
                "lang": lang,
                "tr": lambda key, **fmt: i18n.t(lang, key, **fmt),
                "noun": lambda base, n: i18n.noun(lang, base, n),
                "js_i18n": i18n.js_catalogue(lang),
            },
        )
        if lang in i18n.LANGS:
            resp.set_cookie(i18n.COOKIE, lang, max_age=31536000, httponly=False, samesite="strict")
        return resp

    @app.get("/api/codes", dependencies=guard)
    async def codes() -> JSONResponse:
        rows = await db.fetch_all("SELECT id, totp_ref FROM accounts WHERE totp_ref != ''")
        out: dict[str, dict[str, object]] = {}
        for row in rows:
            seed = vault.secret_get(row["totp_ref"])
            out[row["id"]] = {"code": vault.totp.code(seed), "left": vault.totp.seconds_left()}
        return JSONResponse(out)

    @app.get("/api/secret/{acc_id}", dependencies=guard)
    async def secret(acc_id: str) -> JSONResponse:
        row = await db.fetch_one("SELECT secret_ref FROM accounts WHERE id = ?", (acc_id,))
        if not row or not row["secret_ref"]:
            return JSONResponse({"password": ""}, status_code=404)
        return JSONResponse({"password": vault.secret_get(row["secret_ref"])})

    @app.post("/accounts/import", dependencies=post_guard)
    async def accounts_import(request: Request, blob: str = Form(default="")):
        lang = _lang(request)
        notes = await vault.import_accounts(blob)
        added = notes.count("added")
        updated = notes.count("updated")
        skipped = len(notes) - added - updated
        parts = []
        if added:
            parts.append(i18n.tn(lang, "flash_accounts_imported", added))
        if updated:
            parts.append(i18n.t(lang, "flash_updated", n=updated))
        if skipped:
            parts.append(i18n.t(lang, "flash_skipped", n=skipped))
        return _to(msg=", ".join(parts) if parts else i18n.t(lang, "flash_no_accounts"))

    @app.post("/accounts/add", dependencies=post_guard)
    async def accounts_add(request: Request, email: str = Form(...), password: str = Form(...), seed: str = Form(default="")):
        lang = _lang(request)
        _, status = await vault.add_account(email, password, seed)
        if status == "added":
            return _to(msg=i18n.t(lang, "flash_account_added", email=email))
        if status == "updated":
            return _to(msg=i18n.t(lang, "flash_account_updated", email=email))
        return _to(err=i18n.t(lang, "flash_invalid_email"))

    @app.post("/accounts/{acc_id}/delete", dependencies=post_guard)
    async def accounts_delete(request: Request, acc_id: str):
        await vault.delete_account(acc_id)
        return _to(msg=i18n.t(_lang(request), "flash_account_deleted"))

    @app.post("/cards/import", dependencies=post_guard)
    async def cards_import(request: Request, blob: str = Form(default="")):
        lang = _lang(request)
        notes = await vault.import_cards(blob)
        added = notes.count("added")
        unusable = len(notes) - added
        text = i18n.tn(lang, "flash_cards_imported", added)
        if unusable:
            text += ", " + i18n.t(lang, "flash_skipped", n=unusable)
        return _to(msg=text)

    @app.post("/cards/add", dependencies=post_guard)
    async def cards_add(request: Request, number: str = Form(...), exp: str = Form(...), cvc: str = Form(...)):
        parsed = vault.parse_card(f"{number}|{exp}|{cvc}")
        if not parsed:
            return _to(err=i18n.t(_lang(request), "flash_invalid_card"))
        lang = _lang(request)
        _, status, label = await vault.add_card(*parsed)
        if status == "added":
            return _to(msg=i18n.t(lang, "flash_card_added", label=label))
        if status == "exists":
            return _to(msg=i18n.t(lang, "flash_card_exists", label=label))
        return _to(err=i18n.t(lang, "flash_invalid_card"))

    @app.post("/cards/{card_id}/status", dependencies=post_guard)
    async def cards_status(request: Request, card_id: str, dead: int = Form(...)):
        await vault.kill_card(card_id, dead=bool(dead))
        return _to(msg=i18n.t(_lang(request), "flash_card_status"))

    @app.post("/cards/{card_id}/delete", dependencies=post_guard)
    async def cards_delete(request: Request, card_id: str):
        await vault.delete_card(card_id)
        return _to(msg=i18n.t(_lang(request), "flash_card_deleted"))

    # ── runs ────────────────────────────────────────────────────────────────
    @app.post("/accounts/{acc_id}/run", dependencies=post_guard)
    async def accounts_run(request: Request, acc_id: str):
        ok = await queue.runner.submit(acc_id)
        lang = _lang(request)
        return _to(msg=i18n.t(lang, "flash_queued") if ok
                   else i18n.t(lang, "flash_already_queued"))

    @app.post("/queue/run-all", dependencies=post_guard)
    async def queue_run_all(request: Request):
        rows = await db.fetch_all(
            "SELECT a.id FROM accounts a WHERE a.status != 'subscribed' AND a.card_id != ''"
            " AND a.totp_ref != ''"
            " AND EXISTS (SELECT 1 FROM cards c WHERE c.id = a.card_id AND c.status = 'active')"
            " ORDER BY a.created_at"
        )
        n = await queue.runner.submit_many([r["id"] for r in rows])
        return _to(msg=i18n.tn(_lang(request), "flash_run_all", n))

    @app.post("/queue/stop", dependencies=post_guard)
    async def queue_stop(request: Request):
        await queue.runner.stop()
        return _to(msg=i18n.t(_lang(request), "flash_stop"))

    @app.get("/api/queue", dependencies=guard)
    async def api_queue() -> JSONResponse:
        return JSONResponse({"current": queue.runner.current, "pending": queue.runner.pending})

    @app.get("/api/log/{acc_id}", dependencies=guard)
    async def api_log(acc_id: str) -> JSONResponse:
        path = queue.latest_log(acc_id)
        log = queue.tail(path) if path else ""
        return JSONResponse({"log": log, "size": len(log)})

    @app.get("/api/state", dependencies=guard)
    async def api_state(request: Request) -> JSONResponse:
        """One request per tick: OTP codes, queue, account rows and the live log.

        The page polls this instead of three separate endpoints, so a refresh
        costs one round trip and every panel is guaranteed to be from the same
        snapshot.
        """
        rows = await db.fetch_all("SELECT id, email, totp_ref, status, step, step_message FROM accounts")
        codes: dict[str, dict[str, object]] = {}
        for row in rows:
            if row["totp_ref"]:
                seed = vault.secret_get(row["totp_ref"])
                if seed:
                    codes[row["id"]] = {"code": vault.totp.code(seed), "left": vault.totp.seconds_left()}

        current = queue.runner.current
        log, size = "", 0
        if current:
            path = queue.latest_log(current)
            if path:
                log = queue.tail(path, lines=200)
                size = len(log)

        return JSONResponse({
            "codes": codes,
            "current": current,
            "pending": queue.runner.pending,
            "accounts": {
                r["id"]: {"status": r["status"], "step": r["step"], "message": i18n.t_or(_lang(request), r["step_message"])}
                for r in rows
            },
            "log": log,
            "logSize": size,
        })

    return app


app = create_app()
