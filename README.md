# PDF Compressor (ScanClear)

Web app at **https://pdf.cpcl.cloud** (also via suite `/pdf-compressor/`).

## Features

1. Binary-search quality ladder (stop at first fit under target)
2. Page streaming (low RAM)
3. Dedicated file-queue worker (`webapp-pdf-compressor-worker`)
4. Before/after page-1 preview
5. Cancel in-flight jobs
6. Clarity gate (Laplacian edge variance)
7. Native/text PDF object compression (pikepdf)
8. Deskew + blank-margin crop
9. Per-page colour detection mode
10. Progress % + ETA
11. Recent job history (per user)
12. Disk quota / TTL cleanup (`MAX_DISK_MB`, `JOB_TTL_SECONDS`)
13. Password-protected PDF unlock
14. Auto-split when pages &gt; `SPLIT_PAGES` (default 80)

## Process

- `webapp-pdf-compressor` — gunicorn API on `127.0.0.1:6012`
- `webapp-pdf-compressor-worker` — polls `data/queue/` and compresses

## Limits (env)

- `MAX_UPLOAD_MB=500`, `MAX_BATCH_MB=500`, `MAX_BATCH_FILES=15`
- Strategies: **Quality** (ScanClear ladder) vs **Fast** (coarse online-tool style)
- `MAX_PAGES=3000`, `SPLIT_PAGES=80`, `MAX_DISK_MB=4000`, `MAX_PARALLEL=3`
- Multi-file: each PDF compressed to the same target **in parallel**; download as **ZIP** (separate) or **merged PDF**
- Per-page mode: optional force-colour page list (`1,3,5-8`)
