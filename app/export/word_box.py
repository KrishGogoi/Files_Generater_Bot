"""Box-format .docx writer -- one bordered table per question, identical every time.

Uniformity is the whole point of this module. Every question produces the same
rows in the same order with the same indents, so a 60-question handout reads
predictably from top to bottom and the option letters line up down the page:

    +-------------------------------------------+
    | Question 1                                |   header row: black on white
    | <stem lines> (+ images, + nested tables)  |
    | A.<tab>option   B.<tab>option   ...       |   one paragraph per option
    | Ans. B) option text          [AI-derived] |   when cfg.include_answer
    | Ans. B) option text                       |   the same line, repeated bold
    | Explanation: ...                          |   when present + enabled
    +-------------------------------------------+

Nothing is written outside the boxes -- the document opens on question 1.

Content is never rewritten: stems, options, answers and explanations are printed
exactly as `app.parse` sliced them out of the source, and any bold the source had
is reproduced from the ranges `app.parse.styles` recorded. The only text this
module adds of its own is structural ("Question 1", "Ans. ", the AI marker, and
an "Explanation:" label when the source has none) plus the house-style "Option "
prefix inside an Incorrect/Other-Options block.

`reformat_existing` is the original script's pass-through mode: restyle a document
the user already has, without re-parsing it, and prove the text survived.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Sequence

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.image.exceptions import (
    InvalidImageStreamError,
    UnexpectedEndOfFileError,
    UnrecognizedImageError,
)
from docx.image.image import Image as DocxImage
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Length, Pt
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.export import docx_format as fmt
from app.ingest import _looks_like_question_header
from app.ingest.docx_reader import open_docx
from app.export.verify import verify_verbatim
from app.models import (
    AnswerOrigin,
    BrandConfig,
    ImageAsset,
    Question,
    Spans,
    TableAsset,
    WordExportConfig,
    clamp_spans,
)

log = logging.getLogger(__name__)

# Width of the option label column. At 14pt Calibri "A." is ~11pt wide, so 28pt
# leaves a clear gap AND -- because it is a hanging indent -- wrapped option text
# aligns under the first character instead of under the letter.
OPTION_HANG_PT = 28.0

#: Default Word cell margins are 0.08" left + 0.08" right; leave a little more so
#: a full-width image never forces the table wider than the page.
_CELL_SLACK = Inches(0.24)

#: Pixels -> EMU at 96 dpi (the dpi Word assumes for screen-sourced images).
_EMU_PER_PX = 9525

#: Errors python-docx raises for image bytes it cannot use.
_IMAGE_ERRORS = (
    UnrecognizedImageError,
    InvalidImageStreamError,
    UnexpectedEndOfFileError,
    OSError,
    ValueError,
)


class OutputLockedError(PermissionError):
    """Raised when the destination .docx cannot be written because it is open.

    Subclasses PermissionError on purpose: existing ``except PermissionError``
    handlers keep working, but the message names the file and says what to do --
    this is by far the most common real-world failure of the original script.
    """


# --------------------------------------------------------------------------- #
# Small building blocks
# --------------------------------------------------------------------------- #


def _split_lines(text: str) -> list[str]:
    """Split stored text into visual lines without touching any character."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _para(cell: _Cell) -> Paragraph:
    """A paragraph to write into, reusing the empty one every new cell starts with.

    Without this the first line of every cell would sit under a stray blank
    paragraph, and "uniform" would become "uniformly wrong".
    """
    paras = cell.paragraphs
    if len(paras) == 1 and not paras[0].runs and not paras[0].text:
        return paras[0]
    return cell.add_paragraph()


def _plain_paragraph(cell: _Cell, text: str, bold: Spans = ()) -> Paragraph:
    p = _para(cell)
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    _add_runs(p, text, bold)
    return p


def _add_runs(p: Paragraph, text: str, bold: Spans = ()) -> None:
    """Write `text` into `p`, splitting it into runs so `bold` is reproduced.

    One run when nothing is bold -- which keeps the output of a plain document
    byte-for-byte what it was before bold preservation existed. Any "\\n" inside
    a segment still becomes a soft break and "\\t" a tab, so a multi-line option
    remains one paragraph.
    """
    if not text:
        return
    spans = clamp_spans(bold, len(text))
    if not spans:
        p.add_run(text)
        return
    at = 0
    for start, end in spans:
        if start > at:
            p.add_run(text[at:start])
        p.add_run(text[start:end]).bold = True
        at = end
    if at < len(text):
        p.add_run(text[at:])


