# Product requirements: ScanClear PDF Compressor

**Product name:** PDF Compressor (ScanClear)  
**Audience for this document:** a developer who has never seen the code and must rebuild the same product.

This document is the contract. If the implementation and this document disagree, match this document. It describes the product as it behaves in production, including the rules that keep a download at or under the size the user selected.

Do not copy production secrets, the shared user database, or job files from the server. Those are operational data, not part of the product.

---

## 1. What this product is

Employees of CPCL upload one or more PDFs, pick a maximum size in megabytes, and download a smaller PDF that is still readable. The typical input is a scanned document (a form, a notice, an annual report, a prospectus) that must fit a portal upload limit. Born-digital PDFs with real text are compressed without turning the text into a picture, when that is enough to hit the target.

The product is a web application. A browser talks to a Flask HTTP service. Compression runs in a separate worker process so a long job does not block the web workers. The two processes communicate through files on local disk. There is no database for jobs.

### 1.1 Users

- Signed-in employees. Identity is an employee ID plus a password.
- New employees self-register and cannot sign in until an admin approves them.
- An admin approves registrations and password resets by opening a one-time link emailed to the admin address.
- There is no in-app admin console.

### 1.2 What success looks like

1. A signed-in user can compress one PDF, or up to 15 PDFs in one batch, to a chosen target from 0.1 MB to 50 MB.
2. Each output file’s size on disk is less than or equal to that target. A success response must not be returned if the file the user will download is larger.
3. Text stays as sharp as the target allows. The compressor prefers a higher-resolution, higher-quality encode that still fits, rather than the smallest possible file.
4. The user can watch progress, cancel, preview page 1 before and after, and download the result for about an hour.
5. A batch of two or more successful files can be downloaded as a ZIP of separate PDFs or as one merged PDF.

### 1.3 Explicitly out of scope

- OCR, searchable text on scans, digital signatures, form-field preservation, PDF/A, redaction, page delete/reorder, or watermarking.
- Cloud object storage. Files live on the app server’s disk.
- Per-user storage quotas. There is one disk cap for all jobs.
- Billing, roles beyond “approved user vs pending user”, or an admin UI.
- Mobile native apps. The site must work in a phone browser.

---

## 2. System shape

```
Browser
  │  HTTPS
  ▼
nginx (:443, and an optional /pdf-compressor/ prefix)
  │  proxy to 127.0.0.1:6012
  ▼
gunicorn (2 workers)  ── Flask app (app.py)
  │                         writes data/jobs/<id>/
  │                         writes data/queue/<id>.json
  ▼
worker.py (one process, polls the queue)
  │  compress_pdf() in pdf_compressor/core.py
  ▼
data/jobs/<id>/status.json + compressed PDFs
data/history/<user_id>.json
```

Shared login data is a SQLite file used by several CPCL apps, not only this one. In production that file is `/root/webapps/shared_users.db`. A rebuild may point `DB_PATH` at any path, but the table shape in section 6 must stay compatible if this app shares users with the rest of the suite.

### 2.1 Processes

| Process | How it runs | Port / role |
|---|---|---|
| `webapp-pdf-compressor` | `gunicorn -b 127.0.0.1:6012 -w 2 --timeout 1800 app:app` | HTTP. Memory restart around 1500 MB. |
| `webapp-pdf-compressor-worker` | `python worker.py` | One poll loop. Memory restart around 2500 MB. |

Bind the web process to localhost. nginx is the only public listener.

gunicorn’s timeout must be long (1800 seconds) because an upload of a large PDF can take a while to be received and validated. Compression itself happens in the worker, not inside the request that returns `202`.

### 2.2 Repository layout to rebuild

```
app.py                  Flask routes, upload validation, downloads
auth.py                 Shared-user SQLite, approval email, session gate
worker.py               Queue consumer, parallel compress, ZIP and merge
pdf_compressor/core.py  Compression algorithm
pdf_compressor/cli.py   Optional CLI: pdf-compressor file.pdf 2
templates/login.html
templates/register.html
templates/forgot.html
templates/index.html    The compressor UI (one page, no frontend build)
static/                 Shared visual assets if the templates reference them
requirements.txt
docs/PRD.md             This document
```

Python dependencies:

- flask >= 3.0
- gunicorn >= 22.0
- werkzeug >= 3.0
- Pillow >= 10
- pymupdf >= 1.24
- img2pdf >= 0.5
- pikepdf >= 9

There is no Node build, no database migration tool, and no Redis.

---

## 3. Screens

Visual language: dark panel, one column, readable on a phone. Header shows “PDF Compressor”, “CPCL Suite · ScanClear clarity pipeline”, the signed-in name, a link to the suite home, and Logout.

### 3.1 Login, register, forgot password

**Login** (`GET/POST /login`)

- Fields: Employee ID, password.
- Already signed in and opening the page: redirect to `/`.
- Wrong password or unknown ID: HTTP 401, flash “Invalid Employee ID or password.”
- Account exists but `status` is `pending`: HTTP 403, flash “Your account is awaiting admin approval.” Do not start a session.
- Success: store `user_id`, `employee_id`, and `name` in the Flask session cookie and redirect to `/`.

**Register** (`GET/POST /register`)

- Fields: Employee ID, name, password.
- Password shorter than 6 characters: “Password must be at least 6 characters.”
- Empty field: “All fields are required.”
- Employee ID already present: “Employee ID already registered.”
- Success: insert the user as `pending`, email the admin, flash “Registration submitted. Wait for admin approval, then sign in.” and redirect to login. The user cannot sign in yet.

