"""Branded PowerPoint deck builder.

This is the merge of the three original PPT scripts into one configurable
builder, keeping the pieces that were tuned against real question banks:

* ``PPT_FROM_BOX_FILE_WITH_ANSWERS.py`` -- the overall structure (answer on its
  own slide), the tighter ``0.045`` line-height estimator and the 20-step
  auto-shrink loop.
* ``PPT_FROM_BOX_FILE.py`` -- the answer banner drawn on the question slide, the
  proportional column widths that keep a match-the-columns table compact, and
  the rule-position cap that stops options sliding off the bottom.
* ``PPT_FROM_NORMAL_FILE.py`` -- vertical stacking of several images.

Nothing in here writes question prose. Every word of question text comes from
the `Question` objects, which the parsers built by slicing the source verbatim
(see the architectural rule in ``app/models.py``). The only strings this module
contributes are furniture -- ``"Q3. "``, ``"Answer:"``, the AI-derived marker --
and the branding taken from `BrandConfig`.
"""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from lxml import etree
from PIL import Image
from pptx import Presentation as _new_presentation  # factory function
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.presentation import Presentation  # the class, for type hints
from pptx.shapes.autoshape import Shape
from pptx.slide import Slide, SlideLayout
from pptx.text.text import TextFrame
from pptx.util import Cm, Pt

from app.models import (
    AnswerOrigin,
    BrandConfig,
    ImageAsset,
    Option,
    PptxExportConfig,
    Question,
    TableAsset,
)

logger = logging.getLogger(__name__)

__all__ = ["build_deck", "AI_ANSWER_MARKER", "ANSWER_UNKNOWN_TEXT"]

# --------------------------------------------------------------------------- #
# The only text this module authors (furniture, never question content)
# --------------------------------------------------------------------------- #

#: Shown under an AI-derived answer. The user builds exam material from these
#: decks, so a model's guess must never look like the source's own key.
AI_ANSWER_MARKER = "AI-derived — please verify"

#: Shown instead of a letter when nothing was stated and nothing derived. A
#: blank line would read as "no answer needed"; a fabricated letter is worse.
ANSWER_UNKNOWN_TEXT = "Answer not determined"

# --------------------------------------------------------------------------- #
# Geometry
#
# `PptxExportConfig` owns the slide size, fonts, sizes and colours. The numbers
# below are the layout constants from the original decks; vertical ones are
# expressed against the reference 33.867 x 19.05 cm canvas and scaled by
# `_Geom.v()`, so a differently sized slide scales instead of pushing furniture
# off the edge.
# --------------------------------------------------------------------------- #

_REF_SLIDE_H_CM = 19.05
_QBOX_LEFT_CM = 0.47
_QBOX_TOP_CM = 0.58
_QBOX_SIDE_TOTAL_CM = 1.157       # 33.867 - 32.71, the original box width
_QBOX_MARGIN_LR_CM = 0.25
_QBOX_MARGIN_TB_CM = 0.13
_QBOX_MIN_H_CM = 2.0

_RULE_H_CM = 0.08
_RULE_GAP_CM = 0.1                # stem box -> blue rule
_OPTS_TOP_GAP_CM = 0.35           # blue rule -> first option
_OPTS_LEFT_CM = 0.7
_OPTS_MAX_H_CM = 10.0
_OPTS_WIDTH_FRAC = 0.55           # options width when images sit beside them

#: Vertical room the blue rule must leave below itself: ~5.5 cm of options,
#: ~1.5 cm of answer banner, ~0.8 cm of margins. Capping the rule at
#: ``slide_h - 7.8`` is what stops a generous stem-height estimate from pushing
#: the options off the bottom of the slide -- a real overflow bug in the
#: normal-file builder.
_RULE_RESERVE_CM = 7.8
#: Room the rule must leave when the slide has neither options nor images (a
#: stem-only slide may use nearly the whole canvas).
_RULE_MIN_RESERVE_CM = 1.0

_BANNER_H_CM = 1.5
_BANNER_H_AI_CM = 2.1             # taller: the AI marker adds a second line
_BANNER_BOTTOM_GAP_CM = 0.2

_IMG_GAP_CM = 0.2
_IMG_MAX_H_CM = 6.0
_IMG_MIN_H_CM = 0.5

_TABLE_GAP_CM = 0.2
_TABLE_BOTTOM_RESERVE_CM = 4.5    # room a table must leave for rule + options

_TITLE_LOGO_TOP_CM = 1.2
_TITLE_LOGO_H_CM = 5.5
_TITLE_SUBJECT_TOP_CM = 8.0
_TITLE_RULE_TOP_CM = 11.5
_TITLE_INFO_TOP_CM = 12.2
_TITLE_SUBJECT_PT = 38
_TITLE_INFO_PT = 38
_THANKS_RULE_TOP_CM = 11.0
_THANKS_INFO_TOP_CM = 11.8
_THANKS_PT = 44