def _line_spans(text: str, bold: Spans) -> list[tuple[str, Spans]]:
    """Split `text` into visual lines, carrying each line's share of `bold`."""
    out: list[tuple[str, Spans]] = []
    at = 0
    for line in _split_lines(text):
        end = at + len(line)
        out.append(
            (
                line,
                tuple(
                    (max(s, at) - at, min(e, end) - at)
                    for s, e in bold
                    if s < end and e > at
                ),
            )
        )
        at = end + 1        # +1 for the newline that separated them
    return out


def _text_width(doc: DocxDocument) -> Length:
    """Usable width between the margins of the first section."""
    section = doc.sections[0]
    page_w = section.page_width or Inches(8.5)
    left = section.left_margin or Inches(1)
    right = section.right_margin or Inches(1)
    width = int(page_w) - int(left) - int(right)
    # A pathological section setup must not yield a negative width.
    return Emu(max(width, int(Inches(2))))


def _image_width(img: ImageAsset, avail: Length) -> Length:
    """Natural width capped to the cell, aspect handled by python-docx."""
    natural = int(img.width) * _EMU_PER_PX if img.width else 0
    cap = int(avail)
    if natural <= 0:
        return Emu(cap)
    return Emu(min(natural, cap))


#: A JFIF APP0 segment: marker, length 16, "JFIF\0", v1.01, no density units,
#: 1x1 density, no thumbnail. See `_repair_image`.
_JFIF_APP0 = bytes.fromhex("ffe000104a46494600010100000100010000")

#: The two segments python-docx will accept as the first one of a JPEG.
_JPEG_KNOWN_APPS = (b"\xff\xe0", b"\xff\xe1")   # APP0/JFIF, APP1/EXIF


def _recognised(data: bytes) -> bool:
    """True when python-docx can identify `data` as an image it can embed."""
    try:
        DocxImage.from_blob(data)
    except _IMAGE_ERRORS:
        return False
    return True


def _repair_image(data: bytes) -> bytes | None:
    """A copy of `data` that python-docx will accept, or None if there is none.

    python-docx identifies an image by sniffing *literal signature bytes* -- it
    looks for "JFIF" or "Exif" six bytes in -- so it rejects two kinds of picture
    that Word itself embeds happily, and both are in the real corpus:

    * an **Adobe JPEG**, which opens on an APP14 segment (``ff d8 ff ee``)
      instead of the APP0/APP1 it wants. The compressed data is an ordinary RGB
      JPEG, so grafting the 18-byte JFIF header on is enough -- and it is
      lossless, which matters when the picture is a radiograph that must not be
      re-encoded.
    * a **metafile** (WMF/EMF) or anything else it has no reader for, which has
      to be rasterised. Pillow does that, and is already a dependency.
    """
    if len(data) > 4 and data[:2] == b"\xff\xd8" and data[2:4] not in _JPEG_KNOWN_APPS:
        return data[:2] + _JFIF_APP0 + data[2:]
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            mode = "RGBA" if "A" in img.getbands() else "RGB"
            buf = io.BytesIO()
            img.convert(mode).save(buf, "PNG")
        return buf.getvalue()
    except (ImportError, OSError, ValueError, KeyError) as exc:
        log.debug("could not rasterise a picture (%s bytes): %s", len(data), exc)
        return None


def _embeddable(data: bytes) -> bytes | None:
    """`data` as it stands, else a repaired copy, else None."""
    if not data:
        return None
    if _recognised(data):
        return data
    repaired = _repair_image(data)
    return repaired if repaired and _recognised(repaired) else None


def _add_image(cell: _Cell, img: ImageAsset, avail: Length) -> bool:
    """Embed one image, centred, scaled to fit. False when the bytes are unusable."""
    data = _embeddable(img.data)
    if data is None:
        # A corrupt picture is not a reason to lose the question's text -- but it
        # must be said out loud, because a silently missing radiograph in a
        # question bank is worse than a noisy export.
        log.warning("skipped an unreadable image (%s bytes)", len(img.data))
        return False
    p = _para(cell)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    try:
        p.add_run().add_picture(io.BytesIO(data), width=_image_width(img, avail))
    except _IMAGE_ERRORS as exc:
        log.warning("skipped an unreadable image (%s bytes): %s", len(img.data), exc)
        return False
    return True


