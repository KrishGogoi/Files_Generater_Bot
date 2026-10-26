r"""Word (.docx) -> `SourceDocument`.

Three things here are load-bearing, and all three were bugs in the original
scripts:

1. **True document order.** `doc.paragraphs` then `doc.tables` interleaves
   nothing: a 60-table question bank would come out as 59 blank paragraphs
   followed by 60 tables. We walk `doc.element.body` children instead and map
   each `<w:p>` / `<w:tbl>` element back to its python-docx object, exactly as
   `PPT_FROM_NORMAL_FILE.parse_questions` does.

2. **Soft breaks are real lines.** In the box format all four options live in
   one cell as "A. ...⏎B. ...⏎C. ...⏎D. ...", so every "\n" inside a paragraph
   becomes its own `SourceLine`. Without that split the parsers see one line
   holding four options.

3. **Merged cells are one cell.** `row.cells` repeats the same `<w:tc>` for a
   horizontal or vertical merge; we keep the *elements* in a set (never
   `id()` -- see the note on `iter_unique_cells` in FORMATTING_BOX_FILE.py:
   lxml proxies are transient and their ids get recycled).

Table policy: a single-column table is a text container (question box, mnemonic
box, ...) so it is flattened into `LineKind.CELL` lines and the parsers read it
like flat text; a table with two or more columns is a genuine data grid and goes
to `tables` only, so its cell text is never mistaken for question prose. A
flattened table still contributes any nested grid to `tables`.
"""

from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote

from docx import Document
from docx.document import Document as DocxDocument
from docx.image.exceptions import UnrecognizedImageError
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml import etree
from PIL import Image, UnidentifiedImageError

from ..models import DocKind, LineKind, SourceDocument, Spans
from . import _DocBuilder, _looks_like_question_header

log = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
#: `<w:br>` / `<w:cr>` render as "\n" in python-docx's run text.
_BREAK_TAGS = frozenset({_W + "br", _W + "cr"})
#: Drawing wrappers whose inner text (a text box) must not affect line counting.
_DRAWING_TAGS = frozenset({"drawing", "pict", "AlternateContent", "object"})
#: Deep nesting is always a layout hack, not data; stop flattening there.
_MAX_TABLE_DEPTH = 6


def open_docx(path: str | Path) -> DocxDocument:
    """`Document(path)`, but tolerant of relationships to parts that do not exist.

    Some editors and converters leave an internal relationship pointing at a part
    that was never written -- typically an image whose target is the literal
    string "NULL". Word shows a missing picture and moves on; python-docx raises
    `KeyError: There is no item named 'word/NULL' in the archive` and the whole
    file is lost. On that error the package is rebuilt in memory with every
    dangling internal relationship marked external, so the rId still resolves
    (the image reader then skips it with a warning) and the text is read as usual.
    """
    try:
        return Document(str(path))
    except KeyError as exc:
        log.warning("%s: %s - repairing dangling relationships", path, exc)
        repaired, fixed = _repair_dangling_rels(Path(path).read_bytes())
        if not fixed:
            raise
        log.info("%s: marked %d dangling relationship(s) external", path, fixed)
        return Document(io.BytesIO(repaired))


def _repair_dangling_rels(data: bytes) -> tuple[bytes, int]:
    """Return `(package_bytes, n_fixed)` with dangling internal rels made external."""
    fixed = 0
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        names = set(src.namelist())
        for info in src.infolist():
            blob = src.read(info.filename)
            if info.filename.endswith(".rels"):
                blob, n = _repair_rels_part(info.filename, blob, names)
                fixed += n
            dst.writestr(info, blob)
    return out.getvalue(), fixed


