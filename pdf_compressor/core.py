"""Core compression logic for scanned PDF documents (v2)."""

from __future__ import annotations

import io
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Literal, Sequence

from PIL import Image, ImageFilter, ImageOps, ImageStat
import pymupdf

Mode = Literal["mono", "gray", "color", "auto", "per_page"]
Strategy = Literal["quality", "fast"]

MONO_CANDIDATES = [
    3300, 3000, 2800, 2600, 2500, 2400, 2350, 2300, 2200,
    2000, 1900, 1750, 1650, 1500, 1350, 1200, 1050, 900, 800,
    650, 500, 380, 280,
]
GRAY_LONG_EDGES = [2000, 1750, 1500, 1300, 1150, 1000, 900, 800, 720, 640, 480, 360, 260]
GRAY_QUALITIES = [85, 80, 75, 70, 65, 55, 45, 35, 25, 16, 10]
COLOR_LONG_EDGES = [2200, 2000, 1750, 1500, 1300, 1150, 1000, 900, 800, 720, 640, 560, 400, 280]
COLOR_QUALITIES = [85, 80, 75, 70, 65, 55, 45, 35, 28, 22, 14, 10]

# Fast strategy: few coarse steps (online-tool style size crush)
FAST_MONO_CANDIDATES = [1800, 1200, 900, 700, 500]
FAST_GRAY_LONG_EDGES = [1400, 1000, 720, 540]
FAST_GRAY_QUALITIES = [55, 40, 28, 18]
FAST_COLOR_LONG_EDGES = [1400, 1000, 720, 540]
FAST_COLOR_QUALITIES = [55, 40, 28, 18]

CLARITY_MIN_RATIO = 0.45
CLARITY_ABS_FLOOR = 40.0
SPLIT_PAGES_DEFAULT = 80


@dataclass
class CompressionResult:
    output_path: Path
    original_bytes: int
    compressed_bytes: int
    page_count: int
    mode: str
    long_edge_px: int
    jpeg_quality: int | None = None
    note: str | None = None
    part_paths: list[Path] = field(default_factory=list)
    preview_original: Path | None = None
    preview_compressed: Path | None = None
    clarity_score: float | None = None
    native_pdf: bool = False
    cancelled: bool = False

    @property
    def reduction_percent(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return (1 - self.compressed_bytes / self.original_bytes) * 100


class CancelledError(RuntimeError):
    """Raised when the user cancels a running job."""


ProgressCb = Callable[[str, float | None, float | None], None]
CancelCb = Callable[[], bool]


def parse_target_size_mb(value: str) -> float:
    text = value.strip().lower().replace(" ", "")
    for suffix in ("mb", "m"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    size = float(text)
    if size <= 0:
        raise ValueError("Target size must be greater than 0 MB.")
    return size


def parse_page_list(spec: str | None, page_count: int | None = None) -> set[int]:
    """Parse '1,3,5-8' into 1-based page numbers. Invalid tokens ignored."""
    pages: set[int] = set()
    if not spec or not str(spec).strip():
        return pages
    for part in str(spec).replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for n in range(start, end + 1):
                if n >= 1 and (page_count is None or n <= page_count):
                    pages.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if n >= 1 and (page_count is None or n <= page_count):
                pages.add(n)
    return pages


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem} (compressed){input_path.suffix}")


def _check_cancel(cancel: CancelCb | None) -> None:
    if cancel and cancel():
        raise CancelledError("Cancelled by user.")


def _emit(
    on_progress: ProgressCb | None,
    message: str,
    percent: float | None = None,
    eta: float | None = None,
) -> None:
    if on_progress:
        on_progress(message, percent, eta)


def open_document(pdf_path: Path, password: str | None = None) -> pymupdf.Document:
    doc = pymupdf.open(pdf_path)
    if doc.is_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ValueError("PDF is password-protected. Provide the correct password.")
    return doc


def is_native_text_pdf(pdf_path: Path, password: str | None = None) -> bool:
    doc = open_document(pdf_path, password)
    try:
        pages = min(doc.page_count, 8)
        if pages == 0:
            return False
        text_chars = 0
        image_heavy = 0
        for i in range(pages):
            page = doc[i]
            text = (page.get_text("text") or "").strip()
            text_chars += len(text)
            if len(page.get_images(full=True)) >= 1 and len(text) < 40:
                image_heavy += 1
        return text_chars >= 400 and image_heavy <= pages // 2
    finally:
        doc.close()


def compress_native_pdf(
    input_path: Path,
    output_path: Path,
    on_progress: ProgressCb | None = None,
) -> int:
    import pikepdf

    _emit(on_progress, "Native PDF detected — object compression (no re-rasterize)…", 10)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(input_path) as pdf:
        pdf.save(
            output_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            recompress_flate=True,
        )
    size = output_path.stat().st_size
    _emit(on_progress, f"Native compress done → {size / 1024 / 1024:.2f} MB", 100, 0)
    return size


def deskew_image(image: Image.Image, max_angle: float = 4.0) -> Image.Image:
    gray = image.convert("L")
    thumb = gray.copy()
    thumb.thumbnail((900, 900), Image.BILINEAR)
    bw = thumb.point(lambda x: 0 if x < 180 else 255, mode="L")
    best_angle = 0.0
    best_score = -1.0
    for step in range(int(-max_angle * 2), int(max_angle * 2) + 1):
        angle = step / 2.0
        rotated = bw.rotate(angle, resample=Image.BILINEAR, fillcolor=255)
        w, h = rotated.size
        pixels = list(rotated.getdata())
        row_sums = [0] * h
        for y in range(h):
            row_sums[y] = sum(255 - pixels[y * w + x] for x in range(0, w, 3))
        mean = sum(row_sums) / h
        var = sum((v - mean) ** 2 for v in row_sums) / h
        if var > best_score:
            best_score = var
            best_angle = angle
    if abs(best_angle) < 0.3:
        return image
    return image.rotate(best_angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))


