"""On-disk cache of PyMuPDF table detection for the PTR ↔ Report comparison.

``page.find_tables()`` plus ``table.extract()`` take over 90% of a PTR
comparison (0775: 24 s of 26 s for a 391-page Report), and the result depends
only on the PDF bytes, the PyMuPDF version and the ``find_tables`` arguments —
never on the comparison rules.  Caching exactly that layer therefore stays
valid while rules change, which is when the samples are re-run most often.

What is cached is the raw observation, not an interpretation: for every table
its bbox, the cell rectangles of every row and the extracted text matrix.
Callers get ``CachedTable`` objects that expose the same ``bbox`` / ``rows`` /
``cells`` / ``row_count`` / ``col_count`` / ``extract()`` as PyMuPDF's table.
Any other attribute raises ``AttributeError`` instead of returning a guess.

The cache is opt-in per open document (``enable``); code paths that never
call it keep receiving PyMuPDF's own objects.

Location: ``output/table-cache/`` (git-ignored; it holds text of the private
source documents).  ``REPORT_CHECKER_TABLE_CACHE`` overrides the directory;
``off`` disables the cache.

``open_store`` offers the same file handling to other rule-independent
observations of a PDF (the OCR lines of a scanned PTR, see ``ptr_ocr``).
"""

from __future__ import annotations

import functools
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

# ``import fitz`` makes PyMuPDF print a deprecation warning on *stdout*, which
# corrupts the Worker's one-line JSON reply (WORKER_PROTOCOL_INVALID).
try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older environments
    import fitz

FORMAT_VERSION = 1
ENVIRONMENT_VARIABLE = "REPORT_CHECKER_TABLE_CACHE"
_DOCUMENT_ATTRIBUTE = "_rc_table_cache"
_DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / "output" / "table-cache"

_F = TypeVar("_F", bound=Callable[..., Any])


def cache_directory() -> Path | None:
    """Directory of the cache files, or None when the cache is switched off."""

    configured = os.environ.get(ENVIRONMENT_VARIABLE, "").strip()
    if configured.lower() in {"off", "0", "false", "no"}:
        return None
    return Path(configured).expanduser() if configured else _DEFAULT_DIRECTORY


class CachedRow:
    __slots__ = ("cells", "bbox")

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.cells = [tuple(cell) if cell is not None else None for cell in data["cells"]]
        self.bbox = tuple(data["bbox"])


class CachedTable:
    """The cached observation of one PyMuPDF table."""

    __slots__ = ("bbox", "rows", "cells", "row_count", "col_count", "_text")

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.bbox = tuple(data["bbox"])
        self.rows = [CachedRow(row) for row in data["rows"]]
        self.cells = [tuple(cell) if cell is not None else None for cell in data["cells"]]
        self.row_count = int(data["row_count"])
        self.col_count = int(data["col_count"])
        self._text = data["text"]

    def extract(self) -> list[list[Any]]:
        # PyMuPDF builds a new matrix on every call; callers may modify it.
        return [list(row) for row in self._text]


def _box(value: Any) -> list[float] | None:
    return [float(item) for item in value] if value is not None else None


def _observe(table: Any) -> dict[str, Any]:
    return {
        "bbox": _box(table.bbox),
        "rows": [
            {"bbox": _box(row.bbox), "cells": [_box(cell) for cell in row.cells]}
            for row in table.rows
        ],
        "cells": [_box(cell) for cell in table.cells],
        "row_count": int(table.row_count),
        "col_count": int(table.col_count),
        "text": [[None if cell is None else str(cell) for cell in row] for row in table.extract()],
    }


class _DocumentCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: dict[str, Any] = {}
        self.dirty = False
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                stored = json.load(handle)
            if stored.get("format_version") == FORMAT_VERSION and isinstance(stored.get("calls"), dict):
                self.calls = stored["calls"]
        except (OSError, EOFError, ValueError, AttributeError):
            # Missing, truncated or foreign file: start empty and rebuild.
            self.calls = {}

    def flush(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                json.dump({"format_version": FORMAT_VERSION, "calls": self.calls}, handle, ensure_ascii=False)
            os.replace(temporary, self.path)
        except OSError:
            # The cache only saves time; a read-only or full disk must not
            # fail a comparison.
            pass


# One shared object per cache file, so several opens of the same PDF within a
# process (layout, cell evidence) read the file once and see each other's pages.
_LOADED: dict[Path, _DocumentCache] = {}


def open_store(sha256: str, kind: str) -> _DocumentCache | None:
    """The shared cache object for one PDF and one kind of observation.

    ``kind`` must name everything besides the PDF bytes that the observation
    depends on (library versions, parameters).  Set ``dirty`` after adding to
    ``calls`` and call ``flush()``.
    """

    directory = cache_directory()
    if directory is None or not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
        return None
    path = directory / f"{sha256}.{re.sub(r'[^A-Za-z0-9._-]', '_', kind)}.v{FORMAT_VERSION}.json.gz"
    cache = _LOADED.get(path)
    if cache is None:
        cache = _LOADED[path] = _DocumentCache(path)
    return cache


def enable(document: Any, sha256: str) -> None:
    """Use the cache for ``find_tables`` on this open document.

    ``sha256`` must be the SHA-256 of the file the document was opened from.
    """

    cache = open_store(sha256, f"pymupdf-{fitz.VersionBind}")
    if cache is None:
        return
    try:
        setattr(document, _DOCUMENT_ATTRIBUTE, cache)
    except Exception:  # pragma: no cover - defensive, mirrors checker.py
        pass


def find_tables(page: Any, **kwargs: Any) -> list[Any]:
    """``list(page.find_tables(**kwargs).tables)``, cached when enabled."""

    cache = getattr(page.parent, _DOCUMENT_ATTRIBUTE, None)
    if cache is None:
        return list(page.find_tables(**kwargs).tables)
    pages = cache.calls.setdefault(json.dumps(kwargs, sort_keys=True, ensure_ascii=False), {})
    key = str(page.number)
    observed = pages.get(key)
    if observed is None:
        observed = pages[key] = [_observe(table) for table in page.find_tables(**kwargs).tables]
        cache.dirty = True
    return [CachedTable(table) for table in observed]


def flush() -> None:
    """Write every cache that gained pages in this process."""

    for cache in list(_LOADED.values()):
        cache.flush()


def flush_after(function: _F) -> _F:
    """Decorator: persist newly observed pages when ``function`` ends."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        finally:
            flush()

    return wrapper  # type: ignore[return-value]