**Forgot password** (`GET/POST /forgot-password`)

- Fields: Employee ID, new password, confirm password.
- Mismatch: “Passwords do not match.”
- Unknown employee ID: respond as if it worked. Do not reveal whether the ID exists.
- Account still pending registration: “This account is still awaiting registration approval.”
- A reset already waiting: “A password reset is already pending admin approval for this account.”
- Success: store a hash of the *new* password in `pending_password_hash` and a token in `reset_token`. The current password keeps working until the admin approves. Flash that message.

**Approval links** (no login required; the token is the credential)

- `GET /admin/approve/<token>` — if the token matches a pending user, set `status` to `approved` and clear `approval_token`. Body: “Approved.” or “Invalid or expired token.” (404).
- `GET /admin/approve-reset/<token>` — copy `pending_password_hash` onto `password_hash`, then clear `pending_password_hash` and `reset_token`. Body: “Password reset approved.” or “Invalid or expired token.” (404).

### 3.2 Compressor page (`GET /`)

Requires a session. On each load, delete job directories whose `status.json` mtime is older than `JOB_TTL_SECONDS` (default 3600) and then enforce the disk cap.

Controls, top to bottom:

1. **Drop zone.** Click, Enter/Space, or drag-and-drop. `accept` PDF only. Multiple files. A list of chosen files with size and a Remove button. Summary line: “1 file selected” or “N files · X MB total”.
2. **Strategy.** Two buttons, default Quality.
   - Quality: “ScanClear clarity ladder (sharp text, more attempts).”
   - Fast: “coarse JPEG/size crush like typical online tools — quicker, lower clarity.”
3. **Target size.** Range slider `min=0`, `max=50`, `step=0.1`, default `2`. Label shows one decimal and “MB”. End labels “0 MB” and “50 MB”. Preset buttons: 1, 2, 5, 10, 20, 50. The label says the target is **per file**. Slider value `0` is sent as `0` and the server turns it into **0.1 MB**.
4. **Mode** select, default Auto:
   - `auto` — “Auto: mono first, then gray. Born-digital PDFs use object compression when possible.”
   - `mono` — “1-bit Group-4 — sharpest text and smallest files.”
   - `gray` — “Grayscale JPEG — keeps stamp/photo tone without colour.”
   - `color` — “Colour JPEG first; falls back to gray if the target cannot be hit.”
   - `per_page` — “Colour on selected page numbers plus any pages that look colourful; rest mono.”
5. **Force-colour pages.** Visible only when mode is `per_page`. Text field, placeholder `e.g. 1, 3, 5-8, 12`.
6. **PDF password.** Optional password input, not stored in the browser beyond the request.
7. **Checkboxes, all on by default:** Deskew pages. Crop blank margins. Clarity gate. Auto-split when page count is greater than `SPLIT_PAGES` (shown in the label, default 80).
8. **Buttons:** Compress (disabled until a file is chosen; label becomes “Compress N PDFs” when N > 1). Cancel (hidden until a job is running). Reset (clears the selection and the status panel; does not delete the server job).

**While running**

- Title “Compressing…”, the latest status message, a progress bar from `percent`, an ETA line, a scrolling log of the last progress strings, and a row per file.
- ETA copy: under 5 seconds “Almost done”; under 60 seconds “~Ns left”; otherwise “~N min left”.
- Poll `GET /api/jobs/<id>` every **1000 ms**.
- When previews exist, show page-1 original and compressed JPEGs side by side.

**When finished**

- Title “Ready” if `status` is `done`, even if some files in the batch failed. Title “Error” if the whole job is `error`. Title for `cancelled` follows the status message.
- Single success: three stats — Before, After, Saved (`reduction_percent` plus `mode_used`). One download button for that PDF.
- Two or more successes: stats show “N ok”, “M failed”, “N files”. Primary button downloads the ZIP (`?as=zip`). Secondary button “Download merged PDF” (`?as=merge`) when a merged file exists.
- Each successful file row has its own Download link (`?index=`). If that file was auto-split, also link Part 1, Part 2, … (`?index=&part=`).
- Show `note` when the server sent one (for example a gray fallback, or the split explanation).

**Recent jobs**

- On load, `GET /api/history`. Show up to 20 items: time, message, and a download link when `status` is `done`. Downloads only work until the job directory is deleted (TTL or disk cap).

### 3.3 Client-side limits (must match the server)

| Rule | Value |
|---|---|
| Skip non-PDF | alert and skip |
| One file | ≤ `MAX_UPLOAD_MB` (500) |
| File count | ≤ `MAX_BATCH_FILES` (15) |
| Sum of selected sizes | ≤ `MAX_BATCH_MB` (500). If adding a file would exceed it, drop that file and alert. |
| Duplicate | same name and same byte size is ignored |

The server repeats these checks. The browser checks exist so the user hears about the problem before a long upload.

---

## 4. HTTP API

All `/api/*` routes except `GET /health` require a session. Missing session on an HTML page redirects to `/login`. JSON routes use the same decorator, so an anonymous API call is redirected to the login page rather than returning 401 JSON. That is the current contract; a rebuild should keep pages and the API behind the same session.

Cookie session, `Secure` in production behind HTTPS (ProxyFix trusts `X-Forwarded-*`). Secret from `FLASK_SECRET_KEY`. Never ship the fallback dev string in production.

`MAX_CONTENT_LENGTH` is `MAX_BATCH_MB` megabytes. nginx `client_max_body_size` must be at least that (500m).

