"""Serial run queue.

One account at a time: parallel Google logins from one egress burn reputation
faster than a queue. Each run is a subprocess, so a hung browser cannot block the
web server and a crash is contained; stdout/stderr go to a per-run log file that
the UI tails.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

from .. import db
from ..config import settings

BASE = Path(__file__).resolve().parents[2]
SCRIPT = BASE / "tasks" / "subscribe_flow.py"


class Runner:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._worker: asyncio.Task | None = None
        self.current: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.stopping = False

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            acc_id = await self._queue.get()
            try:
                await self._run(acc_id)
            except Exception as exc:  # noqa: BLE001
                await db.execute("UPDATE accounts SET status = 'failed' WHERE id = ?", (acc_id,))
                print(f"queue error {acc_id}: {type(exc).__name__}: {exc}", flush=True)
            finally:
                self._queued.discard(acc_id)
                self._queue.task_done()

    # ── api ─────────────────────────────────────────────────────────────────
    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def submit(self, acc_id: str) -> bool:
        if acc_id == self.current or acc_id in self._queued:
            return False
        self._queued.add(acc_id)
        await self._queue.put(acc_id)
        return True

    async def submit_many(self, acc_ids: list[str]) -> int:
        n = 0
        for acc_id in acc_ids:
            if await self.submit(acc_id):
                n += 1
        return n

    async def stop(self) -> None:
        self.stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                # A terminate() that does not take effect is not enough: on
                # Windows it only kills the runner (the Playwright driver +
                # Chrome become orphans and lock the profile), and on POSIX a
                # stubborn child can be left behind too. Escalate: kill the
                # entire process tree.
                await asyncio.to_thread(_kill_tree, self.proc.pid)
                await self.proc.wait()

    # ── run ─────────────────────────────────────────────────────────────────
    async def _run(self, acc_id: str) -> None:
        row = await db.fetch_one("SELECT email, step FROM accounts WHERE id = ?", (acc_id,))
        if not row:
            return
        email = row["email"]
        start = max(1, min(3, int(row["step"] or 0) or 1))
        if not SCRIPT.exists():
            await db.execute("UPDATE accounts SET status = 'failed' WHERE id = ?", (acc_id,))
            print(f"script not found: {SCRIPT}", flush=True)
            return

        self.current = acc_id
        self.stopping = False
        # Do not zero the step: the stored value is the resume point if the
        # process dies midway; the run computes its own starting point.
        await db.execute("UPDATE accounts SET status = 'running' WHERE id = ?", (acc_id,))

        # A hard-killed Chrome leaves a lock beside the profile and the next launch
        # dies with "Opening in existing browser session".
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            try:
                (settings.profiles_dir / f"profile_{acc_id}" / lock).unlink(missing_ok=True)
            except OSError:
                pass

        log_path = settings.artifacts_dir / f"run_{acc_id}_{int(time.time())}.log"
        timed_out = False
        with open(log_path, "wb") as fh:
            # Force UTF-8 in the subprocess: on Windows, stdout to a file uses
            # the local codepage (cp1252) and the flow log is full of non-ASCII characters.
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, str(SCRIPT), email, "--from", str(start),
                cwd=str(BASE), stdout=fh, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=settings.run_timeout)
            except asyncio.TimeoutError:
                # A hung run must not hold the queue hostage forever: kill its
                # process tree (Chrome sits under the runner) and mark it
                # failed — not cancelled, that is for a user stop.
                timed_out = True
                await asyncio.to_thread(_kill_tree, self.proc.pid)
                await self.proc.wait()

        if timed_out:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n[queue] run exceeded the {settings.run_timeout}s limit"
                         " — process stopped, account marked failed\n")

        code = self.proc.returncode if self.proc else -1
        self.proc = None
        if timed_out:
            status = "failed"
        elif code == 0:
            status = "subscribed"
        elif self.stopping:
            status = "cancelled"
        else:
            status = "failed"
        await db.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, acc_id))
        self.current = None


def _kill_tree(pid: int) -> None:
    """Kill a process and its children, recursively (stop/timeout path)."""
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True)
    procs.append(parent)
    for proc in procs:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


runner = Runner()

_ACC_ID = re.compile(r"^acc_[0-9a-f]{12}$")


def latest_log(acc_id: str) -> Path | None:
    # A raw acc_id from the URL must not become a glob pattern: ".." or "*"
    # in it could point at files outside this account's log directory.
    if not _ACC_ID.match(acc_id):
        return None
    files = sorted(settings.artifacts_dir.glob(f"run_{acc_id}_*.log"))
    return files[-1] if files else None


def tail(path: Path, lines: int = 60) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])
