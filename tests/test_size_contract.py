"""Size contract: shared split budgets, and the file on disk is the one that fit."""

import io
import os
import tempfile
import unittest
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from pdf_compressor.core import (
    GRAY_LONG_EDGES,
    GRAY_QUALITIES,
    MONO_CANDIDATES,
    _binary_search_settings,
    compress_pdf,
    split_part_targets,
)


def _mb(n: float) -> int:
    return int(n * 1024 * 1024)


def _scan_pdf(path: Path, pages: int, noise: bool) -> None:
    doc = pymupdf.open()
    try:
        for i in range(pages):
            img = Image.new("RGB", (900, 1200), "white")
            if noise:
                img = Image.frombytes("RGB", (900, 1200), os.urandom(900 * 1200 * 3))
            else:
                draw = ImageDraw.Draw(img)
                draw.rectangle((30, 30, 870, 1170), outline="black", width=3)
                for y in range(70, 1120, 34):
                    draw.text((60, y), f"Page {i + 1} employment letter line {y}", fill="black")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            page = doc.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    finally:
        doc.close()


class SplitBudgetTests(unittest.TestCase):
    def test_two_parts_stay_under_2_3_mib(self):
        target = _mb(2.3)
        budgets = split_part_targets(160, 80, target)
        self.assertEqual(len(budgets), 2)
        self.assertLessEqual(sum(budgets), target)
        self.assertTrue(all(b > 0 for b in budgets))

    def test_five_parts_stay_under_10_mib(self):
        target = _mb(10)
        budgets = split_part_targets(400, 80, target)
        self.assertEqual(len(budgets), 5)
        self.assertLessEqual(sum(budgets), target)

    def test_long_document_does_not_give_each_part_the_full_target(self):
        target = _mb(0.1)
        budgets = split_part_targets(148, 80, target)
        self.assertEqual(len(budgets), 2)
        self.assertLessEqual(sum(budgets), target)
        self.assertTrue(all(b < target for b in budgets))


class LadderTests(unittest.TestCase):
    def test_quality_ladders_include_the_small_rungs(self):
        self.assertEqual(MONO_CANDIDATES[-4:], [650, 500, 380, 280])
        self.assertEqual(GRAY_LONG_EDGES[-3:], [480, 360, 260])
        self.assertEqual(GRAY_QUALITIES[-2:], [16, 10])


class BinarySearchTests(unittest.TestCase):
    def test_rewrites_the_file_when_a_sharper_probe_misses(self):
        settings = [10, 8, 6, 4, 2]
        written: list[int] = []

        def try_fn(setting: int) -> int:
            written.append(setting)
            return setting * 100

        found = _binary_search_settings(
            settings, try_fn, 700, None, "mono", None, 0.0, 0, 100,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found[0], 6)
        self.assertEqual(written[-1], 6)

    def test_fallback_probes_smallest_when_binary_search_misses(self):
        settings = [100, 90, 80, 5, 70, 60, 50, 40, 30, 20]
        seen: list[int] = []

        def try_fn(setting: int) -> int:
            seen.append(setting)
            return setting

        found = _binary_search_settings(
            settings, try_fn, 10, None, "gray", None, 0.0, 0, 100,
        )
        self.assertEqual(found[0], 5)
        self.assertIn(5, seen)


class CompressContractTests(unittest.TestCase):
    def test_split_file_stays_under_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "scan.pdf"
            dst = Path(tmp) / "out.pdf"
            _scan_pdf(src, 6, noise=False)
            messages: list[str] = []
            result = compress_pdf(
                src,
                0.55,
                dst,
                mode="auto",
                strategy="fast",
                auto_split=True,
                split_pages=2,
                clarity_gate=False,
                deskew=False,
                on_progress=lambda msg, pct=None, eta=None: messages.append(msg),
            )
            self.assertEqual(result.mode, "split")
            self.assertEqual(len(result.part_paths), 3)
            self.assertLessEqual(dst.stat().st_size, _mb(0.55))
            self.assertEqual(result.compressed_bytes, dst.stat().st_size)
            self.assertTrue(any("budget" in m and "merged target" in m for m in messages))

            tight = Path(tmp) / "tight.pdf"
            result_tight = compress_pdf(
                src,
                0.22,
                tight,
                mode="auto",
                strategy="fast",
                auto_split=True,
                split_pages=2,
                clarity_gate=False,
                deskew=False,
            )
            self.assertEqual(result_tight.mode, "split")
            self.assertLessEqual(tight.stat().st_size, _mb(0.22))
            self.assertEqual(result_tight.compressed_bytes, tight.stat().st_size)

    def test_unreachable_target_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "noise.pdf"
            dst = Path(tmp) / "out.pdf"
            _scan_pdf(src, 2, noise=True)
            with self.assertRaises(RuntimeError):
                compress_pdf(
                    src,
                    0.01,
                    dst,
                    mode="gray",
                    strategy="fast",
                    auto_split=False,
                    clarity_gate=False,
                    deskew=False,
                )


if __name__ == "__main__":
    unittest.main()