### 4.1 `POST /api/jobs`

Multipart form:

| Field | Required | Meaning |
|---|---|---|
| `files` | yes (or legacy single `file`) | One or more PDF parts |
| `target_mb` | no, default `2` | Number. Allowed range 0 through 50 inclusive. `0` becomes `0.1`. |
| `mode` | no, default `auto` | `auto`, `mono`, `gray`, `color`, `per_page` |
| `strategy` | no, default `quality` | `quality` or `fast` |
| `pdf_password` | no | Applied to every file in the batch |
| `deskew` | no, default on | Off when value is `0`, `false`, or `off` |
| `crop` | no, default on | same |
| `clarity_gate` | no, default on | same |
| `auto_split` | no, default on | same |
| `color_pages` | no | Page spec. Applied only when `mode` is `per_page` |

Response `202`:

```json
{ "job_id": "<32 hex chars>", "batch_size": 2 }
```

Errors are JSON `{ "error": "<sentence>" }` with 400, except where noted.

Validation order:

1. At least one file with a filename. Else “Please choose one or more PDF files.”
2. Count ≤ 15. Else “At most 15 PDFs per batch.”
3. `target_mb` parses as a float in `[0, 50]`.
4. `mode` and `strategy` are in the allowed sets.
5. Create `data/jobs/<job_id>/`.
6. For each file, in order: reject non-`.pdf` names; save as `000_original.pdf`, `001_original.pdf`, …; open with PyMuPDF; if encrypted, the supplied password must authenticate; page count ≤ `MAX_PAGES` (3000); each file ≤ `MAX_UPLOAD_MB`. On any failure, delete the job directory and return 400 with the original filename prefixed on the message.
7. Sum of sizes ≤ `MAX_BATCH_MB`.
8. Write `status.json` with `status: "queued"`.
9. Write `data/queue/<job_id>.json` containing the payload below, including the PDF password in plaintext. The queue file is local-disk-only and must not be logged.

Queue payload:

```json
{
  "job_id": "...",
  "user_id": 1,
  "employee_id": "A0001",
  "target_mb": 2.0,
  "mode": "auto",
  "password": null,
  "deskew": true,
  "crop": true,
  "clarity_gate": true,
  "auto_split": true,
  "color_pages": "1,3,5-8",
  "strategy": "quality",
  "items": [
    {
      "index": 0,
      "original_filename": "notice.pdf",
      "input_path": "/abs/path/000_original.pdf",
      "original_bytes": 1234567,
      "page_count": 12
    }
  ]
}
```

`original_filename` is `secure_filename` of the upload name, or `file_<index>.pdf` if that sanitizes to empty.

### 4.2 `GET /api/jobs/<job_id>`

`job_id` must be alphanumeric and at most 64 characters. The job’s `user_id` must equal the session user. Otherwise 404 “Job not found.” Never return another user’s job.

Public fields (omit anything else, especially filesystem paths and the PDF password):

`status`, `message`, `progress`, `percent`, `eta_seconds`, `original_bytes`, `compressed_bytes`, `reduction_percent`, `page_count`, `mode_used`, `mode_requested`, `target_mb`, `download_name`, `error`, `original_filename`, `note`, `files`, `batch_size`, `done_count`, `error_count`, `current_index`, `zip_file`, `merged_file`, `can_merge`, `can_zip`, `preview_original`, `preview_compressed`, `clarity_score`, `cancel_requested`, `color_pages`, `strategy`.

`status` is one of `queued`, `running`, `done`, `error`, `cancelled`.

A batch with some failures and some successes ends as `done`, with message `Finished with N ok, M failed.` A batch where every file failed is `error` with “All files failed to compress.” A full success message is `Compressed N file(s) (each to ~<target_mb> MB).` That sentence is a claim: each downloadable PDF must actually be ≤ the target.

### 4.3 `POST /api/jobs/<job_id>/cancel`

Owner only. If the job is already `done`, `error`, or `cancelled`, return `{ "ok": true, "status": "<that>" }`.

Otherwise set `cancel_requested: true` and message “Cancel requested…”. If the queue file still exists, delete it and set status `cancelled`, message “Cancelled before start.”, percent 100.

If the worker has already renamed the queue file to `.running`, the worker notices `cancel_requested` between pages and between files and stops. In-flight threads are asked to raise `CancelledError`. Files already finished stay on disk; the job status becomes `cancelled`.

### 4.4 `GET /api/jobs/<job_id>/preview/<kind>`

`kind` is `original` or `compressed`. Query `index` defaults to `0`. Resolve the relative path from that file’s `preview_original` / `preview_compressed`, falling back to the top-level fields. Return the JPEG (`image/jpeg`). 404 if not ready.

### 4.5 `GET /api/jobs/<job_id>/download`

Owner only. Job `status` must be `done`, else 409 “Job not ready.”

| Query | File | Download name |
|---|---|---|
| `as=merge` or `merge=1` | `merged_compressed.pdf` | `merged (compressed).pdf` |
| `as=zip` or `zip=1` | `compressed.zip` | `compressed PDFs.zip` |
| `index=<i>&part=<p>` | `<iii>_part<pp>.pdf` | the filename |
| `index=<i>` | that file’s `output_file` | `<stem> (compressed).pdf` |
| (none), batch > 1 and a zip exists | the zip | `compressed PDFs.zip` |
| (none), single file | `output_file` | `<stem> (compressed).pdf` |

Content types: `application/pdf` or `application/zip`. `as_attachment=true`.