def crop_margins(image: Image.Image, pad: int = 12) -> Image.Image:
    gray = image.convert("L")
    bw = gray.point(lambda x: 0 if x < 245 else 255, mode="1")
    bbox = ImageOps.invert(bw.convert("L")).getbbox()
    if not bbox:
        return image
    l, t, r, b = bbox
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(image.width, r + pad)
    b = min(image.height, b + pad)
    if (r - l) < image.width * 0.5 or (b - t) < image.height * 0.5:
        return image
    return image.crop((l, t, r, b))


def preprocess_page(image: Image.Image, deskew: bool = True, crop: bool = True) -> Image.Image:
    out = image.convert("RGB")
    if deskew:
        out = deskew_image(out)
    if crop:
        out = crop_margins(out)
    return out


def page_is_colorful(image: Image.Image, threshold: float = 18.0) -> bool:
    small = image.convert("RGB").copy()
    small.thumbnail((200, 200), Image.BILINEAR)
    stat = ImageStat.Stat(small)
    r, g, b = stat.mean
    gray = (r + g + b) / 3
    pixels = list(small.getdata())
    chroma = sum(abs(p[0] - gray) + abs(p[1] - gray) + abs(p[2] - gray) for p in pixels) / (
        len(pixels) * 3
    )
    return chroma > threshold


def laplacian_variance(image: Image.Image) -> float:
    gray = image.convert("L")
    gray.thumbnail((800, 800), Image.BILINEAR)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    stat = ImageStat.Stat(edges)
    return float(stat.var[0]) if hasattr(stat, "var") else float(stat.stddev[0]) ** 2


def clarity_ok(original: Image.Image, candidate: Image.Image) -> tuple[bool, float, float]:
    base = laplacian_variance(original)
    score = laplacian_variance(candidate)
    floor = max(CLARITY_ABS_FLOOR, base * CLARITY_MIN_RATIO)
    return score >= floor, score, base


def resize_long_edge(image: Image.Image, long_edge_px: int) -> Image.Image:
    width, height = image.size
    if max(width, height) <= long_edge_px:
        return image
    scale = long_edge_px / max(width, height)
    return image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)