_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
_BLACK = RGBColor(0x00, 0x00, 0x00)
_GREY = RGBColor(0x7F, 0x7F, 0x7F)          # AI-derived marker
_ANSWER_GREEN = RGBColor(0x00, 0x7B, 0x00)  # in-slide answer banner
_TABLE_HEADER_FILL = RGBColor(0xFF, 0xED, 0xED)
_TABLE_BORDER_HEX = "FF0000"
_TABLE_BORDER_W_EMU = 6350                  # 0.5 pt

#: Characters Windows forbids in a file name. A subject-derived name such as
#: "ENT: Recall?" would otherwise raise OSError at save time.
_ILLEGAL_NAME_RE = re.compile(r'[\\/:*?"<>|]')

#: Failures that must cost one question, not the whole deck.
_SLIDE_ERRORS = (
    ValueError,
    TypeError,
    AttributeError,
    IndexError,
    KeyError,
    ZeroDivisionError,
    OSError,
)


@dataclass(frozen=True, slots=True)
class _Geom:
    """Resolved slide geometry, all in centimetres."""

    slide_w: float
    slide_h: float
    box_left: float
    box_top: float
    box_width: float
    #: slide_h / 19.05 -- scales vertical constants tuned for the reference deck.
    v_scale: float

    @classmethod
    def from_cfg(cls, cfg: PptxExportConfig) -> "_Geom":
        slide_w = max(float(cfg.slide_w_cm), 12.0)
        slide_h = max(float(cfg.slide_h_cm), 9.0)
        return cls(
            slide_w=slide_w,
            slide_h=slide_h,
            box_left=_QBOX_LEFT_CM,
            box_top=_QBOX_TOP_CM,
            box_width=max(slide_w - _QBOX_SIDE_TOTAL_CM, 6.0),
            v_scale=slide_h / _REF_SLIDE_H_CM,
        )

    def v(self, cm: float) -> float:
        return cm * self.v_scale


def _rgb(color: tuple[int, int, int]) -> RGBColor:
    """Tolerant tuple -> RGBColor: config colours arrive from JSON as lists."""
    r, g, b = (max(0, min(255, int(c))) for c in tuple(color)[:3])
    return RGBColor(r, g, b)


# --------------------------------------------------------------------------- #
# Text measurement and auto-shrink  (ported from the box-format builders)
# --------------------------------------------------------------------------- #


def _estimate_text_height_cm(text: str, font_pt: float, box_width_cm: float) -> float:
    """Estimate the rendered height of `text` in a box `box_width_cm` wide.

    Uses the *tighter* coefficients from the box builders (0.045 line height,
    0.15 padding) rather than the normal-file builder's 0.055/0.3: the looser
    numbers left a band of dead space under every short stem, which pushed the
    options needlessly far down the slide.
    """
    pt = max(float(font_pt), 1.0)
    char_w_cm = pt * 0.020
    usable_cm = max(box_width_cm - 0.6, char_w_cm)
    chars_per_line = max(20, int(usable_cm / char_w_cm))
    line_h_cm = pt * 0.045
    total_lines = 0.0
    for raw_line in text.split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            total_lines += 0.5
            continue
        total_lines += max(1, math.ceil(len(raw_line) / chars_per_line))
    return total_lines * line_h_cm + 0.15


#: Points to centimetres. Paragraph spacing is set in points, the layout is
#: reasoned about in centimetres, and mixing the two silently is how a "centred"
#: block ends up sitting visibly high.
_PT_TO_CM = 2.54 / 72.0


def _options_height_cm(
    options: Sequence[Option], font_pt: float, width_cm: float
) -> float:
    """Rendered height of the whole options block, paragraph spacing included.

    `_estimate_text_height_cm` measures only the text. `_add_options_box` also
    puts 2 pt before and 6 pt after every option and a 0.1 cm top margin on the
    frame -- with four options that is nearly a centimetre, which is the
    difference between "evenly spaced" and "nearly evenly spaced".
    """
    if not options:
        return 0.0
    text = "\n".join(f"{o.label}. {o.text}" for o in options)
    spacing_cm = len(options) * (2 + 6) * _PT_TO_CM
    return _estimate_text_height_cm(text, font_pt, width_cm) + spacing_cm + 0.1


def _compute_font_sizes(
    question_text: str,
    options: Sequence[Option],
    has_image: bool,
    n_tables: int,
    cfg: PptxExportConfig,
    geom: _Geom,
    *,
    reserve_cm: float = 0.0,
) -> tuple[int, int]:
    """Shrink stem/option fonts in 2 pt steps until the content fits.

    At most 20 iterations and never below ``cfg.min_font_pt`` -- past that the
    text is unreadable anyway, so overflow is the lesser evil.
    """
    min_pt = max(int(cfg.min_font_pt), 6)
    q_pt = max(int(cfg.question_size_pt), min_pt)
    o_pt = max(int(cfg.options_size_pt), min_pt)

    available_cm = geom.slide_h - 1.4 - (n_tables * 5.0) - reserve_cm
    opts_width = (
        geom.box_width * _OPTS_WIDTH_FRAC if has_image else geom.box_width - 1.0
    )
    opts_text = "\n".join(f"{o.label}. {o.text}" for o in options)

    for _ in range(20):
        q_h = _estimate_text_height_cm(question_text, q_pt, geom.box_width)
        o_h = _estimate_text_height_cm(opts_text, o_pt, opts_width)
        if q_h + o_h + 1.2 <= available_cm:
            break
        if q_pt > min_pt:
            q_pt -= 2
        if o_pt > min_pt:
            o_pt -= 2
        if q_pt <= min_pt and o_pt <= min_pt:
            break
    return max(q_pt, min_pt), max(o_pt, min_pt)