### 4.6 `GET /api/history`

`{ "items": [ ...up to 20... ] }` from `data/history/<user_id>.json`. Missing file returns `{ "items": [] }`. The worker keeps 30 entries; the API shows 20. Each entry:

```json
{
  "job_id": "...",
  "created_at": "2026-10-11T07:36:15+00:00",
  "finished_at": "...",
  "status": "done",
  "message": "Compressed 1 file (each to ~2.3 MB).",
  "batch_size": 1,
  "files": [
    { "name": "form.pdf", "status": "done", "compressed_bytes": 2100000, "mode_used": "mono" }
  ]
}
```

### 4.7 `GET /health`

No auth. Used by operators.

```json
{ "ok": true, "service": "pdf-compressor", "queued": 0, "disk_mb": 120.5, "disk_limit_mb": 4000 }
```

`queued` is the number of `data/queue/*.json` files.

---

## 5. Job files and the worker

### 5.1 Directory layout

```
data/jobs/<job_id>/
  status.json
  000_original.pdf
  000_compressed.pdf
  000_part00.pdf            only when that file was auto-split
  001_original.pdf
  001_compressed.pdf
  preview_000/
    preview_original.jpg
    preview_compressed.jpg
  000_compressed_parts/     intermediate part PDFs (may be deleted with the job)
  compressed.zip            when at least two files succeeded
  merged_compressed.pdf     when at least two files succeeded and merge worked
data/queue/<job_id>.json    waiting
data/queue/<job_id>.running worker claimed it
data/history/<user_id>.json
```

`status.json` is rewritten by both processes. The worker writes via a temp file and `rename` so readers never see a half-written file. The web process updates status for the initial queue write, cancel, and does not need the rename. Both must tolerate a missing or briefly unreadable file.

### 5.2 Worker loop

1. Sleep `WORKER_POLL_SECONDS` (0.75) when the queue is empty.
2. On each wake, enforce the disk quota.
3. Take the oldest `*.json` by mtime. Rename it to `*.running` (the claim). If the rename fails, another loop already took it; skip.
4. Call `process_job`. On an unexpected exception, set status `error`, message “Worker crashed.”
5. Always delete the `.running` file in a `finally`.
6. Only one job payload is processed at a time by the loop. Inside a job, up to `MAX_PARALLEL` (3) files run together in a thread pool.

Do not run two worker processes against the same queue directory. The rename is the lock.

### 5.3 Parallel files

Each file is compressed with the same target, mode, strategy, password, and toggles. Progress is merged under a lock:

- Each file contributes equally to `percent`. A finished file counts as 100. A queued, errored, or cancelled file counts as 0. A running file counts as its own percent.
- `progress` on the job is the last few lines from each file, capped around 16 lines.
- One file’s exception does not cancel the others. Record `status: "error"`, `message` and `error` set to the exception text, and continue.
- `CancelledError` marks the job cancelled and returns. Threads already running should stop at the next page.

### 5.4 ZIP and merged PDF

After the pool finishes, if at least two files succeeded:

- `compressed.zip` stores each download name. If two files would produce the same archive name, the second becomes `<stem> (1).pdf`, then `(2)`, and so on.
- `merged_compressed.pdf` is the successful outputs concatenated in filename order (`000_compressed.pdf` before `001_compressed.pdf`) using PyMuPDF `insert_pdf`. If merge throws, log it, leave `merged_file` null, and still mark the job done so the ZIP remains available.

The merged PDF of a *batch* is a convenience. Its size is the sum of the files. It is **not** required to be under the per-file target. The per-file target applies to each compressed PDF.

### 5.5 Disk cap and TTL

`MAX_DISK_MB` default 4000, measured on `data/jobs` only (not history).

When size exceeds the cap, delete job directories until usage is at or under **85%** of the cap. Order: finished jobs first (anything whose status is not `queued` or `running`), oldest mtime first; only then delete busy jobs if still over. Log each deletion.

TTL: any job directory whose status file is older than `JOB_TTL_SECONDS` (3600) is removed when someone loads `/` or posts a new job. The worker does not run the TTL sweep; it does run the quota.

History JSON is small and is not purged by these rules.

---

## 6. Accounts

Table `users`:

| Column | Type | Notes |
|---|---|---|
| `id` | integer pk | This is `user_id` in the session and the history filename |
| `employee_id` | text unique | What the person types. Example shape: `A0003`, `D0003`, `10562` |
| `name` | text | Display name |
| `password_hash` | text | Werkzeug hash (`scrypt` or `pbkdf2`). Never store or email the password |
| `created_at` | text | SQLite `datetime('now')` |
| `upload_password_hash` | text null | Legacy column. Leave it. This app does not use it |
| `status` | text | `pending` or `approved`. Default for old rows is `approved` |
| `approval_token` | text null | Set while registration is pending |
| `pending_password_hash` | text null | Proposed new password, hashed |
| `reset_token` | text null | Set while a reset waits |

`init_db` creates the table if needed and `ALTER TABLE`s missing columns. It must not drop or rewrite existing rows. Other apps share this file.

Passwords are checked with Werkzeug `check_password_hash`. Hashes are generated with `generate_password_hash`.

### 6.1 Email

SMTP is Gmail on `smtp.gmail.com:587` with STARTTLS. Credentials come from the environment at send time, not at import:

- `NOTIFY_SMTP_USER`
- `NOTIFY_SMTP_PASS`
- `NOTIFY_ADMIN_EMAIL` default `chaitanya@cpcmail.in`