def _repair_rels_part(name: str, blob: bytes, names: set[str]) -> tuple[bytes, int]:
    """Mark each internal relationship in one .rels part whose target is absent."""
    # "word/_rels/document.xml.rels" describes "word/document.xml": targets are
    # relative to "word/". The package-level "_rels/.rels" is relative to root.
    base = posixpath.dirname(posixpath.dirname(name))
    root = etree.fromstring(blob)
    fixed = 0
    for rel in root:
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        if target.startswith("/"):
            member = target.lstrip("/")
        else:
            member = posixpath.normpath(posixpath.join(base, target))
        if member in names or unquote(member) in names:
            continue
        rel.set("TargetMode", "External")
        fixed += 1
    if not fixed:
        return blob, 0
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True), fixed


def read_docx(path: str | Path) -> SourceDocument:
    """Flatten a .docx into ordered lines plus its images and data grids."""
    doc = open_docx(path)
    builder = _DocBuilder(str(path), DocKind.DOCX)
    walker = _DocxWalker(doc, builder)
    walker.walk()
    if walker.boxes:
        log.debug("%s: %d question-box tables flattened", path, walker.boxes)
    return builder.finish()


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #


def _local(tag: object) -> str:
    """Local name of an lxml tag; "" for comments and processing instructions."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _paragraph_image_rels(p_el: object) -> list[tuple[int, str]]:
    """Return `(visual_line_offset, relationship_id)` for images in a paragraph.

    The offset counts `<w:br>`s seen before the picture, so an image sitting on
    the third soft line of a paragraph anchors to that line rather than to the
    paragraph's first or last one. Breaks inside a drawing (a text box) are
    skipped because they do not contribute to the paragraph's own text.
    """
    found: list[tuple[int, str]] = []
    sub = 0

    def walk(element: object, in_drawing: bool) -> None:
        nonlocal sub
        for child in element:  # type: ignore[attr-defined]
            tag = child.tag
            name = _local(tag)
            if name == "blip":                       # DrawingML picture
                rel_id = child.get(qn("r:embed"))
                if rel_id:
                    found.append((sub, rel_id))
                continue
            if name == "imagedata":                  # legacy VML picture
                rel_id = child.get(qn("r:id"))
                if rel_id:
                    found.append((sub, rel_id))
                continue
            if not in_drawing and tag in _BREAK_TAGS:
                sub += 1
                continue
            walk(child, in_drawing or name in _DRAWING_TAGS)

    walk(p_el, False)
    return found


def _element_image_rels(element: object) -> list[str]:
    """Every image relationship id anywhere under `element`, in document order."""
    return [rel_id for _, rel_id in _paragraph_image_rels(element)]


def _image_dims(blob: bytes, part: object) -> tuple[int, int, str] | None:
    """Pixel size and format of an embedded image, or None if unreadable."""
    try:
        with Image.open(io.BytesIO(blob)) as img:
            return img.width, img.height, (img.format or "png").lower()
    except (UnidentifiedImageError, OSError, ValueError):
        # PIL cannot open EMF/WMF; the docx image header still knows the size.
        pass
    try:
        meta = part.image  # type: ignore[attr-defined]
        return int(meta.px_width), int(meta.px_height), str(meta.ext).lower()
    except (AttributeError, ValueError, KeyError, UnrecognizedImageError):
        return None


# --------------------------------------------------------------------------- #
# Bold runs
# --------------------------------------------------------------------------- #


def _iter_runs(para: Paragraph) -> list[object]:
    """Every run of a paragraph in reading order, hyperlink runs included.

    `Paragraph.runs` skips runs nested inside a ``<w:hyperlink>``, yet
    `Paragraph.text` includes their text, so relying on `runs` alone would put
    the offsets out of step for any paragraph containing a link.
    """
    try:
        out: list[object] = []
        for item in para.iter_inner_content():
            runs = getattr(item, "runs", None)          # a Hyperlink
            out.extend(runs if runs is not None else [item])
        return out
    except AttributeError:      # python-docx older than 1.1
        return list(para.runs)


def _run_is_bold(run: object) -> bool:
    """True when this run renders bold, following the style chain when it must.

    A direct ``<w:b/>`` is the common case. `None` means "inherit", so the run's
    character style (and its bases) decide -- that is how a document whose
    emphasis comes from a style, not from the B button, still exports bold.
    """
    try:
        direct = run.bold  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False
    if direct is not None:
        return bool(direct)
    try:
        style = run.style  # type: ignore[attr-defined]
        depth = 0
        while style is not None and depth < 8:      # guard a cyclic basedOn
            value = style.font.bold
            if value is not None:
                return bool(value)
            style = style.base_style
            depth += 1
    except (AttributeError, ValueError, KeyError):
        pass
    return False


def _merge_spans(spans: list[tuple[int, int]]) -> Spans:
    """Sort and coalesce touching/overlapping ranges (one run per bold stretch)."""
    if not spans:
        return ()
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return tuple(out)


def _bold_spans(para: Paragraph, text: str) -> Spans:
    """Character ranges of `text` that the document shows in bold.

    Run text is located inside `text` with a moving cursor rather than by
    summing lengths: a run holding something `Paragraph.text` does not report
    (a footnote reference, a field) would otherwise shift every later offset.
    A run that cannot be located at all aborts the whole paragraph -- unstyled
    output is a small loss, mis-placed bold is a wrong document.
    """
    if not text:
        return ()
    spans: list[tuple[int, int]] = []
    cursor = 0
    for run in _iter_runs(para):
        try:
            body = run.text  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            return ()
        if not body:
            continue
        at = text.find(body, cursor)
        if at < 0:
            return ()
        cursor = at + len(body)
        if _run_is_bold(run):
            spans.append((at, cursor))
    return _merge_spans(spans)


def _cell_grid(table: Table, builder: _DocBuilder) -> list[list[_Cell]]:
    """Rows of cells, resilient to merge layouts python-docx cannot resolve."""
    grid: list[list[_Cell]] = []
    try:
        rows = list(table.rows)
    except (IndexError, ValueError, AttributeError) as exc:
        builder.warn(f"table rows unreadable ({exc}); reading raw <w:tr>")
        return [
            [_Cell(tc, table) for tc in tr.tc_lst]
            for tr in table._tbl.tr_lst
        ]
    for row in rows:
        try:
            grid.append(list(row.cells))
        except (IndexError, ValueError, KeyError, AttributeError) as exc:
            # Broken gridSpan/vMerge: the layout is unresolvable but the raw
            # <w:tc> children are still perfectly readable.
            builder.warn(f"table row layout unreadable ({exc}); using raw cells")
            grid.append([_Cell(tc, table) for tc in row._tr.tc_lst])
    return grid


# --------------------------------------------------------------------------- #
# Body walker
# --------------------------------------------------------------------------- #


class _DocxWalker:
    """Walks one document body in true order, filling a `_DocBuilder`."""

    def __init__(self, doc: Document, builder: _DocBuilder) -> None:
        self.doc = doc
        self.b = builder
        self.rels = doc.part.rels
        # element -> object maps: the body gives us raw XML, python-docx gives us
        # text extraction, and these two dicts are the bridge between them.
        self.para_map = {p._element: p for p in doc.paragraphs}
        self.tbl_map = {t._element: t for t in doc.tables}
        self.para_pos = {p._element: i for i, p in enumerate(doc.paragraphs)}
        self.tbl_pos = {t._element: i for i, t in enumerate(doc.tables)}
        self.boxes = 0
        self._block = -1
        self._orphan_tables = 0

    # -- body -------------------------------------------------------------- #

    def walk(self) -> None:
        self._walk_body(self.doc.element.body)

    def _walk_body(self, container: object) -> None:
        for child in container:  # type: ignore[attr-defined]
            name = _local(child.tag)
            if name == "p":
                self._block += 1
                self._paragraph(
                    child, self._para_ref(child), self._block, LineKind.PARAGRAPH
                )
            elif name == "tbl":
                self._block += 1
                self._table(child, "", self._table_label(child), self._block, 0)
            elif name == "sdt":
                # A content control (Word form field) wraps ordinary body
                # content that `doc.paragraphs` cannot see, so descend into it.
                for sub in child:
                    if _local(sub.tag) == "sdtContent":
                        self._walk_body(sub)

    def _para_ref(self, p_el: object) -> str | None:
        """"p:<n>" with n == index in `doc.paragraphs`; None for wrapped ones."""
        pos = self.para_pos.get(p_el)
        return None if pos is None else f"p:{pos}"

    def _table_label(self, tbl_el: object) -> str:
        """"tbl:<n>" with n == index in `doc.tables`.

        A table Word hid inside a content control is absent from `doc.tables`,
        so it gets its own "xtbl:<n>" space rather than colliding with a real
        index the Word exporter would then resolve to the wrong table.
        """
        pos = self.tbl_pos.get(tbl_el)
        if pos is not None:
            return f"tbl:{pos}"
        self._orphan_tables += 1
        return f"xtbl:{self._orphan_tables - 1}"

    # -- paragraphs -------------------------------------------------------- #

    def _paragraph(
        self,
        p_el: object,
        ref: str | None,
        block_id: int,
        kind: LineKind,
    ) -> list[int]:
        """Emit one `SourceLine` per visual line of this paragraph."""
        para = self.para_map.get(p_el)
        if para is None:
            para = Paragraph(p_el, self.doc)
        try:
            text = para.text
        except (AttributeError, TypeError, ValueError) as exc:
            self.b.warn(f"paragraph text unreadable ({exc}); kept as blank line")
            text = ""
        try:
            bold = _bold_spans(para, text)
        except (AttributeError, TypeError, ValueError) as exc:
            # Styling is a nicety; the text is the point.
            log.debug("bold runs unreadable (%s); line kept unstyled", exc)
            bold = ()
        idxs = self.b.add_text_lines(
            text, kind=kind, block_id=block_id, ref=ref, bold=bold
        )
        for sub, rel_id in _paragraph_image_rels(p_el):
            target = idxs[min(sub, len(idxs) - 1)] if idxs else self.b.anchor
            self._image(rel_id, self.b.nonblank_at_or_before(target))
        return idxs

    # -- images ------------------------------------------------------------ #

    def _image(self, rel_id: str, anchor: int) -> None:
        """Pull one embedded picture out of the package and record it."""
        rel = self.rels.get(rel_id)
        if rel is None:
            self.b.warn(f"image relationship {rel_id} is missing from the package")
            return
        try:
            if rel.is_external:
                self.b.warn(f"image {rel_id} is linked, not embedded; skipped")
                return
            blob = rel.target_part.blob
        except (ValueError, KeyError, AttributeError) as exc:
            self.b.warn(f"embedded image {rel_id} unreadable ({exc}); skipped")
            return
        dims = _image_dims(blob, rel.target_part)
        if dims is None:
            self.b.warn(
                f"image {rel_id} ({len(blob)} bytes) is in an unsupported format"
            )
            return
        width, height, fmt = dims
        # Original bytes, never re-encoded: the export must show the user's own
        # picture, and re-saving through PIL loses EMF/animated content.
        self.b.add_image(blob, width, height, fmt, anchor=anchor)

    # -- tables ------------------------------------------------------------ #

    def _table(
        self,
        tbl_el: object,
        ref_prefix: str,
        label: str,
        block_id: int,
        depth: int,
    ) -> None:
        table = self.tbl_map.get(tbl_el)
        if table is None:
            table = Table(tbl_el, self.doc)
        grid = _cell_grid(table, self.b)
        if not grid:
            return
        n_cols = max((len(row) for row in grid), default=0)

        if depth > _MAX_TABLE_DEPTH:
            self._data_grid(tbl_el, grid)
            return
        if n_cols >= 2:
            if _is_layout_table(grid):
                self._layout_table(grid, ref_prefix, label, block_id, depth)
            else:
                self._data_grid(tbl_el, grid)
            return

        # Single column: a text container. Flatten it so the parsers see the
        # same lines they would in a flat document.
        if len(grid) >= 3 and _looks_like_question_header(grid[0][0].text):
            self.boxes += 1
        seen: set[object] = set()
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                tc = cell._tc
                if tc in seen:
                    continue     # merged cell: same <w:tc>, already emitted
                seen.add(tc)
                self._walk_cell(
                    tc,
                    f"{ref_prefix}{label}/r:{r}/c:{c}",
                    block_id,
                    depth,
                    [0, 0],
                    True,
                )

    def _layout_table(
        self,
        grid: list[list[_Cell]],
        ref_prefix: str,
        label: str,
        block_id: int,
        depth: int,
    ) -> None:
        """Flatten a multi-column table that is really a text container.

        The shape that bit: one row, two cells, the question stem in the left
        cell and its picture in the right one. Filed as a data grid, the stem
        never became a line, so the parsers saw four options and an answer with
        no question above them -- and the picture was anchored to the previous
        question. The text cells are walked first, in reading order, so the
        stem gets its line; then the pictures of the text-less cells are taken,
        anchored to that line, whichever side of it they sat on.
        """
        seen: set[object] = set()
        picture_cells: list[object] = []
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                tc = cell._tc
                if tc in seen:
                    continue
                seen.add(tc)
                if _cell_text(cell).strip():
                    self._walk_cell(
                        tc, f"{ref_prefix}{label}/r:{r}/c:{c}", block_id, depth, [0, 0], True
                    )
                else:
                    picture_cells.append(tc)
        for tc in picture_cells:
            for rel_id in _element_image_rels(tc):
                self._image(rel_id, self.b.anchor)

    def _data_grid(self, tbl_el: object, grid: list[list[_Cell]]) -> None:
        """Record a >=2-column table as a grid, anchored to the text above it."""
        rows: list[tuple[str, ...]] = []
        for row in grid:
            cells = tuple(_cell_text(cell) for cell in row)
            if any(t.strip() for t in cells):
                rows.append(cells)      # a wholly blank row is layout, not data
        self.b.add_table(rows)
        for rel_id in _element_image_rels(tbl_el):
            self._image(rel_id, self.b.anchor)

    def _walk_cell(
        self,
        container: object,
        prefix: str,
        block_id: int,
        depth: int,
        count: list[int],
        ref_ok: bool,
    ) -> None:
        """Emit a cell's paragraphs and recurse into the tables it contains.

        `count` is `[paragraph, nested table]` and is shared with the content
        controls inside the cell, but a wrapped paragraph gets `ref=None`: it is
        absent from `cell.paragraphs`, so any "p:<n>" we invented for it would
        point the Word exporter at the wrong paragraph.
        """
        for child in container:  # type: ignore[attr-defined]
            name = _local(child.tag)
            if name == "p":
                ref = f"{prefix}/p:{count[0]}" if ref_ok else None
                self._paragraph(child, ref, block_id, LineKind.CELL)
                if ref_ok:
                    count[0] += 1
            elif name == "tbl":
                self._table(
                    child, f"{prefix}/", f"tbl:{count[1]}", block_id, depth + 1
                )
                count[1] += 1
            elif name == "sdt":
                for sub in child:
                    if _local(sub.tag) == "sdtContent":
                        self._walk_cell(
                            sub, prefix, block_id, depth, count, False
                        )


def _cell_text(cell: _Cell) -> str:
    """Verbatim cell text ("" when python-docx cannot read the cell)."""
    try:
        return cell.text
    except (AttributeError, TypeError, ValueError):
        return ""


def _is_layout_table(grid: list[list[_Cell]]) -> bool:
    """True when a multi-column table holds text in at most one cell per row.

    A data grid -- "match the columns", a lab-values chart -- has text side by
    side. A table with text in one column and pictures (or nothing) in the
    other is Word being used as a page-layout tool, and its text is prose that
    the parsers must see as lines. Must contain some text at all, or there is
    nothing to flatten.
    """
    any_text = False
    seen: set[object] = set()
    for row in grid:
        filled = 0
        for cell in row:
            tc = cell._tc
            if tc in seen:
                continue            # a merged cell counts once
            seen.add(tc)
            if _cell_text(cell).strip():
                filled += 1
        if filled > 1:
            return False
        any_text = any_text or filled == 1
    return any_text