def encode_mono_page(image: Image.Image, long_edge_px: int) -> bytes:
    gray = ImageOps.autocontrast(
        resize_long_edge(image, long_edge_px).convert("L"),
        cutoff=1,
    )
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=2))
    bw = gray.point(lambda x: 255 if x > 190 else 0, mode="1")
    buf = io.BytesIO()
    bw.save(buf, format="TIFF", compression="group4")
    return buf.getvalue()


def encode_gray_page(image: Image.Image, long_edge_px: int, quality: int) -> bytes:
    gray = resize_long_edge(image, long_edge_px).convert("L")
    buf = io.BytesIO()
    gray.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def encode_color_page(image: Image.Image, long_edge_px: int, quality: int) -> bytes:
    rgb = resize_long_edge(image, long_edge_px).convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _is_tiff(data: bytes) -> bool:
    return data[:4] in (b"II*\x00", b"MM\x00*")


def build_pdf_from_image_files(paths: Sequence[Path], output_path: Path) -> int:
    import img2pdf

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as pdf_file:
        img2pdf.convert(*[str(p) for p in paths], outputstream=pdf_file, pagesize="A4")
    return os.path.getsize(output_path)


def build_pdf_from_images(image_bytes_list: list[bytes], output_path: Path) -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        paths: list[Path] = []
        ext = "tiff" if _is_tiff(image_bytes_list[0]) else "jpg"
        for index, data in enumerate(image_bytes_list):
            page_path = Path(tmpdir) / f"page_{index:04d}.{ext}"
            page_path.write_bytes(data)
            paths.append(page_path)
        return build_pdf_from_image_files(paths, output_path)


def iter_page_images(
    pdf_path: Path,
    password: str | None = None,
    deskew: bool = True,
    crop: bool = True,
    cancel: CancelCb | None = None,
    dpi: int = 250,
) -> Iterator[Image.Image]:
    doc = open_document(pdf_path, password)
    try:
        for page in doc:
            _check_cancel(cancel)
            images = page.get_images(full=True)
            img = None
            # Fast path: single embedded raster. Fall back to pixmap when the
            # stream is JPX/JPEG2000 or otherwise undecodable by Pillow
            # ("broken data stream when reading image file").
            if len(images) == 1:
                xref = images[0][0]
                try:
                    info = doc.extract_image(xref)
                    ext = (info.get("ext") or "").lower()
                    # JPX often fails in Pillow (CMYK/soft-mask/truncated streams)
                    if ext not in ("jpx", "jp2", "j2k", "jpf"):
                        raw = info.get("image") or b""
                        if raw:
                            pil = Image.open(io.BytesIO(raw))
                            pil.load()
                            img = pil.convert("RGB")
                            pil.close()
                except Exception:
                    img = None
            if img is None:
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            yield preprocess_page(img, deskew=deskew, crop=crop)
    finally:
        doc.close()


def count_pages(pdf_path: Path, password: str | None = None) -> int:
    doc = open_document(pdf_path, password)
    try:
        return doc.page_count
    finally:
        doc.close()


def extract_preview_page(
    pdf_path: Path,
    password: str | None = None,
    deskew: bool = True,
    crop: bool = True,
) -> Image.Image:
    for img in iter_page_images(pdf_path, password, deskew=deskew, crop=crop):
        return img.copy()
    raise ValueError("No pages found")


def save_preview(image: Image.Image, path: Path, long_edge: int = 900) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    thumb = resize_long_edge(image.convert("RGB"), long_edge)
    thumb.save(path, format="JPEG", quality=72, optimize=True)


def _page_encode(image: Image.Image, kind: str, long_edge: int, quality: int | None) -> bytes:
    if kind == "mono":
        return encode_mono_page(image, long_edge)
    if kind == "gray":
        return encode_gray_page(image, long_edge, quality or 75)
    return encode_color_page(image, long_edge, quality or 75)