If user or password is empty, log a warning and still create the pending row. The admin can approve from the database if mail fails.

Registration mail subject: `[CPCL] New Registration Pending Approval — <employee_id>`. Body includes employee ID, name, app name, and `{APP_URL}/admin/approve/{token}`.

Reset mail subject: `[CPCL] Password Reset Pending Approval — <employee_id>`. Link is `{APP_URL}/admin/approve-reset/{token}`. Say that the current password still works until approval.

`APP_URL` is the public base URL of this app, supplied by the environment. Tokens are `secrets.token_urlsafe(32)`. Single use because a successful approval clears the token.

Login attempts are rate-limited at nginx on `location = /login` (`limit_req`, burst 3). The app itself does not count failures.

---

## 7. Compression algorithm

This is the part that must be rebuilt carefully. The function is `compress_pdf(...)` in `pdf_compressor/core.py`. The CLI and the worker both call it.

### 7.1 Inputs

| Argument | Meaning |
|---|---|
| `input_path` | Source PDF |
| `target_size_mb` | Positive number of megabytes. The output file size must be ≤ `floor(target_mb × 1024 × 1024)` bytes |
| `output_path` | Where to write the PDF |
| `mode` | `auto`, `mono`, `gray`, `color`, `per_page` |
| `strategy` | `quality` (default) or `fast` |
| `password` | or null |
| `deskew`, `crop`, `clarity_gate`, `auto_split` | booleans |
| `split_pages` | default 80 |
| `preview_dir` | if set, write `preview_original.jpg` and `preview_compressed.jpg` of page 1, long edge 900 px |
| `cancel` | callable returning true when the user cancelled |
| `color_pages` | string `1,3,5-8` or a set of 1-based page numbers. Only `per_page` mode uses them |
| `on_progress(message, percent, eta_seconds)` | optional |

`strategy=fast` forces `clarity_gate=False` and `deskew=False` even if the caller passed them on. Crop stays as the caller set it. Render DPI is **140** for fast and **250** for quality.

A string target such as `"2mb"` is accepted by the CLI parser: strip space, strip a trailing `mb` or `m`, parse float, reject ≤ 0.

### 7.2 Reading pages

For every page, in order:

1. If the page has exactly one embedded image, and its extension is not `jpx`, `jp2`, `j2k`, or `jpf`, extract the bytes and open them with Pillow. Call `.load()` before converting to RGB, so a broken stream fails here rather than later.
2. On any failure, or when the image is JPEG 2000, or when the page has zero or many images, render with PyMuPDF `get_pixmap(dpi=<strategy dpi>, alpha=False)` and build an RGB image from the samples.
3. Then preprocess.

JPEG 2000 streams in annual-report PDFs often raise Pillow `OSError: broken data stream when reading image file`. The pixmap path is the required fallback. Do not fail the document because one embedded JPX stream is undecodable.

### 7.3 Preprocess

**Deskew** (quality strategy, when the checkbox is on). Downscale a grayscale thumbnail to fit in 900×900. Threshold at 180. Try angles from −4.0° to +4.0° in 0.5° steps. Score each rotation by the variance of horizontal ink (row sums of dark pixels, sampled every third column). Rotate the full image by the winning angle with bicubic resampling and white fill, but only if the angle’s absolute value is at least 0.3°.

**Crop.** Threshold grayscale at 245. Bounding box of non-white pixels, pad 12 px. If the crop would keep less than half the width or half the height, do not crop (the page is probably a faint scan, not a margin).

### 7.4 Native text path

Used only when `mode` is `auto` or `gray`, before rasterizing.

Look at the first `min(8, page_count)` pages. Sum stripped text length. Count pages that have at least one image and fewer than 40 characters of text. Treat the file as born-digital when total text characters ≥ 400 and image-heavy pages ≤ half of the sampled pages.

Then save a new PDF with pikepdf: `compress_streams=True`, `object_stream_mode=generate`, `recompress_flate=True`. If that file is ≤ target, return it with `mode="native"`, `native_pdf=true`, `long_edge_px=0`, note “Born-digital PDF: used object compression (text kept selectable).” If it is still over the target, continue into the raster pipeline and say so in progress. If pikepdf throws, skip the native path and rasterize.

### 7.5 Raster encodes

Every raster page is resized so its long edge equals the candidate length (Lanczos), unless it is already smaller.

**Mono.** Autocontrast grayscale (cutoff 1%), unsharp mask radius 1, percent 80, threshold 2, then threshold at 190 into a 1-bit image. Save as TIFF with Group4 compression. Pages are assembled into an A4 PDF with img2pdf.

**Gray.** Grayscale JPEG, `optimize=True`, `progressive=True`, at the candidate quality.

**Color.** RGB JPEG, same flags.

**Per-page.** The candidate is a color (long-edge, quality) pair, but each page is encoded as color or as mono:

- If the 1-based page number is in the force-colour set, encode color.
- Otherwise compute chroma on a 200×200 thumbnail: mean absolute deviation of RGB from the average of the channel means. If chroma > 18, encode color; else mono.

Force-colour parsing: split on commas; `5-8` is inclusive; swap reversed ranges; ignore tokens that are not integers; drop numbers outside `1..page_count`.

### 7.6 Ladders

Settings are ordered from largest / sharpest to smallest. Lower index means a larger file. The search treats the list as “try a middle setting; if it fits, try sharper; if it does not, try smaller.”