def _add_nested_table(
    cell: _Cell, asset: TableAsset, cfg: WordExportConfig, avail: Length
) -> Table | None:
    """Render a `TableAsset` as a real nested Word table, verbatim cell by cell."""
    if asset.is_empty or asset.n_rows == 0 or asset.n_cols == 0:
        return None
    table = cell.add_table(rows=asset.n_rows, cols=asset.n_cols)
    fmt.set_table_borders(table._tbl, cfg)
    fmt.set_table_width_pct(table._tbl, 100.0)
    col_w = Emu(max(int(avail) // asset.n_cols, int(Inches(0.4))))
    for column in table.columns:
        column.width = col_w
    for r_idx, row in enumerate(asset.rows):
        for c_idx in range(asset.n_cols):
            tcell = table.cell(r_idx, c_idx)
            tcell.width = col_w
            # Ragged source rows are normal (merged cells) -> pad with "".
            tcell.text = row[c_idx] if c_idx < len(row) else ""
    return table


def _drop_edge_blanks(cell: _Cell) -> None:
    """Drop blank paragraphs at the top and bottom of a cell we generated.

    Source explanations often start or end on an empty line, which inside a
    one-row-per-block table reads as a gap in the border. Removing an empty
    ``<w:p>`` changes no text; a cell must keep at least one paragraph, which
    Word requires.
    """
    while len(cell.paragraphs) > 1:
        first = cell.paragraphs[0]._p
        if not fmt.is_blank_p(first):
            break
        first.getparent().remove(first)
    while len(cell.paragraphs) > 1:
        last = cell.paragraphs[-1]._p
        if not fmt.is_blank_p(last):
            break
        if getattr(last.getprevious(), "tag", None) == qn("w:tbl"):
            # A cell may not end on a table -- Word needs a paragraph after it.
            # This blank is structural, not the cosmetic gap we are trimming.
            break
        last.getparent().remove(last)


def _option_paragraph(
    cell: _Cell, label: str, text: str, bold: Spans = ()
) -> Paragraph:
    """One option: "A." + tab + text, with a hanging indent so every question's
    options line up at exactly the same x position."""
    p = _para(cell)
    pf = p.paragraph_format
    pf.left_indent = Pt(OPTION_HANG_PT)
    pf.first_line_indent = Pt(-OPTION_HANG_PT)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.tab_stops.add_tab_stop(Pt(OPTION_HANG_PT), WD_TAB_ALIGNMENT.LEFT)
    lbl = (label or "").strip() or "-"
    # The label and its text share one paragraph; \t becomes <w:tab/> and any \n
    # inside the option becomes a line break, so it stays ONE paragraph. The
    # source's bold ranges are measured against the option body, so they shift by
    # the label we put in front of it.
    head = f"{lbl}.\t"
    shift = len(head)
    _add_runs(
        p,
        head + text,
        tuple((s + shift, e + shift) for s, e in clamp_spans(bold, len(text))),
    )
    return p


# --------------------------------------------------------------------------- #
# Which row an image or grid belongs in
# --------------------------------------------------------------------------- #

#: An `ImageAsset` or a `TableAsset` -- both carry the `anchor_line` that says
#: where in the source they sat.
Anchored = ImageAsset | TableAsset


def _explanation_window(q: Question) -> tuple[int, int] | None:
    """The source-line span of the explanation, or None when it has none."""
    if not q.explanation_lines:
        return None
    return min(q.explanation_lines), max(q.explanation_lines)


def _split_by_section(
    assets: Sequence[Anchored], window: tuple[int, int] | None
) -> tuple[list[Anchored], list[Anchored]]:
    """``(stem-row assets, explanation-row assets)``, by where the source had them.

    Everything used to go in the stem row, which silently lifted every grid and
    picture out of the explanation it belonged to and stacked it under the
    question -- in the real corpus that is 4 of 4 grids in the Forensic files and
    14 of 42 pictures in the Anatomy file, all of them explanation content shown
    as if it were part of the question.

    An asset moves to the explanation row only when its anchor sits inside the
    explanation's own line span. Everything else stays with the stem, including
    an unanchored asset (``anchor_line == -1``), so a "match the columns" grid
    keeps its place and nothing can be lost by a missing anchor.
    """
    if window is None:
        return list(assets), []
    lo, hi = window
    stem = [a for a in assets if not lo <= a.anchor_line <= hi]
    expl = [a for a in assets if lo <= a.anchor_line <= hi]
    return stem, expl


def _slot(anchor: int, source_lines: Sequence[int], n_lines: int) -> int:
    """How many of `source_lines` come at or before `anchor`, capped at `n_lines`.

    The explanation is written one paragraph per source line, so this doubles as
    "emit the asset after paragraph N": 0 puts it above the first line. An
    unanchored asset goes last, which is where it went before.
    """
    if anchor < 0:
        return n_lines
    return min(sum(1 for i in source_lines if i <= anchor), n_lines)


#: One piece of an explanation: ``("line", (text, spans))``, ``("image", asset)``
#: or ``("grid", asset)``.
Item = tuple[str, object]


def _explanation_items(
    lines: Sequence[tuple[str, Spans]],
    assets: Sequence[Anchored],
    source_lines: Sequence[int],
) -> list[Item]:
    """The explanation as one ordered list of lines, pictures and grids.

    Each asset lands after the line it was anchored to, so a units chart that sat
    in the middle of an explanation stays in the middle of it instead of being
    pushed to the end.

    Slots come from `source_lines`, which is one source line per written line.
    When that correspondence does not hold, `_slot` caps the position, so a
    surprising reading puts an asset at the end rather than somewhere wrong.
    """
    n = len(lines)
    by_slot: dict[int, list[Anchored]] = {}
    # Sorted by anchor so two assets sharing a slot keep their source order.
    for asset in sorted(assets, key=lambda a: a.anchor_line):
        by_slot.setdefault(_slot(asset.anchor_line, source_lines, n), []).append(asset)

    items: list[Item] = []

    def push(slot: int) -> None:
        for asset in by_slot.get(slot, ()):
            items.append(("grid" if isinstance(asset, TableAsset) else "image", asset))

    push(0)
    for index, line_spans in enumerate(lines, start=1):
        items.append(("line", line_spans))
        push(index)
    return items


def _explanation_rows(items: Sequence[Item]) -> list[list[Item]]:
    """`items` cut into one row per embedded grid, with the prose either side.

    **Word cannot break a table row across pages when that row contains a nested
    table.** It lays out what fits and silently CLIPS the rest -- no warning, no
    overflow mark, the text is simply not drawn. A long explanation with a grid
    in it is exactly that case, and what the reader loses is everything after the
    grid: usually the whole Incorrect Options block through to the next question.

    So a grid never shares a row with a page's worth of prose. It gets a row of
    its own -- short, so it always fits -- and the prose around it gets rows with
    no nested table in them, which Word is free to break wherever it likes. The
    rules between the pieces are erased afterwards (`fmt.weld_rows`), so the
    reader still sees one unbroken box.

    The first row is always a prose row, even when empty: it carries the repeated
    answer line and the "Explanation:" label.
    """
    rows: list[list[Item]] = [[]]
    for item in items:
        if item[0] == "grid":
            rows.append([item])
            rows.append([])
        else:
            rows[-1].append(item)
    # Keep row 0 whatever happens; drop only the empties the splitting created.
    return [rows[0]] + [r for r in rows[1:] if r]


def _write_items_row(
    cell: _Cell, items: Sequence[Item], cfg: WordExportConfig, inner: Length
) -> bool:
    """Write one row's worth of items. True when it holds prose.

    A grid row is reported as prose-free so the caller does not run the keyword
    rules over it: there is nothing there to format, and its blank paragraph
    after the table is structural rather than cosmetic.
    """
    prose = False
    for kind, payload in items:
        if kind == "line":
            text, spans = payload            # type: ignore[misc]
            _plain_paragraph(cell, text, spans)
            prose = True
        elif kind == "image":
            _add_image(cell, payload, inner)  # type: ignore[arg-type]
            prose = True
        else:
            # The cell's own empty first paragraph would show as a blank line
            # above the grid; the one python-docx adds after it has to stay.
            _add_nested_table(cell, payload, cfg, inner)  # type: ignore[arg-type]
            first = cell.paragraphs[0]._p
            if fmt.is_blank_p(first) and first.getnext() is not None:
                first.getparent().remove(first)
    return prose


def _answer_text(q: Question) -> str:
    """"Ans. B) text" -- or an honest blank when nothing is known."""
    ans = q.answer
    if not ans.is_known or not ans.label:
        return "Ans. (not determined)"
    body = ans.text
    if not body.strip():
        opt = q.option(ans.label)
        body = opt.text if opt is not None else ""
    body = body.strip()
    return f"Ans. {ans.label.strip().upper()})" + (f" {body}" if body else "")


def _answer_paragraph(cell: _Cell, q: Question) -> Paragraph:
    """The answer line, wholly bold -- it is the one line a reader looks for."""
    p = _plain_paragraph(cell, _answer_text(q))
    for run in p.runs:
        run.bold = True
    return p


# --------------------------------------------------------------------------- #
# Title block
# --------------------------------------------------------------------------- #


def _add_title_block(doc: DocxDocument, brand: BrandConfig) -> bool:
    """Subject / faculty / date (and the logo when set). False when nothing given.

    Off by default (`WordExportConfig.title_block`): the box document is used as
    a question bank, where a cover block is one more thing to delete. The deck
    exporter still writes a title slide from the same `BrandConfig`.
    """
    parts = [
        p.strip()
        for p in (brand.subject, brand.faculty, brand.date_str)
        if p and p.strip()
    ]
    if not parts and not (brand.logo_path and os.path.isfile(brand.logo_path)):
        return False

    if brand.logo_path and os.path.isfile(brand.logo_path):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            p.add_run().add_picture(brand.logo_path, width=Inches(1.6))
        except _IMAGE_ERRORS as exc:
            log.warning("could not embed logo %r: %s", brand.logo_path, exc)

    if brand.subject.strip():
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(brand.subject.strip()).bold = True

    sub = " | ".join(
        s.strip() for s in (brand.faculty, brand.date_str) if s and s.strip()
    )
    if sub:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(sub)

    doc.add_paragraph()   # air between the title block and the first box
    return True


# --------------------------------------------------------------------------- #
# One question box
# --------------------------------------------------------------------------- #


def _write_question_box(
    doc: DocxDocument,
    q: Question,
    number: int,
    cfg: WordExportConfig,
    avail: Length,
) -> list[_Cell]:
    """Write one question's table. Returns the cells holding prose (explanation),
    which the caller runs the keyword/option house rules over."""
    keep_bold = bool(cfg.preserve_bold)

    # Decide up front which row each picture and grid belongs in, so neither the
    # stem row nor the explanation row can claim one that is not its own.
    window = _explanation_window(q)
    stem_images, expl_images = _split_by_section(q.images, window)
    stem_tables, expl_tables = _split_by_section(q.tables, window)

    # The stem keeps its long-standing order -- text, then pictures, then grids --
    # but each grid still takes a row of its own, for the reason in
    # `_explanation_rows`. A "match the columns" stem is short today; nothing
    # guarantees the next one will be.
    stem_rows: list[list[Item]] = [
        [
            ("line", ls)
            for ls in _line_spans(q.stem, q.stem_bold if keep_bold else ())
        ]
        + [("image", im) for im in stem_images]
    ]
    stem_rows += [[("grid", asset)] for asset in stem_tables]

    # The explanation's shape has to be known before the table is built, because
    # a grid inside it needs a row to itself -- see `_explanation_rows`.
    show_expl = bool(cfg.include_explanation and q.explanation.strip())
    expl_lines = (
        _line_spans(q.explanation, q.explanation_bold if keep_bold else ())
        if show_expl
        else []
    )
    # The fifth row is part of the shape of a question box, not something an
    # explanation brings with it: it carries the answer line a second time, with
    # the explanation underneath when there is one. So it is written whether or
    # not the question has an explanation -- `_explanation_rows` keeps its first
    # row unconditionally, so an empty item list still yields the one row.
    #
    # The exception is a configuration that would leave it genuinely blank: no
    # answer to repeat and no explanation to carry. An empty bordered row reads
    # as a mistake, and "always five rows" is about the default layout, which
    # always has an answer in it.
    keep_expl_row = show_expl or (
        cfg.include_answer and cfg.repeat_answer_in_explanation
    )
    expl_rows = (
        _explanation_rows(
            _explanation_items(
                expl_lines, [*expl_images, *expl_tables], q.explanation_lines
            )
        )
        if keep_expl_row
        else []
    )

    # Header + stem + options, plus the answer row and however many rows the stem
    # and explanation need. Built at its final shape, so no row is added later.
    n_rows = 2 + len(stem_rows) + (1 if cfg.include_answer else 0) + len(expl_rows)
    table = doc.add_table(rows=n_rows, cols=1)
    table.autofit = False
    fmt.set_table_borders(table._tbl, cfg)
    fmt.set_table_width_pct(table._tbl, 100.0)
    table.columns[0].width = avail
    for row in table.rows:
        row.cells[0].width = avail

    r = 0

    # -- row 0: header ----------------------------------------------------- #
    head = table.cell(r, 0)
    r += 1
    hp = _plain_paragraph(head, f"Question {number}")
    fmt.set_cell_fill(head._tc, cfg.question_fill)
    for run in hp.runs:
        fmt.set_run_bold_color(run._r, cfg.question_text_color)

    # -- row 1 onwards: stem, one extra row per embedded grid --------------- #
    inner = Emu(int(avail) - int(_CELL_SLACK))
    first_stem_row = r
    for items in stem_rows:
        _write_items_row(table.cell(r, 0), items, cfg, inner)
        r += 1
    for upper in range(first_stem_row, r - 1):
        fmt.weld_rows(table.cell(upper, 0)._tc, table.cell(upper + 1, 0)._tc)

    # -- options ------------------------------------------------------------ #
    opt_cell = table.cell(r, 0)
    r += 1
    for opt in q.options:
        _option_paragraph(
            opt_cell, opt.label, opt.text, opt.bold if keep_bold else ()
        )
        for img in opt.images:
            _add_image(opt_cell, img, Emu(int(avail) - int(_CELL_SLACK)))
    if not q.options:
        # Keep the row present so the structure never varies between questions.
        _plain_paragraph(opt_cell, "")

    prose_cells: list[_Cell] = []

    # -- answer -------------------------------------------------------------- #
    if cfg.include_answer:
        ans_cell = table.cell(r, 0)
        r += 1
        p = _answer_paragraph(ans_cell, q)
        if (
            q.answer.origin is AnswerOrigin.AI
            and cfg.mark_ai_answers
            and cfg.ai_answer_marker
        ):
            # Italic, so a reader can tell at a glance that this key was derived
            # rather than taken from the document.
            run = p.add_run(cfg.ai_answer_marker)
            run.italic = True
            run.bold = False

    # -- explanation, one extra row per embedded grid ----------------------- #
    first_expl_row = r
    for n, items in enumerate(expl_rows):
        exp_cell = table.cell(r, 0)
        r += 1
        if n == 0:
            # The key again, right above the explanation: by the time a reader
            # gets here the options row has scrolled off, and looking back up for
            # the letter is the one thing this row can spare them. Written on the
            # same terms as the answer row above, so the two rows always read
            # alike -- including "Ans. (not determined)" when nothing is known.
            if cfg.include_answer and cfg.repeat_answer_in_explanation:
                _answer_paragraph(exp_cell, q)
            # Only label an explanation that exists. Without the guard a question
            # with no explanation gets a bare "Explanation:" heading over nothing.
            if expl_lines and _needs_explanation_label(expl_lines):
                _plain_paragraph(exp_cell, "Explanation:").runs[0].bold = True
        if _write_items_row(exp_cell, items, cfg, inner) or n == 0:
            prose_cells.append(exp_cell)

    # The explanation was cut into rows for Word's benefit, not the reader's, so
    # erase the rules between them: on the page it stays one unbroken box.
    for upper in range(first_expl_row, r - 1):
        fmt.weld_rows(table.cell(upper, 0)._tc, table.cell(upper + 1, 0)._tc)

    return prose_cells


def _needs_explanation_label(lines: Sequence[tuple[str, Spans]]) -> bool:
    """True when this explanation has no heading of its own and wants ours.

    Two ways the answer is no. The block already says "Explanation:" somewhere --
    printing it again is the duplicate the user sees. Or it opens on a different
    keyword ("Other Options:", "Take Home Message:"), in which case an
    "Explanation:" above it would label a block that is not one.
    """
    explanation_bases = getattr(fmt, "EXPLANATION_BASES", frozenset({fmt.KW_EXPLANATION}))
    for text, _ in lines:
        if fmt.match_keyword(text) in explanation_bases:
            return False
    first = next((text for text, _ in lines if text.strip()), "")
    return fmt.match_keyword(first) is None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _save(doc: DocxDocument, out_path: str) -> str:
    """Save, turning the "it's open in Word" failure into a usable message."""
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        doc.save(out_path)
    except PermissionError as exc:
        raise OutputLockedError(
            f"Cannot write '{out_path}': the file is open in Word or locked by "
            f"another program. Close it (or choose a different name) and export "
            f"again."
        ) from exc
    return out_path


def write_box_docx(
    questions: Sequence[Question],
    out_path: str,
    cfg: WordExportConfig,
    brand: BrandConfig,
) -> str:
    """Write `questions` as uniform bordered boxes to `out_path`; return the path.

    A question that cannot be rendered (unusable image, malformed table asset) is
    logged and skipped -- one bad question never costs the user the other 59.
    """
    doc = Document()
    avail = _text_width(doc)
    if cfg.title_block:
        _add_title_block(doc, brand)

    prose_cells: list[_Cell] = []
    written = 0
    for i, q in enumerate(questions):
        number = q.number if q.number > 0 else i + 1
        try:
            prose_cells.extend(_write_question_box(doc, q, number, cfg, avail))
        except (ValueError, KeyError, IndexError, AttributeError, TypeError) as exc:
            log.exception("question %s could not be written to the box docx: %s",
                          number, exc)
            continue
        # Two adjacent tables merge into one in Word -- and questions must never
        # visually collide -- so every box is followed by a blank paragraph.
        doc.add_paragraph()
        written += 1

    # House keyword rules on the prose we emitted (an explanation copied from the
    # source often contains its own "Incorrect Options:" block).
    for cell in prose_cells:
        try:
            fmt.apply_keyword_rules(cell)
            fmt.label_incorrect_options(cell)
            _drop_edge_blanks(cell)
        except (ValueError, AttributeError, IndexError) as exc:
            log.warning("keyword formatting skipped for one cell: %s", exc)

    fmt.apply_house_style(doc, cfg)
    if cfg.color_nested_tables:
        fmt.color_nested_tables(doc, cfg)

    _save(doc, out_path)
    log.info("wrote %d question box(es) to %s", written, out_path)
    return out_path


def has_question_boxes(path: str) -> bool:
    """True when `path` is a .docx already laid out as one table per question.

    Uses the reader's own definition of a question box -- a single-column table
    of at least three rows opening on a question header -- so "this is a box
    document" means the same thing here as it does in `app.ingest`.

    Such a document needs no box writer: the boxes are already there, and the
    user asked for their file formatted rather than for a new one modelled on
    it. Rebuilding drops every row the box model has no slot for (the
    "Tags: #..." row is the one that bites) and it is what put a grid and a page
    of prose in the same unsplittable row.
    """
    try:
        doc = open_docx(path)
    except (PackageNotFoundError, OSError, ValueError, KeyError) as exc:
        log.debug("cannot probe %r for question boxes: %s", path, exc)
        return False
    for tbl in doc.element.body.findall(qn("w:tbl")):    # top level only
        table = Table(tbl, doc)
        try:
            if len(table.columns) != 1 or len(table.rows) < 3:
                continue
            if _looks_like_question_header(table.rows[0].cells[0].text):
                return True
        except (IndexError, ValueError):
            continue
    return False


def reformat_existing(
    src: str, dst: str, cfg: WordExportConfig
) -> tuple[str, bool, str]:
    """Restyle an existing .docx without re-parsing it.

    The original script's mode of operation, and the user's existing workflow:
    structural keyword/option rules, nested-table colouring, house style -- then
    prove that not one character of content changed.

    Returns ``(dst, verified_ok, detail)``. A locked destination raises
    `OutputLockedError` rather than reporting a verification failure, because
    "could not save" and "content changed" must never be confused.
    """
    doc = open_docx(src)

    # Box documents keep everything in table cells; flat documents keep their
    # keyword blocks in the body. Both get the structural rules.
    for cell in fmt.iter_unique_cells(doc):
        fmt.apply_keyword_rules(cell)
        fmt.label_incorrect_options(cell)
    fmt.apply_keyword_rules(doc)
    fmt.label_incorrect_options(doc)

    if cfg.color_nested_tables:
        fmt.color_nested_tables(doc, cfg)
    # Last, so it also covers the paragraphs the rules above inserted.
    fmt.apply_house_style(doc, cfg)

    _save(doc, dst)

    ok, detail = verify_verbatim(src, dst)
    if not ok:
        log.error("verbatim check FAILED for %s -> %s: %s", src, dst, detail)
    return dst, ok, detail
