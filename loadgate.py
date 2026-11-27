"""Keep the desks from opening more browsers than this server can hold.

Every desk imports this same gate. Slots live in one directory on the server,
so a GST run can see that Watchout already has a Chrome.

A browser desk holds slots for its whole run. Income tax and multi-login
Watchout may take several slots when nobody else is waiting and memory still
covers them. A job that cannot start yet waits. It is not dropped.

Defaults target the current KVM 8 box (~8 vCPU / 32 GB). Override with
ERUDORE_MAX_BROWSERS / ERUDORE_RESERVE_MB / ERUDORE_BROWSER_MB. On a smaller
host set ERUDORE_MAX_BROWSERS=2.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

RESERVE_MB = int(os.environ.get("ERUDORE_RESERVE_MB", "2500"))
BROWSER_MB = int(os.environ.get("ERUDORE_BROWSER_MB", "1600"))
MAX_BROWSERS = int(os.environ.get("ERUDORE_MAX_BROWSERS", "10"))
COMPRESSOR_MAX = int(os.environ.get("ERUDORE_COMPRESSOR_MAX", "4"))
POLL_SECONDS = float(os.environ.get("ERUDORE_LOAD_POLL", "5"))

def state_dir() -> Path:
    path = Path(os.environ.get("ERUDORE_LOAD_DIR", "/var/lib/erudore-load"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def available_mb() -> int:
    """Memory the kernel says is free for new work, in megabytes."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return 0
    return 0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_records(kind: str) -> list[dict]:
    folder = state_dir() / kind
    if not folder.is_dir():
        return []
    records = []
    for path in folder.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        record["_path"] = path
        if not _alive(int(record.get("pid") or 0)):
            path.unlink(missing_ok=True)
            continue
        records.append(record)
    return records


def _lock():
    import fcntl

    handle = (state_dir() / ".lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unlock(handle) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def decide(
    held: int,
    available: int,
    want: int,
    minimum: int,
    others_waiting: bool,
    older_waiting: bool,
) -> int:
    """How many browser slots to grant right now. Zero means wait."""
    if older_waiting:
        return 0
    ceiling = minimum if others_waiting else want
    free = MAX_BROWSERS - held
    for count in range(ceiling, 0, -1):
        if count > free:
            continue
        if available >= RESERVE_MB + BROWSER_MB * count:
            return count
    return 0


def snapshot() -> dict:
    """Live slots and waiters. Dead processes are dropped first."""
    handle = _lock()
    try:
        slots = _read_records("slots")
        waiting = _read_records("waiting")
    finally:
        _unlock(handle)
    return {
        "browsers": sum(int(item.get("browsers") or 0) for item in slots),
        "holders": [item.get("desk") for item in slots],
        "waiting": [item.get("desk") for item in waiting],
    }


def compressor_workers(requested: int) -> int:
    """How many PDFs to compress at once, given whatever browsers are up."""
    current = snapshot()
    if current["browsers"] or available_mb() < RESERVE_MB + 800:
        return 1
    return max(1, min(int(requested), COMPRESSOR_MAX))


@contextmanager
def borrow(desk: str, want: int = 1, on_wait=None):
    """Hold browser slots until the run finishes. Yields how many were granted.

    `want` may be several for income tax or multi-login Watchout. Extra
    slots are taken only when this desk is the only waiter and memory covers them.
    """
    want = max(1, min(int(want), MAX_BROWSERS))
    minimum = 1
    token = f"{os.getpid()}-{desk}-{uuid.uuid4().hex[:8]}"
    waiting = state_dir() / "waiting" / f"{token}.json"
    slot = state_dir() / "slots" / f"{token}.json"
    waiting.parent.mkdir(parents=True, exist_ok=True)
    slot.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    waiting.write_text(json.dumps({"pid": os.getpid(), "desk": desk, "since": now, "want": want}))
    told = False
    granted = 0
    try:
        while True:
            handle = _lock()
            try:
                slots = _read_records("slots")
                waiters = _read_records("waiting")
                held = sum(int(item.get("browsers") or 0) for item in slots)
                mine = next((item for item in waiters if item.get("_path") == waiting), None)
                since = float(mine.get("since") if mine else now)
                others = [item for item in waiters if item.get("_path") != waiting]
                older = [item for item in others if float(item.get("since") or 0) < since]
                granted = decide(
                    held,
                    available_mb(),
                    want,
                    minimum,
                    others_waiting=bool(others),
                    older_waiting=bool(older),
                )
                if granted:
                    slot.write_text(json.dumps({"pid": os.getpid(), "desk": desk, "browsers": granted, "since": time.time()}))
                    waiting.unlink(missing_ok=True)
                    break
            finally:
                _unlock(handle)
            if on_wait and not told:
                on_wait()
                told = True
            time.sleep(float(os.environ.get("ERUDORE_LOAD_POLL", str(POLL_SECONDS))))
        yield granted
    finally:
        waiting.unlink(missing_ok=True)
        slot.unlink(missing_ok=True)
