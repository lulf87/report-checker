"""Adapt Records whose handwriting was flattened into the page content.

Supported Records keep every handwritten stroke as a PDF Ink annotation.  Some
exports (first seen with iOS 27, sample 3944) drop the annotations and keep
only what the page shows:

* each handwritten stroke is a filled outline made of Bezier curves;
* straight strokes drawn with the line tool are stroked paths >= 0.8 pt wide;
* printed table borders are thin filled bars instead of stroked lines (the
  pinned PyMuPDF 1.28 table finder still detects these tables);
* an invisible OCR text layer (render mode 3) adds recognised handwriting such
  as "一" or "注机上了" to the page text.

``adapt_flattened_record`` rewrites an *in-memory* document so the existing
9706.1 / 9706.202 parsers see the structure they were built for: one hidden
Ink annotation per handwritten stroke (its approximate pen path) and no OCR
text layer.  The source file on disk is never modified, and a document that already
carries Ink annotations is returned untouched.
"""

from __future__ import annotations

import re
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz


ADAPTER_VERSION = "1.4.0"

# Printed template strokes are at most 0.72 pt; strokes drawn with the iPad
# line tool are 0.85 pt (iOS 27) or 1.04 pt (iOS 26).
USER_STROKE_MIN_WIDTH = 0.8
BEZIER_SAMPLES = (0.0, 0.25, 0.5, 0.75)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _ops(drawing: dict[str, Any]) -> str:
    return "".join(str(item[0]) for item in drawing.get("items", []))


def is_handwriting_drawing(drawing: dict[str, Any]) -> bool:
    """Return whether a vector drawing is a handwritten stroke."""

    kind = drawing.get("type") or ""
    ops = _ops(drawing)
    # Printed templates contain no curved fills; every one seen in exported
    # Records is a pen stroke, including dots and short dashes.
    if "f" in kind and "c" in ops:
        return True
    if kind == "s" and float(drawing.get("width") or 0.0) >= USER_STROKE_MIN_WIDTH:
        return True
    return False


def document_has_ink(document: fitz.Document) -> bool:
    for page in document:
        for annotation in page.annots() or []:
            if len(annotation.type) >= 2 and annotation.type[1] == "Ink":
                return True
    return False


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _bezier(p0: fitz.Point, p1: fitz.Point, p2: fitz.Point, p3: fitz.Point, t: float) -> tuple[float, float]:
    u = 1.0 - t
    x = u**3 * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t**3 * p3.x
    y = u**3 * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t**3 * p3.y
    return (float(x), float(y))


def drawing_points(drawing: dict[str, Any]) -> list[tuple[float, float]]:
    """Sample a drawing's outline as an ordered polyline."""

    points: list[tuple[float, float]] = []

    def add(point: tuple[float, float]) -> None:
        if not points or abs(points[-1][0] - point[0]) > 1e-3 or abs(points[-1][1] - point[1]) > 1e-3:
            points.append(point)

    for item in drawing.get("items", []):
        op = item[0]
        if op == "l":
            add((float(item[1].x), float(item[1].y)))
            add((float(item[2].x), float(item[2].y)))
        elif op == "c":
            for t in BEZIER_SAMPLES:
                add(_bezier(item[1], item[2], item[3], item[4], t))
            add((float(item[4].x), float(item[4].y)))
        elif op == "re":
            rect = fitz.Rect(item[1])
            for corner in (rect.tl, rect.tr, rect.br, rect.bl, rect.tl):
                add((float(corner.x), float(corner.y)))
        elif op == "qu":
            quad = item[1]
            for corner in (quad.ul, quad.ur, quad.lr, quad.ll, quad.ul):
                add((float(corner.x), float(corner.y)))
    return points