**Quality mono** (long edge, pixels):  
3300, 3000, 2800, 2600, 2500, 2400, 2350, 2300, 2200, 2000, 1900, 1750, 1650, 1500, 1350, 1200, 1050, 900, 800, 650, 500, 380, 280.

**Quality gray** is the cartesian product of long edges × qualities, edges first:

- Edges: 2000, 1750, 1500, 1300, 1150, 1000, 900, 800, 720, 640, 480, 360, 260
- Qualities: 85, 80, 75, 70, 65, 55, 45, 35, 25, 16, 10

**Quality color:**

- Edges: 2200, 2000, 1750, 1500, 1300, 1150, 1000, 900, 800, 720, 640, 560, 400, 280
- Qualities: 85, 80, 75, 70, 65, 55, 45, 35, 28, 22, 14, 10

**Fast mono:** 1800, 1200, 900, 700, 500.

**Fast gray and fast color:** edges 1400, 1000, 720, 540 × qualities 55, 40, 28, 18.

The small rungs at the end of the quality ladders exist so a long document can still reach a small target. They must only be chosen when every sharper setting is over the target.

### 7.7 Binary search

```
lo, hi = 0, len(settings) - 1
best = null
while lo <= hi:
    mid = (lo + hi) // 2
    size = encode the whole document at settings[mid]   # this writes the output file
    emit "<mode> <setting> → <size> MB" with a percent and a rough ETA
    if size <= target:
        if clarity gate is on and page 1 fails the gate:
            remember this setting only if nothing else has fit yet
        else:
            best = this setting
        hi = mid - 1          # look for a sharper setting that still fits
    else:
        lo = mid + 1          # too big; look smaller
```

**The file on disk after the loop is often not `best`.** The last probe may be a sharper setting that missed the target. After the loop, if `best` is not the last setting written, encode `best` again so the output file matches the size you report.

If the search returns nothing, probe the last 8 settings from smallest toward larger (skip a setting you just wrote). Take the first that fits. This covers a ladder that is not perfectly sorted by file size (a low quality at a large edge can be smaller than a high quality at a smaller edge).

If nothing fits, return failure. Do not return the smallest oversize file as a success.

ETA: after each attempt, `elapsed / attempts * remaining_attempts`, where `remaining` starts from about `log2(len(settings)) + 2`.

### 7.8 Clarity gate

Measured on **page 1 only**, not on every page. Compare Laplacian-like edge variance (Pillow `FIND_EDGES` on a grayscale thumbnail ≤ 800 px, then the variance of that image).

Let `base` be the original page-1 score and `score` the candidate’s score. The candidate passes when `score >= max(40.0, base × 0.45)`.

If the gate rejects a setting that was under the size target, keep searching toward sharper settings. If no sharper setting both fits and passes, the under-target setting that failed the gate may still be returned (it was stored when nothing better existed). Report `clarity_score` on the result when the gate ran.

Fast strategy does not run the gate.

### 7.9 Mode order

**`color`:** search the color ladder. If nothing fits, search gray. If gray fits, note: “Colour could not reach {target} MB while staying usable. Delivered grayscale instead.” If gray also fails, raise “Could not compress below {target} MB in colour or gray.”

**`per_page`:** search the color ladder with per-page encoding. No gray fallback. Failure: “Could not compress below {target} MB in per-page mode.” If force-colour pages were set, note how many and list up to 30 of the numbers.

**`mono`:** search mono only. Failure: “Could not compress below {target} MB using mono mode.”

**`auto`:** if strategy is quality, search mono first. If mono fits, use it. If mono does not fit, search gray. If strategy is fast, skip mono and search gray only (fast is a size crush, not a bilevel pass). Gray failure: “Could not compress below {target} MB.”

**`gray`:** search gray only (after the native attempt described above).

Before returning any raster success, stat the output file. If that size is greater than the target, raise instead of returning success. The `compressed_bytes` field must be the size on disk, not an earlier probe.

### 7.10 Auto-split

If `auto_split` is on and `page_count > split_pages`:

1. Write part PDFs of `split_pages` pages (the last part may be shorter) with PyMuPDF `insert_pdf`.
2. Give each part a **byte budget**, not the full target. The user’s target applies to the merged file. Sharing the full target with every part and then merging was a production bug: a 10 MB target on a multi-hundred-page prospectus came back at about 34 MB.

Budget rule (`split_part_targets`):

- Let `counts` be the page counts of the parts.
- `reserve = min(max(target_bytes / 50, 32 KiB), target_bytes / 5)`.
- `budget = max(number_of_parts, target_bytes - reserve)`.
- Part `i` gets `max(1, int(budget * counts[i] / page_count))` bytes.
- If rounding makes the sum exceed `budget`, subtract 1 byte at a time from the current largest part until the sum fits or every part is 1 byte.

3. Compress each part with `auto_split=False` and `target_size_mb = budget_bytes / (1024×1024)`. Remap force-colour page numbers into that part’s local 1-based index. Progress should say the part budget and the merged target.
4. Merge the part outputs in order into the final path. Close every PyMuPDF document you open.
5. If the merged file is still over the original target, raise. Do not report success. Suggested message: “Merged file is X MB, over the Y MB target.”
6. Result `mode` is `split`. `part_paths` lists the part files so the worker can copy them to `<index>_part00.pdf`, … Note text explains that the target was shared across the parts and states the merged size.

Example: 160 pages, target 2.3 MB, 80 pages per part → two parts, about 1.13 MB each, sum under 2.3 MB after the reserve.

A part that cannot hit its share fails the whole file. That is preferred to a download that ignores the slider.

