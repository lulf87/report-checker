from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from mvp import table_cache
from mvp.checker import report_page_tables, sha256_file


def _grid_pdf(path: Path) -> None:
    """Two pages, each with one 3×2 ruled table."""

    document = fitz.open()
    for page_number in range(2):
        page = document.new_page(width=400, height=300)
        xs, ys = [50, 150, 250, 350], [50, 90, 130]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for row in range(2):
            for column in range(3):
                page.insert_text((xs[column] + 8, ys[row] + 25), f"p{page_number}r{row}c{column}", fontsize=10)
    document.save(path)
    document.close()


def _observed(tables: list) -> list:
    return [
        (
            tuple(table.bbox),
            [[tuple(cell) if cell is not None else None for cell in row.cells] for row in table.rows],
            [tuple(row.bbox) for row in table.rows],
            [tuple(cell) if cell is not None else None for cell in table.cells],
            table.row_count,
            table.col_count,
            table.extract(),
        )
        for table in tables
    ]


class TableCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.pdf = self.root / "grid.pdf"
        _grid_pdf(self.pdf)
        self.directory = self.root / "cache"
        self._environment = patch.dict(os.environ, {table_cache.ENVIRONMENT_VARIABLE: str(self.directory)})
        self._environment.start()
        table_cache._LOADED.clear()

    def tearDown(self) -> None:
        table_cache._LOADED.clear()
        self._environment.stop()
        self._temporary.cleanup()

    def _tables(self, *, enable: bool, **kwargs) -> list:
        with fitz.open(self.pdf) as document:
            if enable:
                table_cache.enable(document, sha256_file(self.pdf))
            result = [_observed(table_cache.find_tables(page, **kwargs)) for page in document]
        table_cache.flush()
        return result

    def test_cached_tables_equal_pymupdf_tables_cold_and_warm(self) -> None:
        direct = self._tables(enable=False)
        self.assertEqual(direct[0][0][6][0], ["p0r0c0", "p0r0c1", "p0r0c2"])
        cold = self._tables(enable=True)
        files = list(self.directory.iterdir())
        self.assertEqual(len(files), 1)
        self.assertIn(fitz.VersionBind, files[0].name)
        table_cache._LOADED.clear()  # a new process
        with patch.object(fitz.Page, "find_tables", side_effect=AssertionError("must come from the cache")):
            warm = self._tables(enable=True)
        self.assertEqual(cold, direct)
        self.assertEqual(warm, direct)

    def test_arguments_and_file_content_are_part_of_the_key(self) -> None:
        self._tables(enable=True)
        table_cache._LOADED.clear()
        with patch.object(fitz.Page, "find_tables", side_effect=AssertionError("not cached")):
            with self.assertRaises(AssertionError):
                self._tables(enable=True, strategy="lines_strict")
        other = self.root / "other.pdf"
        _grid_pdf(other)
        with fitz.open(other) as document:
            document[0].insert_text((20, 20), "different bytes")
            document.saveIncr()
        self.assertNotEqual(sha256_file(other), sha256_file(self.pdf))
        with fitz.open(other) as document:
            table_cache.enable(document, sha256_file(other))
            with patch.object(fitz.Page, "find_tables", side_effect=AssertionError("not cached")):
                with self.assertRaises(AssertionError):
                    table_cache.find_tables(document[0])

    def test_not_enabled_or_switched_off_returns_pymupdf_objects(self) -> None:
        with fitz.open(self.pdf) as document:
            self.assertNotIsInstance(table_cache.find_tables(document[0])[0], table_cache.CachedTable)
            self.assertNotIsInstance(report_page_tables(document[0])[0], table_cache.CachedTable)
        with patch.dict(os.environ, {table_cache.ENVIRONMENT_VARIABLE: "off"}):
            with fitz.open(self.pdf) as document:
                table_cache.enable(document, sha256_file(self.pdf))
                self.assertNotIsInstance(table_cache.find_tables(document[0])[0], table_cache.CachedTable)
        table_cache.flush()
        self.assertFalse(self.directory.exists())

    def test_unknown_attribute_fails_instead_of_guessing(self) -> None:
        with fitz.open(self.pdf) as document:
            table_cache.enable(document, sha256_file(self.pdf))
            table = table_cache.find_tables(document[0])[0]
        self.assertIsInstance(table, table_cache.CachedTable)
        with self.assertRaises(AttributeError):
            table.header
        rows = table.extract()
        rows[0][0] = "changed"
        self.assertEqual(table.extract()[0][0], "p0r0c0")

    def test_corrupt_cache_file_is_rebuilt(self) -> None:
        direct = self._tables(enable=False)
        self._tables(enable=True)
        cache_file = next(self.directory.iterdir())
        cache_file.write_bytes(b"not gzip")
        table_cache._LOADED.clear()
        self.assertEqual(self._tables(enable=True), direct)
        with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
            self.assertIn('"format_version"', handle.read())


class PtrOcrCacheTests(unittest.TestCase):
    def test_ocr_items_are_cached_per_backend_and_failures_are_not(self) -> None:
        from mvp import ptr_ocr

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "scan.pdf"
            _grid_pdf(pdf)
            calls: list[str] = []

            def backend(name: str, png: bytes, width: int, height: int) -> list[dict]:
                calls.append(name)
                if name == "broken":
                    raise RuntimeError("engine failed")
                return [{"text": "2.1 要求", "bbox": (10, 20, 110, 40), "confidence": 0.5}]

            table_cache._LOADED.clear()
            with patch.dict(os.environ, {table_cache.ENVIRONMENT_VARIABLE: str(root / "cache")}), \
                    patch.object(ptr_ocr, "_run_backend", side_effect=backend), \
                    patch.object(ptr_ocr, "_backend_cache_key", side_effect=lambda name: f"{name}@1@"):
                first = ptr_ocr.iter_ptr_ocr_lines(pdf, backends=["fake"])
                self.assertEqual(calls, ["fake", "fake"])
                table_cache._LOADED.clear()
                with patch.object(ptr_ocr, "_render_page", side_effect=AssertionError("must come from the cache")):
                    second = ptr_ocr.iter_ptr_ocr_lines(pdf, backends=["fake"])
                self.assertEqual(calls, ["fake", "fake"])
                self.assertEqual(second, first)
                self.assertEqual(first[0][0][2:4], ("2.1 要求", "ocr:fake"))
                self.assertEqual(first[1]["source_sha256"], hashlib.sha256(pdf.read_bytes()).hexdigest())

                for _ in range(2):  # a failing backend is tried again every time
                    table_cache._LOADED.clear()
                    lines, diagnostics = ptr_ocr.iter_ptr_ocr_lines(pdf, backends=["broken"])
                    self.assertEqual(lines, [])
                    self.assertEqual(diagnostics["status"], "error")
                self.assertEqual(calls.count("broken"), 4)
            table_cache._LOADED.clear()


if __name__ == "__main__":
    unittest.main()
