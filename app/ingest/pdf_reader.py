r"""PDF -> `SourceDocument`, via PyMuPDF.

`page.get_text("dict")` gives blocks -> lines -> spans. We concatenate the spans
of a line (they are only font runs, not separate words) and sort every line on a
page by (top, left) so the result reads in document order even when MuPDF
returns the blocks in drawing order.

A page with no text layer is a scan. There is no OCR in this app, so that page
contributes a warning and no lines -- it must never abort the file, because a
100-page bank with one scanned page is still worth 99 pages of questions.

`SourceLine.page` is the 1-based page number, and `block_id` is MuPDF's block
number, which keeps the lines of one paragraph recognisably grouped.
"""

from __future__ import annotations

import logging
from pathlib import Path

try:                                  # PyMuPDF >= 1.24 prefers the new name;
    import pymupdf as fitz            # `fitz` remains the historical import.
except ImportError:                   # pragma: no cover - older PyMuPDF only
    import fitz                       # type: ignore[no-redef]

from ..models import DocKind, LineKind, SourceDocument
from . import ReadError, _DocBuilder

log = logging.getLogger(__name__)

#: Lines whose tops differ by less than this (points) are on the same visual
#: row, so they must be ordered left-to-right instead of by a hairline y gap.
_Y_TOLERANCE = 1.0


def read_pdf(path: str | Path) -> SourceDocument:
    """Flatten a PDF into ordered text lines plus its embedded images."""
    p = Path(path)
    builder = _DocBuilder(str(p), DocKind.PDF)
    with fitz.open(str(p)) as pdf:
        if getattr(pdf, "needs_pass", False):
            raise ReadError(f"{p.name}: the PDF is password-protected")
        for page_no in range(1, pdf.page_count + 1):
            try:
                _read_page(pdf, page_no, builder)
            except Exception as exc:
                # WHY blanket: MuPDF raises its own error types for a damaged
                # page. One bad page must not cost the other 99.
                builder.warn(f"page {page_no} could not be read ({exc})")
    return builder.finish()


def _read_page(pdf: object, page_no: int, builder: _DocBuilder) -> None:
    """Emit one page's lines, then anchor that page's images to them."""
    page = pdf[page_no - 1]  # type: ignore[index]
    entries = _page_lines(page)
    if not any(text.strip() for _, _, _, text in entries):
        builder.warn(
            f"page {page_no} has no text layer (scanned?) — OCR not available"
        )
    placed: list[tuple[float, int]] = []
    for n, (top, _left, block_no, text) in enumerate(entries):
        idx = builder.add_line(
            text,
            kind=LineKind.PDF_LINE,
            block_id=block_no,
            ref=f"pdf:{page_no}/l:{n}",
            page=page_no,
        )
        placed.append((top, idx))
    _read_page_images(pdf, page, page_no, builder, placed)


def _page_lines(page: object) -> list[tuple[float, float, int, str]]:
    """(top, left, block number, text) for every text line, in reading order."""
    raw = page.get_text("dict")  # type: ignore[attr-defined]
    entries: list[tuple[float, float, int, str]] = []
    for n, block in enumerate(raw.get("blocks", ())):
        if block.get("type", 0) != 0:
            continue                     # 1 == image block, handled separately
        block_no = int(block.get("number", n))
        for line in block.get("lines", ()):
            text = "".join(sp.get("text", "") for sp in line.get("spans", ()))
            bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
            entries.append((float(bbox[1]), float(bbox[0]), block_no, text))
    # Bucket the y coordinate so two spans of one visual row cannot be split by
    # a sub-point baseline difference.
    entries.sort(key=lambda e: (round(e[0] / _Y_TOLERANCE), e[1]))
    return entries


def _read_page_images(
    pdf: object,
    page: object,
    page_no: int,
    builder: _DocBuilder,
    placed: list[tuple[float, int]],
) -> None:
    """Extract the page's images, anchored to the line just above each one."""
    try:
        infos = page.get_images(full=True)  # type: ignore[attr-defined]
    except Exception as exc:               # WHY blanket: MuPDF-private errors
        builder.warn(f"page {page_no} image list unreadable ({exc})")
        return
    for info in infos:
        xref = int(info[0])
        try:
            data = pdf.extract_image(xref)  # type: ignore[attr-defined]
        except Exception as exc:            # WHY blanket: MuPDF-private errors
            builder.warn(f"page {page_no} image {xref} unreadable ({exc})")
            continue
        blob = data.get("image")
        if not blob:
            continue
        anchor = _anchor_for(page, xref, placed, builder)
        builder.add_image(
            blob,
            int(data.get("width", 0)),
            int(data.get("height", 0)),
            str(data.get("ext", "png")),
            anchor=anchor,
            page=page_no,
        )


def _anchor_for(
    page: object,
    xref: int,
    placed: list[tuple[float, int]],
    builder: _DocBuilder,
) -> int:
    """Index of the last line drawn above this image on its own page."""
    top: float | None = None
    try:
        rects = page.get_image_rects(xref)  # type: ignore[attr-defined]
        if rects:
            top = float(rects[0].y0)
    except Exception:                      # WHY blanket: MuPDF-private errors
        top = None                         # fall through to the page's last line
    if top is None:
        return builder.anchor
    above = [idx for line_top, idx in placed if line_top <= top + _Y_TOLERANCE]
    if above:
        return builder.nonblank_at_or_before(above[-1])
    if placed:
        # The figure leads the page: attribute it to the first line of this page
        # rather than to the previous page's tail, which is another question.
        return next(
            (idx for _, idx in placed if not builder.lines[idx].is_blank),
            placed[0][1],
        )
    return builder.anchor