### 7.11 Streaming

Encode one page at a time into a temp directory, then build the PDF. Do not hold every page image in memory. Cancel is checked at the start of each page.

### 7.12 Result object

Return at least: output path, original bytes, compressed bytes (the file stat), page count, mode (`native`, `mono`, `gray`, `color`, `per_page`, `split`), long edge used (0 for native and split), jpeg quality if any, note, part paths, preview paths, clarity score, `native_pdf`.

`reduction_percent = (1 - compressed/original) × 100` when original is non-zero.

### 7.13 CLI

`pdf-compressor INPUT TARGET [-o OUTPUT] [--mode auto|mono|gray|color] [-v] [-q] [--open-folder]`.

Default output name: `<stem> (compressed).pdf` next to the input. Verbose prints each attempt. The CLI does not need to expose every web flag, but it must call the same `compress_pdf`.

---

## 8. Deployment

### 8.1 Environment

| Variable | Where | Default |
|---|---|---|
| `FLASK_SECRET_KEY` | web | required in production |
| `APP_URL` | web | public base URL of this app (environment) |
| `JOB_TTL_SECONDS` | web | `3600` |
| `MAX_UPLOAD_MB` | web | `500` |
| `MAX_BATCH_FILES` | web | `15` |
| `MAX_BATCH_MB` | web | `500` |
| `MAX_PAGES` | web | `3000` |
| `MAX_DISK_MB` | web and worker | `4000` |
| `SPLIT_PAGES` | web (label) and worker (behavior) | `80` |
| `MAX_PARALLEL` | worker | `3` |
| `WORKER_POLL_SECONDS` | worker | `0.75` |
| `NOTIFY_SMTP_USER` | web | empty disables mail |
| `NOTIFY_SMTP_PASS` | web | |
| `NOTIFY_ADMIN_EMAIL` | web | `chaitanya@cpcmail.in` |
| `PORT` | only for `python app.py` | `6012` |
| `BIND` | only for `python app.py` | `127.0.0.1` |

Keep web and worker limits equal. If the worker’s `SPLIT_PAGES` differs from the number shown in the UI, users will be misled.

### 8.2 nginx

**Primary site**

- HTTP redirects to HTTPS.
- TLS via the host’s certificate (Let’s Encrypt in production).
- `location = /login` is rate-limited.
- Everything else proxies to `http://127.0.0.1:6012` with `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`.
- `client_max_body_size 500m`.
- `proxy_read_timeout` and `proxy_send_timeout` 1800s.
- Include the suite’s real-IP and security-header snippets if this host sits behind Cloudflare.

**Path-prefix mount**

```
location = /pdf-compressor { return 301 /pdf-compressor/; }
location /pdf-compressor/ {
    proxy_pass http://127.0.0.1:6012/;
    proxy_set_header X-Script-Name /pdf-compressor;
    client_max_body_size 500m;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;
}
```

The trailing slash on `proxy_pass` strips the prefix. The app’s prefix middleware reads `X-Script-Name`, sets WSGI `SCRIPT_NAME`, and strips that prefix from `PATH_INFO` if it is still present. Every URL the browser builds (fetch, preview, download) must be prefixed with `request.script_root`. Templates already do this with `const BASE = {{ request.script_root|tojson }}`.

### 8.3 Local run

```
python -m venv venv
./venv/bin/pip install -r requirements.txt
FLASK_SECRET_KEY=dev PORT=6012 ./venv/bin/python app.py
./venv/bin/python worker.py
```

Open http://127.0.0.1:6012/login. Create a user row directly if you do not want to wire SMTP.

---

## 9. Security and privacy

- Session cookie is the only browser credential. Job IDs are unguessable (UUID4 hex) but still require the owning session. Do not serve a job by id alone.
- Do not put the PDF password, password hashes, or SMTP password in status JSON, history, or logs.
- The queue file contains the PDF password until the worker deletes the `.running` file. Permissions on `data/` should be readable only by the service user.
- Approval tokens are bearer secrets. They are single-use. Do not log the full URL at info level if logs are widely readable.
- Unknown employee IDs on forgot-password look like success.
- Downloads are `attachment`. Previews are JPEG renders of page 1, not the original PDF.
- Job files are deleted after an hour or when the disk cap is hit. Do not promise longer retention.
- `GET /health` is public and only exposes queue depth and disk usage.
- Uploads are restricted by extension and by PyMuPDF being able to open them. They are not executed.
- Do not commit `.env`, `data/jobs`, `*.pdf`, or `shared_users.db`.

---

## 10. Error catalog

Use these sentences so the UI and tests stay stable.

| Situation | Response |
|---|---|
| No file | 400 “Please choose one or more PDF files.” |
| Too many files | 400 “At most 15 PDFs per batch.” |
| Bad target | 400 “Target size must be a number (MB).” or “Target size must be between 0 and 50 MB.” |
| Bad mode | 400 “Mode must be auto, mono, gray, color, or per_page.” |
| Bad strategy | 400 “Strategy must be quality or fast.” |
| Not a .pdf name | 400 “Only PDF files are accepted (<name>).” |
| Empty file | 400 “<name>: Uploaded file is empty.” |
| Over per-file size | 400 “<name>: Each PDF must be ≤ 500 MB.” |
| Over batch size | 400 “Total upload exceeds 500 MB.” |
| Bad PDF | 400 “<name>: Could not read this PDF. Is the file valid?” |
| Locked, bad or missing password | 400 “<name>: PDF is password-protected. Enter the password and retry.” |
| Too many pages | 400 “<name>: PDF has N pages (limit 3000).” |
| Cannot hit target | Job file `error` with “Could not compress below X MB …” (wording depends on mode, section 7.9) or the merged-over-target sentence |
| Worker crash | status `error`, message “Worker crashed.” |
| Download before done | 409 “Job not ready.” |
| Not your job | 404 “Job not found.” |

