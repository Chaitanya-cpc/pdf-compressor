"""Command-line interface for PDF compression."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import compress_pdf, parse_target_size_mb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-compressor",
        description=(
            "Compress scanned PDF documents to a target file size while keeping "
            "text as sharp as possible."
        ),
        epilog=(
            "Examples:\n"
            "  pdf-compressor document.pdf 2\n"
            "  pdf-compressor document.pdf 1.5mb -o small.pdf\n"
            "  pdf-compressor document.pdf 2 --mode mono --verbose\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to the source PDF file",
    )
    parser.add_argument(
        "target_size",
        nargs="?",
        help="Target maximum file size in MB (e.g. 2, 1.5, 2mb)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output PDF path (default: <name> (compressed).pdf in same folder)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "mono", "gray", "color"],
        default="auto",
        help=(
            "Compression mode: auto (default, mono then gray), "
            "mono (best for text scans), gray (grayscale), "
            "color (colour JPEG with gray fallback)"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show each compression attempt while searching for the best fit",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print errors",
    )
    parser.add_argument(
        "--open-folder",
        action="store_true",
        help="Open the output folder when finished (Windows/macOS/Linux)",
    )
    return parser


def _open_folder(path: Path) -> None:
    folder = str(path.parent.resolve())
    if sys.platform == "win32":
        import os
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess
        subprocess.run(["open", folder], check=False)
    else:
        import subprocess
        subprocess.run(["xdg-open", folder], check=False)


def _prompt(message: str, default: str = "") -> str:
    if default:
        value = input(f"{message} [{default}]: ").strip()
        return value or default
    return input(f"{message}: ").strip()


def _run_compression(args: argparse.Namespace) -> int:
    try:
        target_mb = parse_target_size_mb(args.target_size)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    def on_progress(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        result = compress_pdf(
            input_path=args.input,
            target_size_mb=target_mb,
            output_path=args.output,
            mode=args.mode,
            on_progress=on_progress if args.verbose else None,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(
            "Error: Missing dependency. Re-run setup or install requirements.\n"
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print()
        print("=" * 50)
        print("  COMPRESSION COMPLETE")
        print("=" * 50)
        print(f"  Input:      {Path(args.input).name} ({result.original_bytes / 1024 / 1024:.2f} MB)")
        print(f"  Output:     {result.output_path}")
        print(f"  Size:       {result.compressed_bytes / 1024 / 1024:.2f} MB")
        print(f"  Reduction:  {result.reduction_percent:.1f}%")
        print(f"  Pages:      {result.page_count}")
        print("=" * 50)

    if args.open_folder:
        _open_folder(result.output_path)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.input or not args.target_size:
        if not sys.stdin.isatty():
            parser.error("input and target_size are required in non-interactive mode.")
        print()
        print("  PDF Compressor")
        print("  --------------")
        print()
        args.input = _prompt("PDF file path")
        args.target_size = _prompt("Target size in MB", "2")
        print()

    return _run_compression(args)