# --------------------------------------------------------------------------- #
# Low-level slide helpers
# --------------------------------------------------------------------------- #


def _add_blank_slide(prs: Presentation, layout: SlideLayout) -> Slide:
    slide = prs.slides.add_slide(layout)
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _WHITE
    return slide


def _set_no_border(shape: Shape) -> None:
    """Explicitly give a shape no outline.

    ``a:ln`` carrying ``a:noFill`` rather than simply omitting the element: a
    textbox with no ``a:ln`` at all inherits the theme's line style, which is how
    a stray outline reappears in PowerPoint. Saying "none" out loud is what makes
    it stick.
    """
    sp_pr = shape._element.spPr
    existing = sp_pr.find(qn("a:ln"))
    if existing is not None:
        sp_pr.remove(existing)
    ln = etree.SubElement(sp_pr, qn("a:ln"))
    etree.SubElement(ln, qn("a:noFill"))


def _set_cell_borders(tc, hex_color: str = _TABLE_BORDER_HEX) -> None:
    """Give one table cell four thin borders.

    The borders are *inserted at the front* of ``a:tcPr``, not appended: the
    CT_TableCellProperties sequence is lnL, lnR, lnT, lnB, ..., then the fill,
    and `cell.fill.solid()` has already written ``a:solidFill``. The original
    scripts appended, producing out-of-order XML that PowerPoint can reject with
    a repair prompt -- one of the few places this port deliberately differs.
    """
    tcPr = tc.find(qn("a:tcPr"))
    if tcPr is None:
        tcPr = etree.SubElement(tc, qn("a:tcPr"))
    for position, border in enumerate(("lnL", "lnR", "lnT", "lnB")):
        ln = tcPr.makeelement(qn(f"a:{border}"), {"w": str(_TABLE_BORDER_W_EMU)})
        solid_fill = etree.SubElement(ln, qn("a:solidFill"))
        clr = etree.SubElement(solid_fill, qn("a:srgbClr"))
        clr.set("val", hex_color)
        tcPr.insert(position, ln)


