"""File-queue worker for PDF compressor jobs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from pdf_compressor.core import CancelledError, compress_pdf

APP_ROOT = Path(__file__).resolve().parent
JOBS_DIR = APP_ROOT / "data" / "jobs"
QUEUE_DIR = APP_ROOT / "data" / "queue"
HISTORY_DIR = APP_ROOT / "data" / "history"
POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "0.75"))
MAX_DISK_MB = int(os.environ.get("MAX_DISK_MB", "4000"))
SPLIT_PAGES = int(os.environ.get("SPLIT_PAGES", "80"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
log = logging.getLogger("pdf-worker")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_status(job_id: str, **fields) -> dict:
    path = _job_dir(job_id) / "status.json"
    data = _read_json(path) if path.exists() else {}
    previous_status = data.get("status")
    data.update(fields)
    data["updated_at"] = _utcnow()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    from runlog import note_status

    note_status("plugin", JOBS_DIR.parent, job_id, previous_status, data)
    return data


def _is_cancelled(job_id: str) -> bool:
    st = _read_json(_job_dir(job_id) / "status.json")
    return bool(st.get("cancel_requested"))


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def enforce_disk_quota() -> int:
    """Keep data/jobs under MAX_DISK_MB (4 GB default). Delete oldest finished first."""
    if not JOBS_DIR.exists():
        return 0
    limit = MAX_DISK_MB * 1024 * 1024
    size = _dir_size_bytes(JOBS_DIR)
    if size <= limit:
        return 0

    jobs: list[tuple[int, float, Path]] = []
    for entry in JOBS_DIR.iterdir():
        if not entry.is_dir():
            continue
        st_path = entry / "status.json"
        try:
            mtime = st_path.stat().st_mtime if st_path.exists() else entry.stat().st_mtime
            busy = False
            if st_path.exists():
                st = _read_json(st_path).get("status")
                busy = st in ("queued", "running")
            jobs.append((1 if busy else 0, mtime, entry))
        except OSError:
            continue
    jobs.sort(key=lambda t: (t[0], t[1]))

    target = int(limit * 0.85)
    freed = 0
    for _, _, entry in jobs:
        if size <= target:
            break
        try:
            sz = _dir_size_bytes(entry)
            shutil.rmtree(entry, ignore_errors=True)
            size -= sz
            freed += sz
            log.info(
                "quota: removed %s (freed %.1f MB, now %.1f / %d MB)",
                entry.name,
                sz / 1024 / 1024,
                size / 1024 / 1024,
                MAX_DISK_MB,
            )
        except Exception:
            pass
    return freed


def append_history(user_id, entry: dict) -> None:
    if not user_id:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{user_id}.json"
    items = []
    if path.exists():
        try:
            items = json.loads(path.read_text())
        except Exception:
            items = []
    items.insert(0, entry)
    path.write_text(json.dumps(items[:30], indent=2))


def _download_name_for(original_stem: str) -> str:
    if original_stem.endswith(" (compressed)"):
        return f"{original_stem}.pdf"
    return f"{original_stem} (compressed).pdf"


def process_job(job_id: str, payload: dict) -> None:
    job_path = _job_dir(job_id)
    items = payload["items"]
    target_mb = float(payload["target_mb"])
    mode = payload.get("mode") or "auto"
    password = payload.get("password") or None
    deskew = bool(payload.get("deskew", True))
    crop = bool(payload.get("crop", True))
    clarity_gate = bool(payload.get("clarity_gate", True))
    auto_split = bool(payload.get("auto_split", True))
    color_pages = payload.get("color_pages") or None
    strategy = payload.get("strategy") or "quality"

    lock = threading.Lock()
    files_state = []
    for item in items:
        files_state.append(
            {
                "index": item["index"],
                "original_filename": item["original_filename"],
                "status": "queued",
                "message": "Waiting…",
                "original_bytes": item["original_bytes"],
                "page_count": item.get("page_count"),
            }
        )

    _write_status(
        job_id,
        status="running",
        message=f"Starting {len(items)} file(s) in parallel (target {target_mb} MB each)…",
        progress=[],
        files=files_state,
        percent=0,
        eta_seconds=None,
        done_count=0,
        error_count=0,
        batch_size=len(items),
        worker_pid=os.getpid(),
        color_pages=color_pages,
        can_merge=len(items) >= 2,
        can_zip=len(items) >= 2,
    )

    file_progress: dict[int, list[str]] = {item["index"]: [] for item in items}
    file_pct: dict[int, float] = {item["index"]: 0.0 for item in items}

    def _refresh_status(msg: str | None = None):
        done_count = sum(1 for f in files_state if f["status"] == "done")
        error_count = sum(1 for f in files_state if f["status"] == "error")
        running = sum(1 for f in files_state if f["status"] == "running")
        avg = sum(file_pct.values()) / max(len(file_pct), 1)
        batch_pct = (done_count * 100 + avg * running / max(len(items), 1)) / max(len(items), 1)
        # Better: weight each file equally by its own pct
        batch_pct = sum(
            100.0 if f["status"] == "done" else (0.0 if f["status"] in ("queued", "error", "cancelled") else file_pct.get(f["index"], 0.0))
            for f in files_state
        ) / max(len(files_state), 1)
        progress_flat = []
        for f in files_state:
            progress_flat.extend(file_progress.get(f["index"], [])[-3:])
        _write_status(
            job_id,
            status="running",
            message=msg or f"Compressing… {done_count}/{len(items)} done, {running} active",
            progress=progress_flat[-16:],
            files=files_state,
            done_count=done_count,
            error_count=error_count,
            percent=batch_pct,
        )

    def _compress_one(item: dict) -> tuple[int, dict, Path | None, str | None]:
        i = item["index"]
        with lock:
            if _is_cancelled(job_id):
                raise CancelledError("Cancelled by user.")
            files_state[i]["status"] = "running"
            files_state[i]["message"] = "Compressing…"
            _refresh_status(f"File {i + 1}/{len(items)}: starting {item['original_filename']}")

        progress_log: list[str] = []

        def on_progress(msg: str, pct=None, eta=None, idx=i):
            progress_log.append(msg)
            with lock:
                file_progress[idx] = progress_log[-8:]
                if pct is not None:
                    file_pct[idx] = float(pct)
                files_state[idx]["message"] = msg
                files_state[idx]["progress"] = progress_log[-8:]
                _refresh_status(f"File {idx + 1}/{len(items)}: {msg}")

        out_name = f"{i:03d}_compressed.pdf"
        output_path = job_path / out_name
        preview_dir = job_path / f"preview_{i:03d}"
        result = compress_pdf(
            Path(item["input_path"]),
            target_size_mb=target_mb,
            output_path=output_path,
            mode=mode,
            on_progress=on_progress,
            password=password,
            deskew=deskew,
            crop=crop,
            clarity_gate=clarity_gate,
            auto_split=auto_split,
            split_pages=SPLIT_PAGES,
            preview_dir=preview_dir,
            cancel=lambda: _is_cancelled(job_id),
            color_pages=color_pages,
            strategy=strategy,
        )
        dl_name = _download_name_for(Path(item["original_filename"]).stem)
        meta = {
            "status": "done",
            "message": result.note or "Done",
            "compressed_bytes": result.compressed_bytes,
            "reduction_percent": round(result.reduction_percent, 1),
            "mode_used": result.mode,
            "note": result.note,
            "download_name": dl_name,
            "output_file": out_name,
            "preview_original": f"preview_{i:03d}/preview_original.jpg"
            if result.preview_original and result.preview_original.exists()
            else None,
            "preview_compressed": f"preview_{i:03d}/preview_compressed.jpg"
            if result.preview_compressed and result.preview_compressed.exists()
            else None,
            "clarity_score": result.clarity_score,
            "parts": [str(p.name) for p in result.part_paths],
            "progress": progress_log[-8:],
        }
        for pi, part in enumerate(result.part_paths):
            dest = job_path / f"{i:03d}_part{pi:02d}.pdf"
            if part.resolve() != dest.resolve():
                shutil.copy2(part, dest)
        return i, meta, output_path, dl_name

    successes: list[tuple[Path, str]] = []
    workers = max(1, min(MAX_PARALLEL, len(items)))

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_compress_one, item): item for item in items}
            for fut in as_completed(futures):
                item = futures[fut]
                i = item["index"]
                try:
                    idx, meta, out_path, dl_name = fut.result()
                    with lock:
                        files_state[idx].update(meta)
                        file_pct[idx] = 100.0
                        if out_path and out_path.is_file():
                            successes.append((out_path, dl_name or out_path.name))
                        _refresh_status()
                except CancelledError:
                    with lock:
                        files_state[i].update({"status": "cancelled", "message": "Cancelled"})
                        _write_status(
                            job_id,
                            status="cancelled",
                            message="Cancelled by user.",
                            files=files_state,
                            percent=100,
                        )
                    # cancel remaining
                    for f in futures:
                        f.cancel()
                    return
                except Exception as exc:
                    log.exception("job %s file %s failed", job_id, item["original_filename"])
                    with lock:
                        files_state[i].update(
                            {
                                "status": "error",
                                "message": str(exc),
                                "error": str(exc),
                            }
                        )
                        _refresh_status()
    except CancelledError:
        _write_status(job_id, status="cancelled", message="Cancelled by user.", percent=100)
        return

    # Sort successes by filename index for stable merge
    successes.sort(key=lambda t: t[0].name)

    done_count = sum(1 for f in files_state if f["status"] == "done")
    error_count = sum(1 for f in files_state if f["status"] == "error")

    zip_name = None
    merged_name = None
    if len(successes) >= 2:
        zip_name = "compressed.zip"
        zip_path = job_path / zip_name
        used_names: dict[str, int] = {}
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, member in successes:
                base = member
                if base in used_names:
                    used_names[base] += 1
                    stem = Path(base).stem
                    member = f"{stem} ({used_names[base]}).pdf"
                else:
                    used_names[base] = 0
                zf.write(path, arcname=member)

        # Merged single PDF of all compressed outputs
        try:
            import pymupdf

            merged_name = "merged_compressed.pdf"
            merged_path = job_path / merged_name
            merged = pymupdf.open()
            for path, _ in successes:
                merged.insert_pdf(pymupdf.open(path))
            merged.save(merged_path)
            merged.close()
        except Exception as exc:
            log.exception("merge failed for %s: %s", job_id, exc)
            merged_name = None

    if done_count == 0:
        batch_status = "error"
        message = "All files failed to compress."
    elif error_count:
        batch_status = "done"
        message = f"Finished with {done_count} ok, {error_count} failed."
    else:
        batch_status = "done"
        message = f"Compressed {done_count} file{'s' if done_count != 1 else ''} (each to ~{target_mb} MB)."

    single = next((f for f in files_state if f["status"] == "done"), None)
    extra = {}
    if single and done_count == 1:
        extra = {
            "original_bytes": single.get("original_bytes"),
            "compressed_bytes": single.get("compressed_bytes"),
            "reduction_percent": single.get("reduction_percent"),
            "page_count": single.get("page_count"),
            "mode_used": single.get("mode_used"),
            "note": single.get("note"),
            "download_name": single.get("download_name"),
            "output_file": single.get("output_file"),
            "original_filename": single.get("original_filename"),
            "preview_original": single.get("preview_original"),
            "preview_compressed": single.get("preview_compressed"),
            "clarity_score": single.get("clarity_score"),
        }

    status = _write_status(
        job_id,
        status=batch_status,
        message=message,
        files=files_state,
        done_count=done_count,
        error_count=error_count,
        batch_size=len(items),
        zip_file=zip_name,
        merged_file=merged_name,
        can_merge=bool(merged_name),
        can_zip=bool(zip_name),
        percent=100,
        eta_seconds=0,
        **extra,
    )

    append_history(
        payload.get("user_id"),
        {
            "job_id": job_id,
            "created_at": status.get("created_at") or _utcnow(),
            "finished_at": _utcnow(),
            "status": batch_status,
            "message": message,
            "batch_size": len(items),
            "files": [
                {
                    "name": f.get("original_filename"),
                    "status": f.get("status"),
                    "compressed_bytes": f.get("compressed_bytes"),
                    "mode_used": f.get("mode_used"),
                }
                for f in files_state
            ],
        },
    )
    enforce_disk_quota()



def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    log.info("PDF compressor worker started (poll=%.2fs)", POLL_SECONDS)
    while True:
        try:
            enforce_disk_quota()
            jobs = sorted(QUEUE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not jobs:
                time.sleep(POLL_SECONDS)
                continue
            qpath = jobs[0]
            try:
                payload = json.loads(qpath.read_text())
            except Exception:
                qpath.unlink(missing_ok=True)
                continue
            job_id = payload.get("job_id") or qpath.stem
            running = qpath.with_suffix(".running")
            try:
                qpath.rename(running)
            except FileNotFoundError:
                continue
            log.info("processing %s", job_id)
            try:
                process_job(job_id, payload)
            except Exception:
                log.error("job %s crashed\n%s", job_id, traceback.format_exc())
                _write_status(job_id, status="error", message="Worker crashed.", error="worker")
            finally:
                running.unlink(missing_ok=True)
        except Exception:
            log.error("loop error\n%s", traceback.format_exc())
            time.sleep(2)


if __name__ == "__main__":
    main()