def _encode_document_streaming(
    pdf_path: Path,
    output_path: Path,
    kind: str,
    long_edge: int,
    quality: int | None,
    password: str | None,
    deskew: bool,
    crop: bool,
    per_page: bool,
    cancel: CancelCb | None,
    force_color_pages: set[int] | None = None,
    dpi: int = 250,
) -> int:
    force_color_pages = force_color_pages or set()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths: list[Path] = []
        for index, image in enumerate(
            iter_page_images(pdf_path, password, deskew=deskew, crop=crop, cancel=cancel, dpi=dpi)
        ):
            _check_cancel(cancel)
            page_num = index + 1  # 1-based
            page_kind = kind
            if per_page:
                if page_num in force_color_pages:
                    page_kind = "color"
                else:
                    page_kind = "color" if page_is_colorful(image) else "mono"
            data = _page_encode(image, page_kind, long_edge, quality)
            ext = "tiff" if _is_tiff(data) else "jpg"
            p = Path(tmpdir) / f"page_{index:04d}.{ext}"
            p.write_bytes(data)
            paths.append(p)
        if not paths:
            raise ValueError("No pages found")
        return build_pdf_from_image_files(paths, output_path)


def _binary_search_settings(
    settings: list,
    try_fn: Callable,
    target_bytes: int,
    on_progress: ProgressCb | None,
    label: str,
    cancel: CancelCb | None,
    started: float,
    pct_lo: float,
    pct_hi: float,
    clarity_check: Callable[[object], bool] | None = None,
) -> tuple[object, int] | None:
    """Find the sharpest setting that fits.

    The last encode is often not the winner: a later sharper probe can miss
    the target and leave that larger file on disk. When that happens, encode
    the winner again. If the binary search misses a fit on a sawtooth ladder,
    probe the eight smallest settings from small toward large.
    """
    lo, hi = 0, len(settings) - 1
    best: tuple[object, int] | None = None
    attempts = 0
    total_est = max(1, int(math.log2(max(len(settings), 2)) + 2))
    last_setting = None
    last_size: int | None = None

    def remember(setting, size: int, pct: float, eta: float | None) -> bool:
        nonlocal best
        if size > target_bytes:
            return False
        ok = True
        if clarity_check:
            ok = clarity_check(setting)
            if not ok:
                _emit(on_progress, f"Clarity gate rejected {setting}", pct, eta)
        if ok:
            best = (setting, size)
            return True
        if best is None:
            best = (setting, size)
        return False

    def probe(setting):
        nonlocal attempts, last_setting, last_size
        _check_cancel(cancel)
        attempts += 1
        elapsed = time.time() - started
        eta = (elapsed / attempts) * max(0, total_est - attempts) if attempts else None
        pct = pct_lo + (pct_hi - pct_lo) * min(1.0, attempts / total_est)
        size = try_fn(setting)
        last_setting = setting
        last_size = size
        _emit(
            on_progress,
            f"{label} {setting} → {size / 1024 / 1024:5.2f} MB",
            min(pct_hi, pct),
            eta,
        )
        return size, pct, eta

    while lo <= hi:
        mid = (lo + hi) // 2
        size, pct, eta = probe(settings[mid])
        if size <= target_bytes:
            remember(settings[mid], size, pct, eta)
            hi = mid - 1
        else:
            lo = mid + 1

    if best is None and settings:
        for setting in reversed(settings[-8:]):
            if setting == last_setting and last_size is not None:
                size, pct, eta = last_size, pct_hi, None
            else:
                size, pct, eta = probe(setting)
            if remember(setting, size, pct, eta):
                break

    if best is not None and best[0] != last_setting:
        size, _, _ = probe(best[0])
        best = (best[0], size)
    return best


def split_part_targets(page_count: int, pages_per_part: int, target_bytes: int) -> list[int]:
    """Byte budget for each auto-split part. The sum fits under the merged target.

    A reserve is held back so the merged PDF, not each part, is what the
    slider limits. Sharing the full target with every part made long files
    download larger than the size the user picked.
    """
    page_count = int(page_count)
    pages_per_part = int(pages_per_part)
    target_bytes = int(target_bytes)
    if page_count <= 0 or pages_per_part <= 0 or target_bytes <= 0:
        return []
    counts: list[int] = []
    left = page_count
    while left > 0:
        take = min(pages_per_part, left)
        counts.append(take)
        left -= take
    nparts = len(counts)
    reserve = min(max(target_bytes / 50, 32 * 1024), target_bytes / 5)
    budget = max(nparts, int(target_bytes - reserve))
    parts = [max(1, int(budget * counts[i] / page_count)) for i in range(nparts)]
    while sum(parts) > budget and any(size > 1 for size in parts):
        idx = max(range(nparts), key=lambda i: parts[i])
        if parts[idx] <= 1:
            break
        parts[idx] -= 1
    return parts