def _add_rule(slide: Slide, y_cm: float, cfg: PptxExportConfig, geom: _Geom) -> Shape:
    """The brand's blue separator rule."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Cm(0.5),
        Cm(y_cm),
        Cm(geom.slide_w - 1.0),
        Cm(_RULE_H_CM),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(cfg.rule_color)
    shape.line.fill.background()
    return shape


def _centred_text(
    slide: Slide,
    text: str,
    *,
    top_cm: float,
    height_cm: float,
    font: str,
    size_pt: float,
    color: RGBColor,
    geom: _Geom,
    bold: bool = False,
) -> Shape | None:
    """One centred line of title-slide furniture. Empty text adds no shape."""
    if not text:
        return None
    box = slide.shapes.add_textbox(
        Cm(1.0), Cm(top_cm), Cm(geom.slide_w - 2.0), Cm(height_cm)
    )
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = text
    run.font.name = font
    run.font.size = Pt(size_pt)
    run.font.color.rgb = color
    run.font.bold = bold
    return box


def _add_logo(slide: Slide, logo_path: str, geom: _Geom) -> None:
    """Centre the brand logo. A missing or unreadable file is not an error."""
    if not logo_path:
        return
    path = Path(logo_path).expanduser()
    if not path.is_file():
        # A warning, not info: the title slide is *specified* as carrying the
        # logo, so a deck built without it is not what was asked for. This going
        # unsaid is how a stale path cost every deck its logo unnoticed.
        logger.warning("Logo not found, building the slide without it: %s", path)
        return
    try:
        with Image.open(path) as img:
            aspect = (img.width / img.height) if img.height else 1.0
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable logo %s (%s); slide built without it", path, exc)
        return

    logo_h = geom.v(_TITLE_LOGO_H_CM)
    logo_w = logo_h * aspect
    max_w = geom.slide_w - 2.0
    if logo_w > max_w:  # a very wide logo must not run off the sides
        logo_w = max_w
        logo_h = logo_w / aspect if aspect else logo_h
    try:
        slide.shapes.add_picture(
            str(path),
            Cm((geom.slide_w - logo_w) / 2),
            Cm(geom.v(_TITLE_LOGO_TOP_CM)),
            Cm(logo_w),
            Cm(logo_h),
        )
    except (OSError, ValueError) as exc:
        logger.warning("Could not place logo %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Nested tables rendered as real PPTX tables
# --------------------------------------------------------------------------- #


def _table_rows(asset: TableAsset) -> list[list[str]]:
    """Normalise a `TableAsset` into a rectangular list-of-rows.

    Ragged rows are padded: ``table.cell(r, c)`` would raise on a short row, and
    losing a match-the-columns grid is far worse than a couple of empty cells.
    """
    n_cols = asset.n_cols
    if n_cols == 0:
        return []
    rows: list[list[str]] = []
    for row in asset.rows:
        cells = list(row)
        if not any(c.strip() for c in cells):
            continue
        cells.extend([""] * (n_cols - len(cells)))
        rows.append(cells[:n_cols])
    return rows


def _add_table_to_slide(
    slide: Slide,
    table_data: Sequence[Sequence[str]],
    *,
    x_cm: float,
    y_cm: float,
    width_cm: float,
    font_pt: float,
    cfg: PptxExportConfig,
    geom: _Geom,
) -> float:
    """Draw `table_data` as a real PPTX table; return the y below it.

    Red text on white with a light-red header row, banding disabled and explicit
    per-cell borders (``a:lnL/lnR/lnT/lnB``) -- PowerPoint's default table style
    would otherwise repaint the whole grid blue when the deck is opened.
    """
    rows = len(table_data)
    cols = max((len(r) for r in table_data), default=0)
    if rows == 0 or cols == 0:
        return y_cm

    # A very tall stem estimate can push the anchor past the slide; keep the
    # frame on the canvas rather than emitting an off-slide shape.
    y_cm = min(max(y_cm, 0.2), geom.slide_h - 1.2)

    tbl_font_pt = max(min(font_pt - 8, 16), 11)
    char_w_cm = tbl_font_pt * 0.018
    base_row_h = tbl_font_pt * 0.055

    # Match-the-columns grids have a short "List I" and a long "List II"; giving
    # the right column most of the width nearly halves the wrapped height.
    col_fracs = [0.30, 0.70] if cols == 2 else [1.0 / cols] * cols
    col_widths_cm = [width_cm * f for f in col_fracs]
    chars_per_col = [max(8, int(w / char_w_cm)) for w in col_widths_cm]

    row_heights: list[float] = []
    for row_data in table_data:
        max_lines = 1
        for c_idx, cell_text in enumerate(row_data[:cols]):
            need = max(1, math.ceil(len(cell_text) / chars_per_col[c_idx]))
            max_lines = max(max_lines, need)
        row_heights.append(base_row_h * max_lines)

    tbl_h_cm = sum(row_heights)
    max_h = geom.slide_h - y_cm - _TABLE_BOTTOM_RESERVE_CM
    if max_h < 1.0:
        # Almost no room left: shrink to whatever still fits on the slide rather
        # than dropping the grid or drawing past the bottom edge.
        max_h = max(geom.slide_h - y_cm - 0.2, 0.6)
    if tbl_h_cm > max_h:
        scale = max_h / tbl_h_cm
        row_heights = [h * scale for h in row_heights]
        tbl_h_cm = max_h

    table = slide.shapes.add_table(
        rows, cols, Cm(x_cm), Cm(y_cm), Cm(width_cm), Cm(tbl_h_cm)
    ).table

    tbl_el = table._tbl
    tblPr = tbl_el.find(qn("a:tblPr"))
    if tblPr is None:
        tblPr = etree.SubElement(tbl_el, qn("a:tblPr"))
    tblPr.set("bandRow", "0")
    tblPr.set("bandCol", "0")
    tblPr.set("firstRow", "0")
    tblPr.set("lastRow", "0")

    for r_idx, row_data in enumerate(table_data):
        for c_idx, cell_text in enumerate(row_data[:cols]):
            cell = table.cell(r_idx, c_idx)
            cell.text = ""
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT
            run = p.add_run()
            run.text = cell_text
            run.font.name = cfg.question_font
            run.font.size = Pt(tbl_font_pt)
            run.font.color.rgb = _rgb(cfg.question_color)
            run.font.bold = r_idx == 0
            cell.fill.solid()
            cell.fill.fore_color.rgb = _TABLE_HEADER_FILL if r_idx == 0 else _WHITE
            cell.margin_left = Cm(0.15)
            cell.margin_right = Cm(0.15)
            cell.margin_top = Cm(0.05)
            cell.margin_bottom = Cm(0.05)

    for c_idx in range(cols):
        table.columns[c_idx].width = Cm(col_widths_cm[c_idx])
    for r_idx, height in enumerate(row_heights):
        table.rows[r_idx].height = Cm(height)

    for r_idx in range(rows):
        for c_idx in range(cols):
            _set_cell_borders(table.cell(r_idx, c_idx)._tc)

    return y_cm + tbl_h_cm + 0.3


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


def _gather_images(q: Question, cfg: PptxExportConfig) -> list[ImageAsset]:
    """Question images plus any attached to an option, deduplicated by hash.

    Word routinely embeds the same picture twice, and an option-level image
    still belongs on the slide even though it cannot be placed inline. Images
    below the options (answer / explanation area) are left out when the deck
    is set not to include them.
    """
    skip = set() if cfg.include_images_after_options else {
        id(img) for img in q.images_after_options()
    }
    seen: set[str] = set()
    out: list[ImageAsset] = []
    for img in [*q.images, *(i for o in q.options for i in o.images)]:
        if id(img) in skip or not img.data or img.sha1 in seen:
            continue
        seen.add(img.sha1)
        out.append(img)
    return out


def _place_images(
    slide: Slide,
    images: Sequence[ImageAsset],
    *,
    x_cm: float,
    y_cm: float,
    max_w_cm: float,
    geom: _Geom,
    q_number: int,
) -> None:
    """Stack images downwards, aspect preserved, never past the slide edge."""
    if not images or max_w_cm <= _IMG_MIN_H_CM:
        logger.warning("Q%s: no horizontal room for %d image(s)", q_number, len(images))
        return
    bottom_limit = geom.slide_h - 0.2
    n_imgs = len(images)
    span = bottom_limit - y_cm - _IMG_GAP_CM * (n_imgs - 1)
    available_per_img = max(span / n_imgs, 1.5)

    for placed, img in enumerate(images):
        if y_cm + _IMG_MIN_H_CM > bottom_limit:
            logger.warning(
                "Q%s: no vertical room left for %d image(s)",
                q_number,
                n_imgs - placed,
            )
            return
        try:
            aspect = img.aspect or 1.0
            disp_h = min(available_per_img, _IMG_MAX_H_CM)
            disp_w = disp_h * aspect
            if disp_w > max_w_cm:
                disp_w = max_w_cm
                disp_h = disp_w / aspect
            if disp_h > available_per_img:
                disp_h = available_per_img
                disp_w = disp_h * aspect
            if y_cm + disp_h > bottom_limit:
                disp_h = max(bottom_limit - y_cm, _IMG_MIN_H_CM)
                disp_w = min(disp_h * aspect, max_w_cm)
            slide.shapes.add_picture(
                io.BytesIO(img.data), Cm(x_cm), Cm(y_cm), Cm(disp_w), Cm(disp_h)
            )
            y_cm += disp_h + _IMG_GAP_CM
        except (OSError, ValueError, ZeroDivisionError) as exc:
            # One corrupt picture must not cost the rest of the slide.
            logger.warning("Q%s: skipped an image (%s)", q_number, exc)


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #


def _answer_display(q: Question) -> tuple[str, bool]:
    """Return (text to show, is_ai_derived).

    The body is copied from the answer or from the matching option -- never
    composed -- so the source's wording survives into the deck.
    """
    ans = q.answer
    label = (ans.label or "").strip().upper()
    if not label or ans.origin is AnswerOrigin.NONE:
        return ANSWER_UNKNOWN_TEXT, False
    body = ans.text.strip()
    if not body:
        opt = q.option(label)
        body = opt.text.strip() if opt is not None else ""
    return (f"{label}) {body}" if body else label), ans.is_ai_derived


def _fill_answer_frame(
    tf: TextFrame,
    ans_text: str,
    *,
    ai_derived: bool,
    cfg: PptxExportConfig,
    size_pt: float,
    color: RGBColor,
) -> None:
    """Write ``Answer:  <text>`` and, when needed, the AI-derived marker."""
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    prefix = p.add_run()
    prefix.text = "Answer:  "
    prefix.font.name = cfg.options_font
    prefix.font.size = Pt(size_pt)
    prefix.font.bold = True
    prefix.font.color.rgb = color

    body = p.add_run()
    body.text = ans_text
    body.font.name = cfg.options_font
    body.font.size = Pt(size_pt)
    body.font.color.rgb = color

    if not ai_derived:
        return
    marker_p = tf.add_paragraph()
    marker_p.alignment = PP_ALIGN.LEFT
    marker = marker_p.add_run()
    marker.text = AI_ANSWER_MARKER
    marker.font.name = cfg.options_font
    marker.font.size = Pt(max(size_pt - 8, 10))
    marker.font.italic = True
    marker.font.color.rgb = _GREY


def _stem_heading(number: int, stem: str, cfg: PptxExportConfig) -> str:
    """The first line of a question slide, per the pre-flight tick boxes.

    `stem` arrives already emptied when the question text is switched off, so
    this only has to decide the label. Nothing here alters the stem's wording --
    the prefix is added, never substituted.
    """
    style = (cfg.question_prefix or "number").strip().lower()
    if style == "none":
        label = ""
    elif style == "q":
        label = "Q."
    else:
        label = f"Q{number}."
    if not stem:
        return label
    return f"{label} {stem}" if label else stem


def _add_answer_slide(
    prs: Presentation,
    layout: SlideLayout,
    q: Question,
    cfg: PptxExportConfig,
    geom: _Geom,
) -> None:
    """A plain white slide carrying only the answer (the WITH_ANSWERS look)."""
    slide = _add_blank_slide(prs, layout)
    ans_text, ai_derived = _answer_display(q)

    box = slide.shapes.add_textbox(
        Cm(0.7), Cm(0.7), Cm(geom.slide_w - 1.4), Cm(min(3.0, geom.slide_h - 1.0))
    )
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Cm(0.0)
    tf.margin_right = Cm(0.0)
    tf.margin_top = Cm(0.0)
    tf.margin_bottom = Cm(0.0)
    _fill_answer_frame(
        tf,
        ans_text,
        ai_derived=ai_derived,
        cfg=cfg,
        size_pt=cfg.options_size_pt,
        color=_BLACK,
    )


def _add_answer_banner(
    slide: Slide,
    ans_text: str,
    ai_derived: bool,
    *,
    height_cm: float,
    cfg: PptxExportConfig,
    geom: _Geom,
) -> None:
    """Answer banner along the bottom of the question slide, in dark green."""
    top = max(geom.slide_h - height_cm - _BANNER_BOTTOM_GAP_CM, 0.2)
    height = max(min(height_cm, geom.slide_h - top - 0.1), 0.4)

    box = slide.shapes.add_textbox(
        Cm(_OPTS_LEFT_CM), Cm(top), Cm(geom.slide_w - 2 * _OPTS_LEFT_CM), Cm(height)
    )
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Cm(0.15)
    tf.margin_right = Cm(0.15)
    tf.margin_top = Cm(0.05)
    tf.margin_bottom = Cm(0.05)
    _fill_answer_frame(
        tf,
        ans_text,
        ai_derived=ai_derived,
        cfg=cfg,
        size_pt=cfg.options_size_pt,
        color=_ANSWER_GREEN,
    )


# --------------------------------------------------------------------------- #
# Question slide
# --------------------------------------------------------------------------- #


def _add_stem_box(
    slide: Slide,
    text: str,
    *,
    height_cm: float,
    font_pt: int,
    cfg: PptxExportConfig,
    geom: _Geom,
) -> Shape:
    """The bordered red stem box: one paragraph per source line, verbatim."""
    box = slide.shapes.add_textbox(
        Cm(geom.box_left), Cm(geom.box_top), Cm(geom.box_width), Cm(height_cm)
    )
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Cm(_QBOX_MARGIN_LR_CM)
    tf.margin_right = Cm(_QBOX_MARGIN_LR_CM)
    tf.margin_top = Cm(_QBOX_MARGIN_TB_CM)
    tf.margin_bottom = Cm(_QBOX_MARGIN_TB_CM)
    tf.auto_size = None

    first = True
    for line in text.split("\n"):
        if not line.strip():
            continue
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = PP_ALIGN.LEFT
        p.space_after = Pt(2)
        run = p.add_run()
        # No strip(): the stem is a verbatim source slice and stays that way.
        run.text = line
        run.font.name = cfg.question_font
        # A pipe-delimited line is a flattened table row: drop 4 pt so its
        # columns still fit on one line.
        run.font.size = Pt(
            font_pt if " | " not in line else max(font_pt - 4, cfg.min_font_pt)
        )
        run.font.color.rgb = _rgb(cfg.question_color)

    # No outline: the red text and the blue rule below already separate the stem
    # from the options, and a black box around it reads as a stray shape.
    _set_no_border(box)
    return box


def _add_options_box(
    slide: Slide,
    options: Sequence[Option],
    *,
    top_cm: float,
    width_cm: float,
    height_cm: float,
    font_pt: int,
    cfg: PptxExportConfig,
) -> Shape:
    """Options in black, one paragraph each, with a bold label run."""
    box = slide.shapes.add_textbox(
        Cm(_OPTS_LEFT_CM), Cm(top_cm), Cm(width_cm), Cm(height_cm)
    )
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Cm(0.15)
    tf.margin_right = Cm(0.15)
    tf.margin_top = Cm(0.1)
    tf.auto_size = None

    for idx, opt in enumerate(options):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_after = Pt(6)
        p.space_before = Pt(2)

        label_run = p.add_run()
        label_run.text = f"{opt.label}. "
        label_run.font.name = cfg.options_font
        label_run.font.size = Pt(font_pt)
        label_run.font.color.rgb = _rgb(cfg.options_color)
        label_run.font.bold = True

        body_run = p.add_run()
        body_run.text = opt.text          # verbatim, label prefix already stripped
        body_run.font.name = cfg.options_font
        body_run.font.size = Pt(font_pt)
        body_run.font.color.rgb = _rgb(cfg.options_color)
    return box


def _build_question_slide(
    prs: Presentation,
    layout: SlideLayout,
    q: Question,
    number: int,
    cfg: PptxExportConfig,
    geom: _Geom,
) -> None:
    """One question slide: stem box, tables, rule, options, images, banner."""
    slide = _add_blank_slide(prs, layout)

    options = [o for o in q.options if o.label or o.text] if cfg.include_options else []
    images = _gather_images(q, cfg)
    # Only grids sitting between the stem and the first option belong on a
    # question slide; anything anchored further down is explanation material.
    tables = [
        rows
        for rows in (_table_rows(t) for t in q.tables_between_stem_and_options())
        if rows
    ]
    banner = cfg.include_answers and not cfg.answer_on_separate_slide
    ans_text, ans_ai = _answer_display(q)
    banner_h = geom.v(_BANNER_H_AI_CM if ans_ai else _BANNER_H_CM)

    stem = q.stem.strip("\n") if cfg.include_question_text else ""
    full_q = _stem_heading(number, stem, cfg)

    q_pt, o_pt = _compute_font_sizes(
        full_q,
        options,
        bool(images),
        len(tables),
        cfg,
        geom,
        reserve_cm=banner_h if banner else 0.0,
    )

    # Both the label and the question text can be switched off in the pre-flight
    # panel. Drawing an empty bordered box then would look like a mistake, so the
    # slide simply starts with its content instead.
    if full_q.strip():
        est_h = max(
            _estimate_text_height_cm(full_q, q_pt, geom.box_width), _QBOX_MIN_H_CM
        )
        stem_box = _add_stem_box(
            slide, full_q, height_cm=est_h, font_pt=q_pt, cfg=cfg, geom=geom
        )
        cur_y = geom.box_top + est_h + _RULE_GAP_CM
    else:
        est_h = 0.0
        stem_box = None
        cur_y = geom.box_top

    for rows in tables:
        cur_y = _add_table_to_slide(
            slide,
            rows,
            x_cm=_OPTS_LEFT_CM,
            y_cm=cur_y,
            width_cm=geom.box_width - 0.5,
            font_pt=q_pt,
            cfg=cfg,
            geom=geom,
        )
        cur_y += _TABLE_GAP_CM

    # Cap the rule so the options always have room below it. Skipped when a
    # table was drawn: there the cap would drag the rule up *into* the table.
    reserve = _RULE_RESERVE_CM if (options or images) else _RULE_MIN_RESERVE_CM
    floor_y = geom.slide_h - geom.v(_RULE_MIN_RESERVE_CM)
    rule_y = cur_y + _RULE_GAP_CM
    if not tables:
        rule_y = min(rule_y, geom.slide_h - geom.v(reserve))
    if stem_box is not None:
        rule_y = min(max(rule_y, geom.box_top + 1.0), floor_y)
        _add_rule(slide, rule_y, cfg, geom)
        # The height estimate is generous by design; clamp the frame so the
        # bordered box cannot swallow the rule (or run off the slide) when it
        # over-estimates.
        stem_box.height = Cm(max(min(est_h, rule_y - geom.box_top - 0.05), 0.8))
    else:
        # No stem box means no rule to separate it from: content starts at the top.
        rule_y = max(cur_y - _OPTS_TOP_GAP_CM, geom.box_top - _OPTS_TOP_GAP_CM)

    opts_top = min(rule_y + _OPTS_TOP_GAP_CM, geom.slide_h - 0.6)
    opts_width = geom.box_width * _OPTS_WIDTH_FRAC if images else geom.box_width
    opts_bottom = (
        geom.slide_h - banner_h - _BANNER_BOTTOM_GAP_CM - 0.2
        if banner
        else geom.slide_h - 0.5
    )

    # Even spacing. Everything above is laid out top-down, so whatever vertical
    # room the question and its options do not use piles up as one dead band at
    # the foot of the slide -- measured across a real deck it ran from 3.0 cm to
    # 7.0 cm, varying question by question, which is what reads as "unevenly
    # spaced". Split that leftover in half instead: the gap under the rule then
    # equals the margin under the last option, whatever length the question is.
    #
    # Not when a table or an image is on the slide. Those are placed from
    # `opts_top` downwards and already occupy the space this would be giving
    # away, so moving the options would only squeeze them.
    if options and not tables and not images:
        needed = _options_height_cm(options, o_pt, opts_width)
        slack = opts_bottom - opts_top - needed
        if slack > 0:
            opts_top = min(opts_top + slack / 2.0, max(opts_bottom - needed, opts_top))

    opts_h = min(max(opts_bottom - opts_top, 2.0), _OPTS_MAX_H_CM)
    opts_h = max(min(opts_h, geom.slide_h - opts_top - 0.2), 0.4)

    if options:
        _add_options_box(
            slide,
            options,
            top_cm=opts_top,
            width_cm=opts_width,
            height_cm=opts_h,
            font_pt=o_pt,
            cfg=cfg,
        )

    if images:
        # Beside the options (which were narrowed to 55% for exactly this) when
        # both are present; otherwise the images own the full width.
        img_x = opts_width + 1.2 if options else 1.0
        _place_images(
            slide,
            images,
            x_cm=img_x,
            y_cm=opts_top,
            max_w_cm=geom.slide_w - img_x - 0.5,
            geom=geom,
            q_number=number,
        )

    if banner:
        _add_answer_banner(
            slide, ans_text, ans_ai, height_cm=banner_h, cfg=cfg, geom=geom
        )

    logger.debug(
        "Q%s slide: fonts %spt/%spt, %d options, %d images, %d tables",
        number,
        q_pt,
        o_pt,
        len(options),
        len(images),
        len(tables),
    )


# --------------------------------------------------------------------------- #
# Title / thank-you slides
# --------------------------------------------------------------------------- #


def _add_title_slide(
    prs: Presentation,
    layout: SlideLayout,
    cfg: PptxExportConfig,
    brand: BrandConfig,
    geom: _Geom,
) -> None:
    slide = _add_blank_slide(prs, layout)
    _add_logo(slide, brand.logo_path or "", geom)

    _centred_text(
        slide,
        brand.subject or "",
        top_cm=geom.v(_TITLE_SUBJECT_TOP_CM),
        height_cm=geom.v(3.0),
        font=cfg.question_font,
        size_pt=_TITLE_SUBJECT_PT,
        color=_rgb(cfg.question_color),
        geom=geom,
        bold=True,
    )
    _add_rule(slide, geom.v(_TITLE_RULE_TOP_CM), cfg, geom)

    info = brand.faculty or ""
    if brand.date_str:
        info = f"{info}  —  {brand.date_str}" if info else brand.date_str
    _centred_text(
        slide,
        info,
        top_cm=geom.v(_TITLE_INFO_TOP_CM),
        height_cm=geom.v(2.0),
        font=cfg.question_font,
        size_pt=_TITLE_INFO_PT,
        color=_BLACK,
        geom=geom,
    )


def _add_thankyou_slide(
    prs: Presentation,
    layout: SlideLayout,
    cfg: PptxExportConfig,
    brand: BrandConfig,
    geom: _Geom,
) -> None:
    slide = _add_blank_slide(prs, layout)
    _add_logo(slide, brand.logo_path or "", geom)

    _centred_text(
        slide,
        "Thank you",
        top_cm=geom.v(_TITLE_SUBJECT_TOP_CM),
        height_cm=geom.v(2.5),
        font=cfg.question_font,
        size_pt=_THANKS_PT,
        color=_rgb(cfg.question_color),
        geom=geom,
        bold=True,
    )
    _add_rule(slide, geom.v(_THANKS_RULE_TOP_CM), cfg, geom)
    _centred_text(
        slide,
        "Best wishes!",
        top_cm=geom.v(_THANKS_INFO_TOP_CM),
        height_cm=geom.v(2.0),
        font=cfg.question_font,
        size_pt=_TITLE_INFO_PT,
        color=_BLACK,
        geom=geom,
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def _prepare_out_path(out_path: str | Path) -> Path:
    """Resolve `out_path`, sanitise its file name and create its folder."""
    dest = Path(out_path).expanduser()
    name = _ILLEGAL_NAME_RE.sub("-", dest.name).strip() or "deck.pptx"
    if not name.lower().endswith(".pptx"):
        name = f"{name}.pptx"
    dest = dest.with_name(name) if dest.name else dest / name

    parent = dest.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot create the output folder {parent} - permission denied. "
            "Pick a different output folder in Settings."
        ) from exc
    return dest


def build_deck(
    questions: Sequence[Question],
    out_path: str | Path,
    cfg: PptxExportConfig,
    brand: BrandConfig,
) -> str:
    """Build the branded deck and return the path actually written.

    Layout: optional title slide, then per question one slide (bordered red stem
    box, nested tables, blue rule, black options, images beside the options) plus
    either an answer slide or an in-slide answer banner, then the thank-you
    slide. A question that fails to render is logged and skipped -- one bad
    question never costs the whole deck.
    """
    geom = _Geom.from_cfg(cfg)
    dest = _prepare_out_path(out_path)

    prs = _new_presentation()
    prs.slide_width = Cm(geom.slide_w)
    prs.slide_height = Cm(geom.slide_h)
    layout = prs.slide_layouts[6]  # blank layout of the default template

    if cfg.title_slide:
        _add_title_slide(prs, layout, cfg, brand, geom)

    separate_answers = cfg.include_answers and cfg.answer_on_separate_slide
    failures = 0
    for pos, q in enumerate(questions, start=1):
        number = q.number if q.number > 0 else pos
        try:
            _build_question_slide(prs, layout, q, number, cfg, geom)
            if separate_answers:
                _add_answer_slide(prs, layout, q, cfg, geom)
        except _SLIDE_ERRORS as exc:
            failures += 1
            logger.warning("Q%s could not be rendered and was skipped: %s", number, exc)

    if cfg.thankyou_slide:
        _add_thankyou_slide(prs, layout, cfg, brand, geom)

    try:
        prs.save(str(dest))
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot write {dest} - it is probably open in PowerPoint. "
            "Close the file (or choose another name) and export again."
        ) from exc

    logger.info(
        "Deck written: %s (%d slides, %d questions, %d skipped)",
        dest,
        len(prs.slides),
        len(questions),
        failures,
    )
    return str(dest)