def _outline_chunks(drawing: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Split a filled stroke into its closed outline chunks (pen order).

    iOS draws one pen stroke as a chain of short closed outlines; a chunk
    ends where the next segment does not start at the previous end point.
    """

    chunks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    last: Any = None
    for item in drawing.get("items", []):
        op = item[0]
        if op not in ("l", "c"):
            continue
        start, end = item[1], item[-1]
        if current and last is not None and (abs(start.x - last.x) > 1e-3 or abs(start.y - last.y) > 1e-3):
            chunks.append(current)
            current = []
        current.extend(drawing_points({"items": [item]}))
        last = end
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def stroke_centerline(drawing: dict[str, Any]) -> list[tuple[float, float]]:
    """Approximate the pen path of a handwritten stroke.

    Filled strokes: the centre of each outline chunk in pen order, extended at
    both ends to the chunk point farthest from its neighbour so the stroke
    keeps its full length.  Validated against the Ink annotations of the 2948
    and 1539 Records (same strokes exported both ways).  Stroked line-tool
    paths already are pen paths.
    """

    if (drawing.get("type") or "") == "s":
        return drawing_points(drawing)
    chunks = _outline_chunks(drawing)
    if not chunks:
        return []
    centres = [
        (sum(x for x, _ in chunk) / len(chunk), sum(y for _, y in chunk) / len(chunk))
        for chunk in chunks
    ]
    if len(centres) == 1:
        points = chunks[0]
        first = max(points, key=lambda point: _dist(point, centres[0]))
        second = max(points, key=lambda point: _dist(point, first))
        return [first, second]
    start = max(chunks[0], key=lambda point: _dist(point, centres[1]))
    end = max(chunks[-1], key=lambda point: _dist(point, centres[-2]))
    return [start, *centres, end]


# ---------------------------------------------------------------------------
# Content-stream editing: drop the invisible OCR text layer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(rb"\s+|%[^\r\n]*|/[^\s/\[\]()<>{}%]*|<<|>>|<[0-9A-Fa-f\s]*>|\(|[\[\]{}]|[^\s/\[\]()<>{}%]+")


def _literal_string_end(data: bytes, start: int) -> int:
    """Return the index just past a literal string starting at ``start`` ('(')."""

    depth = 0
    index = start
    while index < len(data):
        char = data[index]
        if char == 0x5C:  # backslash escape
            index += 2
            continue
        if char == 0x28:
            depth += 1
        elif char == 0x29:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(data)


_BT_RE = re.compile(rb"(?:^|(?<=[\s\])>]))BT(?=[\s/\[(<])")


def _strip_text_blocks_safe(data: bytes, font_names: set[bytes]) -> tuple[bytes, int]:
    """Remove BT..ET blocks that select one of ``font_names``.

    Only the text blocks are tokenised (path data from the handwriting is
    large and contains no strings), and the stream is rebuilt by slicing the
    original bytes so everything else is preserved byte for byte.
    """

    pieces: list[bytes] = []
    cursor = 0
    removed = 0
    length = len(data)
    search_from = 0
    while True:
        match = _BT_RE.search(data, search_from)
        if match is None:
            break
        block_start = match.start()
        index = match.end()
        block_fonts: set[bytes] = set()
        last_name: bytes | None = None
        block_end: int | None = None
        while index < length:
            if data[index] == 0x28:
                index = _literal_string_end(data, index)
                continue
            token_match = _TOKEN_RE.match(data, index)
            if token_match is None:
                index += 1
                continue
            token = token_match.group(0)
            index = token_match.end()
            if token.startswith(b"/"):
                last_name = token[1:]
            elif token == b"Tf" and last_name is not None:
                block_fonts.add(last_name)
            elif token == b"ET":
                block_end = index
                break
        if block_end is None:
            break
        if block_fonts & font_names:
            pieces.append(data[cursor:block_start])
            pieces.append(b" ")
            cursor = block_end
            removed += 1
        search_from = block_end
    pieces.append(data[cursor:])
    return b"".join(pieces), removed


def _invisible_ocr_fonts(page: fitz.Page) -> set[bytes]:
    """Return resource names of fonts used only for invisible text."""

    invisible: set[str] = set()
    visible: set[str] = set()
    for span in page.get_texttrace():
        name = str(span.get("font") or "")
        (invisible if span.get("type") == 3 else visible).add(name)
    only_invisible = invisible - visible
    if not only_invisible:
        return set()
    names: set[bytes] = set()
    for font in page.get_fonts(full=True):
        basefont = str(font[3])
        base = basefont.split("+", 1)[-1]
        # get_texttrace truncates font names (24 characters in PyMuPDF 1.26+).
        if any(name and (base.startswith(name) or name.startswith(base)) for name in only_invisible):
            names.add(str(font[4]).encode("latin-1"))
    return names


def _remove_invisible_text(document: fitz.Document, page: fitz.Page) -> int:
    fonts = _invisible_ocr_fonts(page)
    if not fonts:
        return 0
    removed = 0
    for xref in page.get_contents():
        stream = document.xref_stream(xref)
        if not stream:
            continue
        rebuilt, count = _strip_text_blocks_safe(stream, fonts)
        if count:
            document.update_stream(xref, rebuilt)
            removed += count
    return removed


def collect_invisible_text(document: fitz.Document) -> dict[int, list[tuple[str, tuple[float, float, float, float]]]]:
    """Return the invisible handwriting-recognition text per page (0-based).

    iOS writes its own reading of the handwriting as invisible text.  The
    adapter removes that layer from the page; callers that want to use it as an
    independent reading of handwritten numbers collect it first.  Each entry is
    ``(character, bbox)`` in content order.
    """

    pages: dict[int, list[tuple[str, tuple[float, float, float, float]]]] = {}
    for page in document:
        chars: list[tuple[str, tuple[float, float, float, float]]] = []
        for span in page.get_texttrace():
            if span.get("type") != 3:
                continue
            for char in span.get("chars", ()):
                bbox = tuple(float(value) for value in char[3])
                chars.append((chr(char[0]), bbox))  # type: ignore[arg-type]
        if chars:
            pages[page.number] = chars
    return pages


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _same_color(a: Any, b: Any, tolerance: float = 0.02) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return max(abs(float(x) - float(y)) for x, y in zip(a, b)) <= tolerance


def _remove_nested_shading(page: fitz.Page, drawings: list[dict[str, Any]]) -> tuple[int, int]:
    """Drop inset duplicates of cell shading.

    iOS 27 exports a shaded table cell twice: the cell rectangle and an inset
    rectangle (the paragraph shading) of the same colour.  The inset edges add
    phantom columns to the table finder.  An inset copy is removed with a
    graphics-only redaction, and only when nothing else lies entirely inside
    it, so no border, text or handwriting can be removed with it.
    Returns ``(removed, skipped)``.
    """

    shading = [
        drawing
        for drawing in drawings
        if drawing.get("type") == "f"
        and _ops(drawing) == "re"
        and drawing.get("fill")
        and min(float(value) for value in drawing["fill"]) > 0.3
        and min(fitz.Rect(drawing["rect"]).width, fitz.Rect(drawing["rect"]).height) > 2.0
    ]
    targets: list[fitz.Rect] = []
    skipped = 0
    for inner in shading:
        inner_rect = fitz.Rect(inner["rect"])
        container = next(
            (
                outer
                for outer in shading
                if outer is not inner
                and fitz.Rect(outer["rect"]) != inner_rect
                and fitz.Rect(outer["rect"]).contains(inner_rect)
                and _same_color(outer.get("fill"), inner.get("fill"))
            ),
            None,
        )
        if container is None:
            continue
        zone = fitz.Rect(inner_rect.x0 - 0.05, inner_rect.y0 - 0.05, inner_rect.x1 + 0.05, inner_rect.y1 + 0.05)
        covered = [
            other
            for other in drawings
            if other is not inner and zone.contains(fitz.Rect(other["rect"]))
            and not (fitz.Rect(other["rect"]) == inner_rect and _same_color(other.get("fill"), inner.get("fill")))
        ]
        if covered:
            skipped += 1
            continue
        targets.append(zone)
    if not targets:
        return 0, skipped
    # MuPDF removes only one path when redaction rectangles touch, so apply
    # them in passes of mutually non-touching rectangles.
    passes: list[list[fitz.Rect]] = []
    for zone in targets:
        for batch in passes:
            if not any(zone.intersects(other) for other in batch):
                batch.append(zone)
                break
        else:
            passes.append([zone])
    for batch in passes:
        for zone in batch:
            page.add_redact_annot(zone, fill=False, cross_out=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=fitz.PDF_REDACT_TEXT_NONE,
        )
    return len(targets), skipped


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


_APPEARANCE_CACHE: "dict[int, int]" = {}


def _empty_appearance(document: fitz.Document) -> int:
    """Return the xref of one shared empty appearance stream.

    Without ``/AP`` MuPDF synthesises an appearance and pads the reported
    annotation rectangle by several points; with an (empty) appearance it
    reports ``/Rect`` exactly, like the Ink annotations iOS writes.
    """

    key = id(document)
    xref = _APPEARANCE_CACHE.get(key)
    if xref is not None and xref < document.xref_length():
        try:
            if "/Form" in document.xref_object(xref):
                return xref
        except Exception:  # pragma: no cover - defensive
            pass
    xref = document.get_new_xref()
    document.update_object(xref, "<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] >>")
    document.update_stream(xref, b" ")
    _APPEARANCE_CACHE[key] = xref
    return xref


def _add_hidden_ink_annotations(
    document: fitz.Document,
    page: fitz.Page,
    polylines: list[tuple[list[tuple[float, float]], fitz.Rect | None]],
    centerlines: list[list[tuple[float, float]] | None] | None = None,
) -> None:
    """Attach one hidden Ink annotation per polyline as raw PDF objects.

    ``Page.add_ink_annot`` builds an appearance stream per annotation, which
    costs ~14 ms each (6000+ strokes in one Record).  The parsers only read
    ``/InkList`` and ``/Rect``, so the annotations are written directly and
    flagged Hidden (``/F 2``) so renderers skip them; the visible handwriting
    stays in the page content.
    """

    if not polylines:
        return
    appearance = _empty_appearance(document)
    to_pdf = ~page.transformation_matrix
    new_refs: list[str] = []
    for index, (points, outline) in enumerate(polylines):
        pdf_points = [fitz.Point(x, y) * to_pdf for x, y in points]
        centre = centerlines[index] if centerlines is not None else None
        centre_key = ""
        if centre and len(centre) >= 2:
            centre_pdf = [fitz.Point(x, y) * to_pdf for x, y in centre]
            centre_key = " /RCCenterline [" + " ".join(f"{_fmt(point.x)} {_fmt(point.y)}" for point in centre_pdf) + "]" 
        if outline is not None:
            # Real Ink rectangles cover the drawn stroke, i.e. the outline box.
            corners = [fitz.Point(outline.x0, outline.y0) * to_pdf, fitz.Point(outline.x1, outline.y1) * to_pdf]
            xs = [point.x for point in pdf_points + corners]
            ys = [point.y for point in pdf_points + corners]
            rect = [min(xs), min(ys), max(xs), max(ys)]
        else:
            xs = [point.x for point in pdf_points]
            ys = [point.y for point in pdf_points]
            rect = [min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5]
        ink = " ".join(f"{_fmt(point.x)} {_fmt(point.y)}" for point in pdf_points)
        xref = document.get_new_xref()
        document.update_object(
            xref,
            "<< /Type /Annot /Subtype /Ink /F 2 /CA 0 /T (flattened-record-adapter) "
            f"/P {page.xref} 0 R /Rect [{' '.join(_fmt(value) for value in rect)}] "
            f"/BS << /W 0.1 >> /AP << /N {appearance} 0 R >> /InkList [[{ink}]]{centre_key} >>",
        )
        new_refs.append(f"{xref} 0 R")

    kind, value = document.xref_get_key(page.xref, "Annots")
    if kind == "xref":
        target = int(value.split()[0])
        existing = document.xref_object(target, compressed=True).strip()
        inner = existing[1:-1] if existing.startswith("[") else ""
        document.update_object(target, "[" + " ".join(filter(None, [inner.strip(), *new_refs])) + "]")
    elif kind == "array":
        inner = value.strip()[1:-1]
        document.xref_set_key(page.xref, "Annots", "[" + " ".join(filter(None, [inner.strip(), *new_refs])) + "]")
    else:
        document.xref_set_key(page.xref, "Annots", "[" + " ".join(new_refs) + "]")


STROKE_GEOMETRIES = ("outline", "centerline")


def adapt_flattened_record(document: fitz.Document, stroke_geometry: str = "outline") -> dict[str, Any]:
    """Adapt ``document`` in memory when its handwriting is flattened.

    ``stroke_geometry`` selects the Ink vertices written for each stroke:

    * ``"outline"`` (GB 9706.1): the drawn outline.  The 9706.1 parser asks
      whether ink *reaches* a checkbox, and the outline covers exactly what is
      visible.  Validated: identical results to the Ink annotations on 2948.
    * ``"centerline"`` (GB 9706.202): the approximate pen path.  The 202
      parser classifies √ / △ / × by stroke start, end and length, which a
      closed outline cannot provide.

    Returns a summary that callers store with the run result.
    """

    if stroke_geometry not in STROKE_GEOMETRIES:
        raise ValueError(f"unknown stroke_geometry {stroke_geometry!r}")

    if document_has_ink(document):
        return {"handwriting_source": "ink_annotations", "adapter_version": ADAPTER_VERSION}

    if not any(
        is_handwriting_drawing(drawing) for page in document for drawing in page.get_drawings()
    ):
        return {"handwriting_source": "none", "adapter_version": ADAPTER_VERSION}

    plans: list[tuple[fitz.Page, list[dict[str, Any]]]] = []
    shading_removed = 0
    shading_skipped = 0
    stroke_total = 0
    for page in document:
        removed, skipped = _remove_nested_shading(page, page.get_drawings())
        shading_removed += removed
        shading_skipped += skipped
        drawings = page.get_drawings()
        strokes = [drawing for drawing in drawings if is_handwriting_drawing(drawing)]
        stroke_total += len(strokes)
        plans.append((page, strokes))

    if stroke_total == 0:
        return {"handwriting_source": "none", "adapter_version": ADAPTER_VERSION}

    pages_with_strokes = 0
    removed_text_blocks = 0
    for page, strokes in plans:
        removed_text_blocks += _remove_invisible_text(document, page)
        polylines = []
        centerlines: list[list[tuple[float, float]] | None] = []
        for drawing in strokes:
            if stroke_geometry == "centerline":
                points = stroke_centerline(drawing)
                outline = fitz.Rect(drawing["rect"]) if (drawing.get("type") or "") != "s" else None
                centre = None
            else:
                points = drawing_points(drawing)
                outline = None
                # The outline answers "does ink reach this box"; the pen path
                # is kept beside it for reading simple handwritten glyphs.
                centre = stroke_centerline(drawing)
            if len(points) >= 2:
                polylines.append((points, outline))
                centerlines.append(centre)
        _add_hidden_ink_annotations(document, page, polylines, centerlines)
        if strokes:
            pages_with_strokes += 1

    return {
        "handwriting_source": "page_vector_strokes",
        "adapter_version": ADAPTER_VERSION,
        "stroke_geometry": stroke_geometry,
        "strokes": stroke_total,
        "pages_with_strokes": pages_with_strokes,
        "invisible_text_blocks_removed": removed_text_blocks,
        "nested_shading_removed": shading_removed,
        "nested_shading_skipped": shading_skipped,
    }