def _mono_settings(strategy: Strategy = "quality"):
    return list(FAST_MONO_CANDIDATES if strategy == "fast" else MONO_CANDIDATES)


def _gray_settings(strategy: Strategy = "quality"):
    edges = FAST_GRAY_LONG_EDGES if strategy == "fast" else GRAY_LONG_EDGES
    quals = FAST_GRAY_QUALITIES if strategy == "fast" else GRAY_QUALITIES
    return [(e, q) for e in edges for q in quals]


def _color_settings(strategy: Strategy = "quality"):
    edges = FAST_COLOR_LONG_EDGES if strategy == "fast" else COLOR_LONG_EDGES
    quals = FAST_COLOR_QUALITIES if strategy == "fast" else COLOR_QUALITIES
    return [(e, q) for e in edges for q in quals]


def _search_mode(
    pdf_path: Path,
    output_path: Path,
    target_bytes: int,
    mode_kind: str,
    password: str | None,
    deskew: bool,
    crop: bool,
    per_page: bool,
    use_clarity: bool,
    on_progress: ProgressCb | None,
    cancel: CancelCb | None,
    started: float,
    pct_lo: float,
    pct_hi: float,
    force_color_pages: set[int] | None = None,
    strategy: Strategy = "quality",
    dpi: int = 250,
) -> tuple[int, int | None, int, float | None] | None:
    if mode_kind == "mono":
        settings = _mono_settings(strategy)
    elif mode_kind == "gray":
        settings = _gray_settings(strategy)
    else:
        settings = _color_settings(strategy)

    first_page = None
    if use_clarity:
        try:
            first_page = extract_preview_page(pdf_path, password, deskew, crop)
        except Exception:
            use_clarity = False

    def try_fn(setting):
        if mode_kind == "mono":
            le, q = setting, None
        else:
            le, q = setting
        return _encode_document_streaming(
            pdf_path, output_path, mode_kind, le, q, password, deskew, crop,
            per_page=per_page, cancel=cancel, force_color_pages=force_color_pages, dpi=dpi,
        )

    def clarity_check(setting):
        if not use_clarity or first_page is None:
            return True
        if mode_kind == "mono":
            le, q = setting, None
            data = encode_mono_page(first_page, le)
        else:
            le, q = setting
            if mode_kind == "gray":
                data = encode_gray_page(first_page, le, q)
            else:
                data = encode_color_page(first_page, le, q)
        cand = Image.open(io.BytesIO(data)).convert("RGB")
        ok, _, _ = clarity_ok(first_page, cand)
        return ok

    found = _binary_search_settings(
        settings, try_fn, target_bytes, on_progress, mode_kind,
        cancel, started, pct_lo, pct_hi,
        clarity_check=clarity_check if use_clarity else None,
    )
    if not found:
        if first_page:
            first_page.close()
        return None
    setting, size = found
    if mode_kind == "mono":
        le, q = setting, None
    else:
        le, q = setting
    score = None
    if use_clarity and first_page is not None:
        if mode_kind == "mono":
            data = encode_mono_page(first_page, le)
        elif mode_kind == "gray":
            data = encode_gray_page(first_page, le, q or 75)
        else:
            data = encode_color_page(first_page, le, q or 75)
        cand = Image.open(io.BytesIO(data)).convert("RGB")
        _, score, _ = clarity_ok(first_page, cand)
        first_page.close()
    return le, q, size, score