One bad file in a batch fails the upload before any compression starts (the job directory is removed). A failure *during* compression fails only that file.

---

## 11. Acceptance tests

A rebuild is done when every row below passes. Use synthetic PDFs (embedded JPEGs or PyMuPDF text pages). Do not depend on a particular customer file, but do keep one JPEG 2000 sample if you can generate it.

### 11.1 Accounts

1. Register a new employee ID. Status is `pending`. Login returns the approval flash and does not set a session.
2. Open the approval URL. Status becomes `approved`, token is null. Login works.
3. Register the same ID again. Error “Employee ID already registered.”
4. Forgot-password for an unknown ID returns the same success flash as a real ID.
5. Forgot-password for a real ID, then login with the old password, still works. After the reset URL, only the new password works.
6. A second reset while one is pending is rejected.

### 11.2 Upload and isolation

7. Anonymous `POST /api/jobs` does not create a job.
8. User A cannot `GET` user B’s job, preview, or download (404).
9. A `.txt` renamed to `.pdf` is rejected.
10. A 2-file batch over `MAX_BATCH_FILES` is rejected when the limit is lowered in a test config.
11. Slider 0 is stored as target `0.1`.
12. `GET /health` returns `ok: true` without a cookie.

### 11.3 Size contract

13. A 6-page noisy image PDF, strategy fast, auto-split on, `split_pages=2`, target 0.55 MB: output mode `split`, file size ≤ target, and `compressed_bytes` equals the file size on disk. Three parts, each budget about 0.17 MB.
14. The same file at target 0.22 MB also finishes ≤ 0.22 MB. This is the regression for “the slider said X and the file was bigger.”
15. `split_part_targets(160, 80, 2.3 MiB)` returns two budgets whose sum is ≤ 2.3 MiB.
16. `split_part_targets(400, 80, 10 MiB)` returns five budgets whose sum is ≤ 10 MiB.
17. A short born-digital text PDF in auto mode returns `mode=native` when object compression is already under the target, and the text is still extractable (`get_text` is non-empty).
18. A scan that cannot hit an absurdly small target (for example 0.01 MB on a huge page, if you allow that target in a unit call) raises. It does not write a success result whose file is larger than the target.
19. After a gray or mono search, the bytes on disk equal the reported `compressed_bytes`. Construct a case where an early probe fits and a later sharper probe does not; the file that remains must be the one that fit.

### 11.4 Image and page behavior

20. A one-image-per-page PDF whose embedded stream is JPEG 2000 completes. It must not raise “broken data stream”.
21. Deskew: a page rotated by about 2° comes back closer to level when deskew is on, and is unchanged when deskew is off.
22. Crop: a page with a large white margin gets smaller when crop is on, and a page that is almost entirely light is left alone.
23. Per-page mode with `color_pages=1`: page 1 is a color JPEG, a grayscale text page later in the file is mono, when the target forces a small encode you can still tell them apart by image colorspace or by the presence of color.
24. Color mode falls back to gray when the color ladder cannot fit and gray can, and the note says grayscale was delivered.
25. Fast strategy does not deskew and does not run the clarity gate, and it finishes in fewer encode attempts than quality on the same file (observable in the progress log).

### 11.5 Jobs

26. Poll shows `queued`, then `running` with an increasing percent, then `done`.
27. Cancel before the worker claims the queue file yields `cancelled` and no compressed PDF.
28. Two files in one job produce both a ZIP and a merged PDF. The ZIP names end with `(compressed).pdf`. The merged page count equals the sum of the two outputs’ page counts.
29. If one of two files is corrupt in a way only the worker notices (or you force `compress_pdf` to throw), the job is `done`, `error_count` is 1, `done_count` is 1, and the good file downloads.
30. History for that user has the new job first. Another user’s history does not.
31. A job directory older than the TTL is gone after the next `GET /`.
32. Filling `data/jobs` past `MAX_DISK_MB` causes the oldest finished job to disappear and usage to fall to about 85% of the cap. A `running` job is not the first victim.

### 11.6 Hosting

33. The site root redirects anonymous users to `/login` and accepts a 500 MB-class body size at nginx (a slightly-under-limit upload is not rejected with 413).
34. A path-prefix mount at `/pdf-compressor/` serves the same app. A compress started there polls and downloads under `/pdf-compressor/api/...`, not under the site root.

---

## 12. Build order for a new developer

Do these in order. Do not start with the UI polish.

1. `compress_pdf` plus the CLI. Get tests 13–25 green with no web server. This is the product.
2. `auth.py` against a private SQLite file. Tests 1–6.
3. `app.py` upload, status, download, history. Tests 7–12 and 26–32. You can call `compress_pdf` inside the request at first.
4. Move compression into `worker.py` and the file queue. Re-run 26–29. Confirm a 300-page job does not hold a gunicorn worker.
5. Templates. Match section 3. Poll every 1 second. Prefix every URL with `script_root`.
6. nginx and the two processes. Tests 33–34.
7. Only then point `DB_PATH` at a shared user database, and only in an environment that already owns that file.

When you change a ladder, a default, or an error sentence, change this document in the same commit.
