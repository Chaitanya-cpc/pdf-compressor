"""ScanClear PDF Compressor — Flask web app (shared CPCL login)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import auth
from secrets_env import load_secrets

APP_ROOT = Path(__file__).resolve().parent
JOBS_DIR = APP_ROOT / "data" / "jobs"
QUEUE_DIR = APP_ROOT / "data" / "queue"
HISTORY_DIR = APP_ROOT / "data" / "history"
TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3000"))
MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", "15"))
MAX_BATCH_MB = int(os.environ.get("MAX_BATCH_MB", "500"))
MAX_DISK_MB = int(os.environ.get("MAX_DISK_MB", "4000"))
SPLIT_PAGES = int(os.environ.get("SPLIT_PAGES", "80"))
APP_URL = os.environ.get("APP_URL", "https://plugin.erudore.com")
APP_NAME = "PDF Compressor"

load_secrets()

log = logging.getLogger(__name__)


class _PrefixMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get("HTTP_X_SCRIPT_NAME", "").rstrip("/")
        if script_name:
            environ["SCRIPT_NAME"] = script_name
            path = environ.get("PATH_INFO", "")
            if path.startswith(script_name):
                environ["PATH_INFO"] = path[len(script_name) :] or "/"
        return self.app(environ, start_response)


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "pdf-compressor-dev-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = MAX_BATCH_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_NAME"] = "plugin_session"
app.wsgi_app = _PrefixMiddleware(ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1))

auth.init_db()
JOBS_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


@app.context_processor
def inject_user():
    return {
        "current_user": {
            "employee_id": session.get("employee_id"),
            "name": session.get("name"),
        }
        if session.get("user_id")
        else None,
        "app_name": APP_NAME,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _write_status(job_id: str, **fields) -> dict:
    path = _job_dir(job_id) / "status.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    data.update(fields)
    data["updated_at"] = _utcnow().isoformat()
    path.write_text(json.dumps(data, indent=2))
    return data


def _read_status(job_id: str) -> dict | None:
    path = _job_dir(job_id) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


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


def _job_mtime_and_busy(entry: Path) -> tuple[float, bool]:
    """Return (mtime, is_busy). Busy = queued/running — prefer not to delete first."""
    status_path = entry / "status.json"
    mtime = status_path.stat().st_mtime if status_path.exists() else entry.stat().st_mtime
    busy = False
    if status_path.exists():
        try:
            st = json.loads(status_path.read_text()).get("status")
            busy = st in ("queued", "running")
        except Exception:
            pass
    return mtime, busy


def enforce_disk_quota() -> int:
    """
    Keep data/jobs under MAX_DISK_MB.
    When breached, delete oldest finished jobs first, then anything else,
    until size is under 85% of the cap. Returns bytes freed.
    """
    if not JOBS_DIR.exists():
        return 0
    limit = MAX_DISK_MB * 1024 * 1024
    size = _dir_size_bytes(JOBS_DIR)
    if size <= limit:
        return 0

    jobs: list[tuple[int, float, Path]] = []  # (busy_rank, mtime, path)
    for entry in JOBS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime, busy = _job_mtime_and_busy(entry)
            jobs.append((1 if busy else 0, mtime, entry))
        except OSError:
            continue
    # finished first (busy_rank 0), then oldest
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
            log.info("disk quota: removed job %s (freed %.1f MB, now %.1f MB)",
                     entry.name, sz / 1024 / 1024, size / 1024 / 1024)
        except OSError:
            continue
    return freed


def _cleanup_expired_jobs() -> None:
    now = time.time()
    if not JOBS_DIR.exists():
        return
    for entry in JOBS_DIR.iterdir():
        if not entry.is_dir():
            continue
        status_path = entry / "status.json"
        try:
            mtime = status_path.stat().st_mtime if status_path.exists() else entry.stat().st_mtime
            if now - mtime > TTL_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue
    enforce_disk_quota()


def _owned_by_user(status: dict) -> bool:
    return status.get("user_id") == session.get("user_id")


def _validate_pdf(path: Path, password: str | None = None) -> tuple[int | None, str | None]:
    if path.stat().st_size == 0:
        return None, "Uploaded file is empty."
    if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        return None, f"Each PDF must be ≤ {MAX_UPLOAD_MB} MB."
    try:
        import pymupdf

        doc = pymupdf.open(path)
        if doc.is_encrypted:
            if not password or not doc.authenticate(password):
                doc.close()
                return None, "PDF is password-protected. Enter the password and retry."
        page_count = doc.page_count
        doc.close()
    except Exception:
        return None, "Could not read this PDF. Is the file valid?"
    if page_count > MAX_PAGES:
        return None, f"PDF has {page_count} pages (limit {MAX_PAGES})."
    return page_count, None


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id") and request.method == "GET":
        return redirect(url_for("index"))
    if request.method == "POST":
        employee_id = request.form.get("employee_id", "")
        password = request.form.get("password", "")
        result = auth.authenticate_user(employee_id, password)
        if result == "pending":
            flash("Your account is awaiting admin approval.", "error")
            return render_template("login.html"), 403
        if not result:
            flash("Invalid Employee ID or password.", "error")
            return render_template("login.html"), 401
        session["user_id"] = result["id"]
        session["employee_id"] = result["employee_id"]
        session["name"] = result["name"]
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        err = auth.register_user(
            request.form.get("employee_id", ""),
            request.form.get("name", ""),
            request.form.get("password", ""),
            app_name=APP_NAME,
            app_url=APP_URL,
        )
        if err:
            flash(err, "error")
            return render_template("register.html"), 400
        flash("Registration submitted. Wait for admin approval, then sign in.", "ok")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if session.get("user_id") and request.method == "GET":
        return redirect(url_for("index"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("forgot.html"), 400
        err = auth.request_password_reset(
            request.form.get("employee_id", ""),
            password,
            app_name="PDF Compressor",
            app_url=APP_URL,
        )
        if err:
            flash(err, "error")
            return render_template("forgot.html"), 400
        flash(
            "Password reset submitted. Wait for admin approval. Current password still works until then.",
            "ok",
        )
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.get("/admin/approve-reset/<token>")
def admin_approve_reset(token):
    ok = auth.approve_password_reset(token)
    return ("Password reset approved." if ok else "Invalid or expired token."), (200 if ok else 404)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin/approve/<token>")
def admin_approve(token):
    ok = auth.approve_user(token)
    return ("Approved." if ok else "Invalid or expired token."), (200 if ok else 404)


# ── App ───────────────────────────────────────────────────────────────────────

@app.get("/")
@auth.require_auth
def index():
    _cleanup_expired_jobs()
    return render_template(
        "index.html",
        max_upload_mb=MAX_UPLOAD_MB,
        max_pages=MAX_PAGES,
        max_batch_files=MAX_BATCH_FILES,
        max_batch_mb=MAX_BATCH_MB,
        split_pages=SPLIT_PAGES,
    )


@app.get("/api/history")
@auth.require_auth
def job_history():
    uid = session.get("user_id")
    path = HISTORY_DIR / f"{uid}.json"
    if not path.exists():
        return jsonify({"items": []})
    try:
        items = json.loads(path.read_text())
    except Exception:
        items = []
    return jsonify({"items": items[:20]})


@app.post("/api/jobs")
@auth.require_auth
def create_job():
    _cleanup_expired_jobs()
    enforce_disk_quota()

    uploads = request.files.getlist("files")
    if not uploads or all(not u.filename for u in uploads):
        single = request.files.get("file")
        uploads = [single] if single and single.filename else []
    uploads = [u for u in uploads if u and u.filename]

    if not uploads:
        return jsonify({"error": "Please choose one or more PDF files."}), 400
    if len(uploads) > MAX_BATCH_FILES:
        return jsonify({"error": f"At most {MAX_BATCH_FILES} PDFs per batch."}), 400

    try:
        target_mb = float(request.form.get("target_mb", "2"))
    except ValueError:
        return jsonify({"error": "Target size must be a number (MB)."}), 400
    if target_mb < 0 or target_mb > 50:
        return jsonify({"error": "Target size must be between 0 and 50 MB."}), 400
    if target_mb == 0:
        target_mb = 0.1

    mode = (request.form.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "mono", "gray", "color", "per_page"):
        return jsonify({"error": "Mode must be auto, mono, gray, color, or per_page."}), 400

    pdf_password = (request.form.get("pdf_password") or "").strip() or None
    deskew = request.form.get("deskew", "1") not in ("0", "false", "off")
    crop = request.form.get("crop", "1") not in ("0", "false", "off")
    clarity_gate = request.form.get("clarity_gate", "1") not in ("0", "false", "off")
    auto_split = request.form.get("auto_split", "1") not in ("0", "false", "off")
    color_pages = (request.form.get("color_pages") or "").strip() or None
    if color_pages and mode != "per_page":
        # Still accept; only applied in per_page mode
        pass

    strategy = (request.form.get("strategy") or "quality").strip().lower()
    if strategy not in ("quality", "fast"):
        return jsonify({"error": "Strategy must be quality or fast."}), 400

    job_id = uuid.uuid4().hex
    job_path = _job_dir(job_id)
    job_path.mkdir(parents=True, exist_ok=True)

    items = []
    total_bytes = 0
    for index, upload in enumerate(uploads):
        raw_name = upload.filename or f"file_{index}.pdf"
        filename = secure_filename(raw_name) or f"file_{index}.pdf"
        if not filename.lower().endswith(".pdf"):
            shutil.rmtree(job_path, ignore_errors=True)
            return jsonify({"error": f"Only PDF files are accepted ({raw_name})."}), 400

        input_path = job_path / f"{index:03d}_original.pdf"
        upload.save(input_path)
        page_count, err = _validate_pdf(input_path, pdf_password)
        if err:
            shutil.rmtree(job_path, ignore_errors=True)
            return jsonify({"error": f"{raw_name}: {err}"}), 400

        size = input_path.stat().st_size
        total_bytes += size
        items.append(
            {
                "index": index,
                "original_filename": filename,
                "input_path": str(input_path),
                "original_bytes": size,
                "page_count": page_count,
            }
        )

    if total_bytes > MAX_BATCH_MB * 1024 * 1024:
        shutil.rmtree(job_path, ignore_errors=True)
        return jsonify({"error": f"Total upload exceeds {MAX_BATCH_MB} MB."}), 400

    _write_status(
        job_id,
        status="queued",
        message="Queued…",
        progress=[],
        percent=0,
        user_id=session.get("user_id"),
        employee_id=session.get("employee_id"),
        target_mb=target_mb,
        mode_requested=mode,
        strategy=strategy,
        batch_size=len(items),
        files=[
            {
                "index": it["index"],
                "original_filename": it["original_filename"],
                "status": "queued",
                "message": "Queued…",
                "original_bytes": it["original_bytes"],
                "page_count": it["page_count"],
            }
            for it in items
        ],
        created_at=_utcnow().isoformat(),
    )

    payload = {
        "job_id": job_id,
        "user_id": session.get("user_id"),
        "employee_id": session.get("employee_id"),
        "target_mb": target_mb,
        "mode": mode,
        "password": pdf_password,
        "deskew": deskew,
        "crop": crop,
        "clarity_gate": clarity_gate,
        "auto_split": auto_split,
        "color_pages": color_pages,
        "strategy": strategy,
        "items": items,
    }
    qpath = QUEUE_DIR / f"{job_id}.json"
    qpath.write_text(json.dumps(payload))

    return jsonify({"job_id": job_id, "batch_size": len(items)}), 202


@app.get("/api/jobs/<job_id>")
@auth.require_auth
def job_status(job_id: str):
    if not job_id.isalnum() or len(job_id) > 64:
        return jsonify({"error": "Invalid job id."}), 400
    status = _read_status(job_id)
    if not status or not _owned_by_user(status):
        return jsonify({"error": "Job not found."}), 404
    public = {
        k: status.get(k)
        for k in (
            "status",
            "message",
            "progress",
            "percent",
            "eta_seconds",
            "original_bytes",
            "compressed_bytes",
            "reduction_percent",
            "page_count",
            "mode_used",
            "mode_requested",
            "target_mb",
            "download_name",
            "error",
            "original_filename",
            "note",
            "files",
            "batch_size",
            "done_count",
            "error_count",
            "current_index",
            "zip_file",
            "merged_file",
            "can_merge",
            "can_zip",
            "preview_original",
            "preview_compressed",
            "clarity_score",
            "cancel_requested",
            "color_pages",
            "strategy",
        )
    }
    return jsonify(public)


@app.post("/api/jobs/<job_id>/cancel")
@auth.require_auth
def job_cancel(job_id: str):
    if not job_id.isalnum() or len(job_id) > 64:
        return jsonify({"error": "Invalid job id."}), 400
    status = _read_status(job_id)
    if not status or not _owned_by_user(status):
        return jsonify({"error": "Job not found."}), 404
    if status.get("status") in ("done", "error", "cancelled"):
        return jsonify({"ok": True, "status": status.get("status")})
    _write_status(job_id, cancel_requested=True, message="Cancel requested…")
    # Drop from queue if still waiting
    qpath = QUEUE_DIR / f"{job_id}.json"
    if qpath.exists():
        qpath.unlink(missing_ok=True)
        _write_status(job_id, status="cancelled", message="Cancelled before start.", percent=100)
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/preview/<kind>")
@auth.require_auth
def job_preview(job_id: str, kind: str):
    if not job_id.isalnum() or len(job_id) > 64:
        return "Invalid job id.", 400
    if kind not in ("original", "compressed"):
        return "Invalid kind.", 400
    status = _read_status(job_id)
    if not status or not _owned_by_user(status):
        return "Not found.", 404
    index = request.args.get("index", "0")
    try:
        i = int(index)
    except ValueError:
        return "Invalid index.", 400
    rel = status.get(f"preview_{kind}")
    files = status.get("files") or []
    match = next((f for f in files if f.get("index") == i), None)
    if match:
        rel = match.get(f"preview_{kind}") or rel
    if not rel:
        return "Preview not ready.", 404
    path = _job_dir(job_id) / rel
    if not path.is_file():
        return "Preview missing.", 404
    return send_file(path, mimetype="image/jpeg")


@app.get("/api/jobs/<job_id>/download")
@auth.require_auth
def job_download(job_id: str):
    if not job_id.isalnum() or len(job_id) > 64:
        return "Invalid job id.", 400
    status = _read_status(job_id)
    if not status or not _owned_by_user(status):
        return "Job not found.", 404
    if status.get("status") != "done":
        return "Job not ready.", 409

    as_kind = (request.args.get("as") or "").strip().lower()
    if request.args.get("merge") == "1":
        as_kind = "merge"
    if request.args.get("zip") == "1":
        as_kind = "zip"

    # Merged PDF of all compressed outputs
    if as_kind == "merge":
        merged = status.get("merged_file")
        if not merged:
            return "Merged PDF not available.", 404
        out = _job_dir(job_id) / merged
        if not out.is_file():
            return "Merged file missing.", 404
        return send_file(
            out,
            as_attachment=True,
            download_name="merged (compressed).pdf",
            mimetype="application/pdf",
        )

    # ZIP of separate compressed files
    if as_kind == "zip":
        zip_file = status.get("zip_file")
        if not zip_file:
            return "ZIP not available.", 404
        out = _job_dir(job_id) / zip_file
        if not out.is_file():
            return "ZIP missing.", 404
        return send_file(
            out,
            as_attachment=True,
            download_name="compressed PDFs.zip",
            mimetype="application/zip",
        )

    part = request.args.get("part")
    if part is not None:
        try:
            pi = int(part)
            fi = int(request.args.get("index", "0"))
        except ValueError:
            return "Invalid part.", 400
        out = _job_dir(job_id) / f"{fi:03d}_part{pi:02d}.pdf"
        if not out.is_file():
            return "Part not found.", 404
        return send_file(out, as_attachment=True, download_name=out.name, mimetype="application/pdf")

    idx = request.args.get("index")
    if idx is not None:
        try:
            i = int(idx)
        except ValueError:
            return "Invalid index.", 400
        files = status.get("files") or []
        match = next((f for f in files if f.get("index") == i and f.get("status") == "done"), None)
        if not match:
            return "File not found.", 404
        out = _job_dir(job_id) / match["output_file"]
        if not out.is_file():
            return "File missing.", 404
        return send_file(
            out,
            as_attachment=True,
            download_name=match.get("download_name") or "compressed.pdf",
            mimetype="application/pdf",
        )

    # Default: multi-file → ZIP; single → that PDF
    if (status.get("batch_size") or 1) > 1 and status.get("zip_file"):
        out = _job_dir(job_id) / status["zip_file"]
        if out.is_file():
            return send_file(
                out,
                as_attachment=True,
                download_name="compressed PDFs.zip",
                mimetype="application/zip",
            )

    out = _job_dir(job_id) / (status.get("output_file") or "compressed.pdf")
    if not out.is_file():
        files = status.get("files") or []
        match = next((f for f in files if f.get("status") == "done"), None)
        if not match:
            return "File missing.", 404
        out = _job_dir(job_id) / match["output_file"]
        name = match.get("download_name") or "compressed.pdf"
    else:
        name = status.get("download_name") or "compressed.pdf"
    return send_file(out, as_attachment=True, download_name=name, mimetype="application/pdf")


@app.get("/health")
@app.get("/healthz")
def health():
    queued = len(list(QUEUE_DIR.glob("*.json"))) if QUEUE_DIR.exists() else 0
    return jsonify(
        {
            "ok": True,
            "service": "pdf-compressor",
            "queued": queued,
            "disk_mb": round(_dir_size_bytes(JOBS_DIR) / 1024 / 1024, 1),
            "disk_limit_mb": MAX_DISK_MB,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "6012"))
    app.run(host=os.environ.get("BIND", "127.0.0.1"), port=port, debug=False)