def split_pdf_by_pages(
    pdf_path: Path,
    out_dir: Path,
    pages_per_part: int,
    password: str | None = None,
) -> list[Path]:
    doc = open_document(pdf_path, password)
    parts: list[Path] = []
    try:
        n = doc.page_count
        if n <= pages_per_part:
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, n, pages_per_part):
            end = min(start + pages_per_part, n) - 1
            part = out_dir / f"part_{start + 1:04d}-{end + 1:04d}.pdf"
            part_doc = pymupdf.open()
            part_doc.insert_pdf(doc, from_page=start, to_page=end)
            part_doc.save(part)
            part_doc.close()
            parts.append(part)
    finally:
        doc.close()
    return parts


def _adapt_progress(on_progress) -> ProgressCb | None:
    if on_progress is None:
        return None
    try:
        argc = on_progress.__code__.co_argcount
        # bound methods: co_argcount includes self
        if getattr(on_progress, "__self__", None) is not None:
            argc -= 1
    except Exception:
        argc = 1
    if argc >= 2:
        return on_progress

    def wrapped(msg, pct=None, eta=None, _cb=on_progress):
        _cb(msg)

    return wrapped


def compress_pdf(
    input_path: str | Path,
    target_size_mb: float | str,
    output_path: str | Path | None = None,
    mode: Mode = "auto",
    on_progress=None,
    password: str | None = None,
    deskew: bool = True,
    crop: bool = True,
    clarity_gate: bool = True,
    auto_split: bool = True,
    split_pages: int = SPLIT_PAGES_DEFAULT,
    preview_dir: Path | None = None,
    cancel: CancelCb | None = None,
    color_pages: str | set[int] | None = None,
    strategy: Strategy = "quality",
) -> CompressionResult:
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    progress = _adapt_progress(on_progress)
    strategy = "fast" if str(strategy).lower() == "fast" else "quality"
    dpi = 140 if strategy == "fast" else 250
    if strategy == "fast":
        # Online-tool style: skip expensive clarity / deskew unless caller forced them on
        # for speed; crop still helps size and is cheap.
        clarity_gate = False
        deskew = False

    if isinstance(target_size_mb, str):
        target_mb = parse_target_size_mb(target_size_mb)
    else:
        target_mb = float(target_size_mb)
        if target_mb <= 0:
            raise ValueError("Target size must be greater than 0 MB.")

    dst = Path(output_path).expanduser().resolve() if output_path else default_output_path(src)
    target_bytes = int(target_mb * 1024 * 1024)
    original_bytes = src.stat().st_size
    started = time.time()
    per_page = mode == "per_page"

    _emit(
        progress,
        f"Reading {src.name} ({original_bytes / 1024 / 1024:.2f} MB)… "
        f"[{strategy} strategy]",
        1,
    )
    _check_cancel(cancel)

    page_count = count_pages(src, password)
    if isinstance(color_pages, set):
        force_color = {n for n in color_pages if 1 <= n <= page_count}
    else:
        force_color = parse_page_list(color_pages, page_count)
    if force_color and mode == "per_page":
        _emit(
            progress,
            f"Found {page_count} page(s). Target: {target_mb:.2f} MB. "
            f"Force-colour pages: {sorted(force_color)[:20]}{'…' if len(force_color)>20 else ''}.",
            3,
        )
    else:
        _emit(progress, f"Found {page_count} page(s). Target: {target_mb:.2f} MB.", 3)

    preview_original = None
    preview_compressed = None
    if preview_dir is not None:
        try:
            first = extract_preview_page(src, password, deskew, crop)
            preview_original = preview_dir / "preview_original.jpg"
            save_preview(first, preview_original)
            first.close()
        except Exception:
            pass

    native = False
    try:
        native = is_native_text_pdf(src, password)
    except ValueError:
        raise
    except Exception:
        native = False

    if native and mode in ("auto", "gray"):
        try:
            size = compress_native_pdf(src, dst, progress)
            if size <= target_bytes:
                note = "Born-digital PDF: used object compression (text kept selectable)."
                if preview_dir and preview_original:
                    try:
                        comp = extract_preview_page(dst, None, deskew=False, crop=False)
                        preview_compressed = preview_dir / "preview_compressed.jpg"
                        save_preview(comp, preview_compressed)
                        comp.close()
                    except Exception:
                        pass
                return CompressionResult(
                    output_path=dst,
                    original_bytes=original_bytes,
                    compressed_bytes=size,
                    page_count=page_count,
                    mode="native",
                    long_edge_px=0,
                    note=note,
                    preview_original=preview_original,
                    preview_compressed=preview_compressed,
                    native_pdf=True,
                )
            _emit(progress, "Native compress still over target — rasterizing…", 15)
        except Exception as exc:
            _emit(progress, f"Native path skipped ({exc}); rasterizing…", 15)

    part_paths: list[Path] = []
    if auto_split and page_count > split_pages:
        _emit(progress, f"Auto-splitting into ~{split_pages}-page parts…", 8)
        split_dir = dst.parent / f"{dst.stem}_parts"
        parts = split_pdf_by_pages(src, split_dir, split_pages, password)
        if parts:
            budgets = split_part_targets(page_count, split_pages, target_bytes)
            if len(budgets) != len(parts):
                raise RuntimeError("Split budget count does not match part count.")
            notes = []
            for i, part in enumerate(parts):
                _check_cancel(cancel)
                part_out = split_dir / f"{i:03d}_compressed.pdf"
                part_mb = budgets[i] / (1024 * 1024)
                _emit(
                    progress,
                    f"Part {i + 1}/{len(parts)} budget {part_mb:.2f} MB "
                    f"(merged target {target_mb:.2f} MB)…",
                    8,
                )
                # Remap force-colour pages into this part's local 1-based indices
                part_start = i * split_pages + 1
                part_end = part_start + split_pages - 1
                local_color = {
                    (n - part_start + 1)
                    for n in force_color
                    if part_start <= n <= part_end
                }
                sub = compress_pdf(
                    part,
                    part_mb,
                    part_out,
                    mode="auto" if per_page else mode,
                    on_progress=on_progress,
                    password=None,
                    deskew=deskew,
                    crop=crop,
                    clarity_gate=clarity_gate,
                    auto_split=False,
                    preview_dir=None,
                    cancel=cancel,
                    color_pages=local_color if per_page else None,
                    strategy=strategy,
                )
                part_paths.append(sub.output_path)
                notes.append(f"part{i + 1}:{sub.mode}")
            merged = pymupdf.open()
            try:
                for p in part_paths:
                    src_doc = pymupdf.open(p)
                    try:
                        merged.insert_pdf(src_doc)
                    finally:
                        src_doc.close()
                merged.save(dst)
            finally:
                merged.close()
            compressed_bytes = dst.stat().st_size
            if compressed_bytes > target_bytes:
                raise RuntimeError(
                    f"Merged file is {compressed_bytes / 1024 / 1024:.2f} MB, "
                    f"over the {target_mb:.2f} MB target."
                )
            note = (
                f"Auto-split into {len(part_paths)} parts; "
                f"the {target_mb:.2f} MB target was shared across the parts. "
                f"Merged size {compressed_bytes / 1024 / 1024:.2f} MB. "
                f"[{', '.join(notes)}]"
            )
            if preview_dir:
                try:
                    comp = extract_preview_page(dst, None, deskew=False, crop=False)
                    preview_compressed = preview_dir / "preview_compressed.jpg"
                    save_preview(comp, preview_compressed)
                    comp.close()
                except Exception:
                    pass
            return CompressionResult(
                output_path=dst,
                original_bytes=original_bytes,
                compressed_bytes=compressed_bytes,
                page_count=page_count,
                mode="split",
                long_edge_px=0,
                note=note,
                part_paths=part_paths,
                preview_original=preview_original,
                preview_compressed=preview_compressed,
            )

    selected_mode = mode
    long_edge = 0
    jpeg_quality: int | None = None
    compressed_bytes = 0
    note = None
    clarity_score = None

    if mode == "color":
        _emit(progress, "Trying colour JPEG (binary search)…", 20)
        result = _search_mode(
            src, dst, target_bytes, "color", password, deskew, crop, False,
            clarity_gate, progress, cancel, started, 20, 70,
            strategy=strategy, dpi=dpi,
        )
        if result:
            long_edge, jpeg_quality, compressed_bytes, clarity_score = result
            selected_mode = "color"
        else:
            _emit(progress, "Colour could not hit target — falling back to gray…", 72)
            result = _search_mode(
                src, dst, target_bytes, "gray", password, deskew, crop, False,
                clarity_gate, progress, cancel, started, 72, 95,
                strategy=strategy, dpi=dpi,
            )
            if not result:
                raise RuntimeError(
                    f"Could not compress below {target_mb:.2f} MB in colour or gray."
                )
            long_edge, jpeg_quality, compressed_bytes, clarity_score = result
            selected_mode = "gray"
            note = (
                f"Colour could not reach {target_mb:.2f} MB while staying usable. "
                "Delivered grayscale instead."
            )
    elif mode == "per_page":
        _emit(progress, "Per-page mode (colour pages keep colour, rest mono)…", 20)
        settings = _color_settings(strategy)

        def try_fn(setting):
            le, q = setting
            return _encode_document_streaming(
                src, dst, "color", le, q, password, deskew, crop,
                per_page=True, cancel=cancel, force_color_pages=force_color, dpi=dpi,
            )

        found = _binary_search_settings(
            settings, try_fn, target_bytes, progress, "per_page",
            cancel, started, 20, 95,
        )
        if not found:
            raise RuntimeError(
                f"Could not compress below {target_mb:.2f} MB in per-page mode."
            )
        (long_edge, jpeg_quality), compressed_bytes = found
        selected_mode = "per_page"
        if force_color:
            note = (
                f"Forced colour on {len(force_color)} page(s): "
                f"{', '.join(str(n) for n in sorted(force_color)[:30])}"
                f"{'…' if len(force_color) > 30 else ''}."
            )
    else:
        if mode in ("auto", "mono") and not (strategy == "fast" and mode == "auto"):
            _emit(progress, "Trying mono (binary search, best for scans)…", 20)
            result = _search_mode(
                src, dst, target_bytes, "mono", password, deskew, crop, False,
                clarity_gate, progress, cancel, started, 20, 70,
                strategy=strategy, dpi=dpi,
            )
            if result:
                long_edge, jpeg_quality, compressed_bytes, clarity_score = result
                selected_mode = "mono"
            elif mode == "mono":
                raise RuntimeError(
                    f"Could not compress below {target_mb:.2f} MB using mono mode."
                )

        if selected_mode != "mono":
            label = "fast grayscale JPEG…" if strategy == "fast" else "Trying grayscale JPEG (binary search)…"
            _emit(progress, label, 72 if strategy != "fast" else 25)
            result = _search_mode(
                src, dst, target_bytes, "gray", password, deskew, crop, False,
                clarity_gate, progress, cancel, started,
                25 if strategy == "fast" else 72,
                95,
                strategy=strategy, dpi=dpi,
            )
            if not result:
                raise RuntimeError(
                    f"Could not compress below {target_mb:.2f} MB."
                )
            long_edge, jpeg_quality, compressed_bytes, clarity_score = result
            selected_mode = "gray"

    compressed_bytes = dst.stat().st_size
    if compressed_bytes > target_bytes:
        raise RuntimeError(f"Could not compress below {target_mb:.2f} MB.")

    if preview_dir is not None:
        try:
            comp = extract_preview_page(dst, None, deskew=False, crop=False)
            preview_compressed = preview_dir / "preview_compressed.jpg"
            save_preview(comp, preview_compressed)
            comp.close()
        except Exception:
            pass

    _emit(progress, f"Done → {compressed_bytes / 1024 / 1024:.2f} MB", 100, 0)
    return CompressionResult(
        output_path=dst,
        original_bytes=original_bytes,
        compressed_bytes=compressed_bytes,
        page_count=page_count,
        mode=selected_mode,
        long_edge_px=long_edge or 0,
        jpeg_quality=jpeg_quality,
        note=note,
        part_paths=part_paths,
        preview_original=preview_original,
        preview_compressed=preview_compressed,
        clarity_score=clarity_score,
        native_pdf=native,
    )
