"""Append-only record of a desk run. Passwords are never written."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"done", "error", "cancelled"}
_ITEM_KEYS = (
    "row",
    "reference",
    "portal",
    "status",
    "name",
    "original_filename",
    "label",
    "query",
    "section",
    "message",
)
_SECRET = ("password", "secret", "token", "captcha")


def _clean(value, limit: int = 180) -> str:
    return " ".join(str(value).split())[:limit]


def _items(current: dict) -> list[dict]:
    rows = None
    for key in ("rows", "entries", "files"):
        value = current.get(key)
        if isinstance(value, list):
            rows = value
            break
    if not rows:
        return []
    kept: list[dict] = []
    for row in rows[:40]:
        if not isinstance(row, dict):
            continue
        item = {}
        for key in _ITEM_KEYS:
            if any(word in key.lower() for word in _SECRET):
                continue
            raw = row.get(key)
            if raw not in (None, ""):
                item[key] = _clean(raw)
        if item:
            kept.append(item)
    return kept


def log_run(app: str, event: str, data_dir: Path, **fields) -> None:
    record = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app": app,
        "event": event,
    }
    for key, value in fields.items():
        if value in (None, "") or any(word in key.lower() for word in _SECRET):
            continue
        if isinstance(value, (dict, list)):
            record[key] = value
        else:
            record[key] = _clean(value, 300)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    targets = [Path(data_dir) / "run-log.jsonl"]
    shared = os.environ.get("ERUDORE_RUN_LOG", "/var/log/erudore/runs.jsonl")
    if shared:
        targets.append(Path(shared))
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            continue


def note_status(app: str, data_dir: Path, job_id: str, previous_status: str | None, current: dict) -> None:
    """Log a run when it is queued, and again when it finishes."""
    try:
        status = current.get("status")
        if not status or status == previous_status:
            return
        if status == "queued":
            event = "started"
        elif status in TERMINAL:
            event = "finished"
        else:
            return
        fields = {
            "job_id": job_id,
            "status": status,
            "employee_id": current.get("employee_id"),
            "user_id": current.get("user_id"),
            "message": current.get("message"),
            "detail": current.get("detail"),
            "query_count": current.get("query_count"),
            "done_count": current.get("done_count"),
            "error_count": current.get("error_count"),
            "searches_total": current.get("searches_total"),
            "searches_done": current.get("searches_done"),
            "batch_size": current.get("batch_size"),
        }
        items = _items(current)
        if items:
            fields["items"] = items
        log_run(app, event, data_dir, **fields)
    except Exception:
        return
