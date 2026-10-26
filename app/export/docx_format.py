"""Word (.docx) XML surgery: house style, keyword spacing, option labels, colour.

Ported from ``FORMATTING_BOX_FILE.py``, which is battle-tested against real user
documents. The behaviour is unchanged; the only difference is that the hard-coded
module constants became :class:`~app.models.WordExportConfig` fields so the UI can
drive them.

Why the XML level at all? python-docx's convenience API cannot express most of
what is needed here (paragraph-mark fonts, ``docDefaults``, cell shading, nested
table borders) and Word *silently discards* any element inserted at a
schema-invalid position -- hence :func:`get_or_add_ordered` and the ``*_ORDER``
sequences. That ordering is the part everybody gets wrong; do not "simplify" it.

The rules implemented here never change a character of document content. They
add and remove *empty* paragraphs, split a paragraph at a boundary that already
existed, and set run properties (bold, colour, font) -- none of which touches a
character. The one deliberate exception is the bold ``"Option "`` prefix of rule
6, which ``app.export.verify`` knows how to subtract again when it proves the
output is verbatim.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Iterator, Sequence

from docx.document import Document as DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.table import _Cell
from lxml.etree import _Element

from app.models import WordExportConfig

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Header-row font colour of a coloured (nested) table. Not configurable: it only
#: ever sits on ``WordExportConfig.header_fill``, and white is the only colour
#: that stays legible on the whole plausible range of header fills.
HEADER_TEXT_COLOR = "FFFFFF"
#: Un-banded body rows of a coloured table.
PLAIN_FILL = "FFFFFF"

# Schema child orders -- used to insert a new element at a valid position.
RPR_ORDER: list[str] = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
    "dstrike", "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid",
    "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz",
    "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
    "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
]
TCPR_ORDER: list[str] = [
    "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd",
    "noWrap", "tcMar", "textDirection", "tcFit", "vAlign", "hideMark",
    "cellIns", "cellDel", "cellMerge",
]
TBLPR_ORDER: list[str] = [
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
    "tblStyleColBandSize", "tblW", "tblJc", "tblCellSpacing", "tblInd",
    "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption",
    "tblDescription",
]

# Keyword "bases": matched without the trailing colon so that "Other Options"
# and "Other Options:" are handled identically.
KW_EXPLANATION = "Explanation"
KW_INCORRECT = "Incorrect Options"
KW_OTHER = "Other Options"
KW_TAKEHOME = "Take Home Message"
KW_OBJECTIVE = "Educational Objective"
#: Headings some banks use for the same two blocks. They are their own bases
#: rather than variants of the two above because "Other Options Explanation"
#: is followed by prose about the wrong answers, not by "A. ..." entries, and
#: must not open an Incorrect/Other-Options block that relabels nothing.
KW_CORRECT_EXPL = "Correct Option Explanation"
KW_OTHER_EXPL = "Other Options Explanation"
KW_ANSWER_EXPL = "Correct Answer Explanation"
#: `match_keyword` takes the LONGEST base that fits, so "Other Options
#: Explanation" is never read as "Other Options" plus a word.
KEYWORD_BASES: list[str] = [
    KW_EXPLANATION, KW_INCORRECT, KW_OTHER, KW_TAKEHOME, KW_OBJECTIVE,
    KW_CORRECT_EXPL, KW_OTHER_EXPL, KW_ANSWER_EXPL,
]

#: Bases that mean "here is the explanation" -- an exporter that finds one of
#: these in a block must not add an "Explanation:" heading of its own.
EXPLANATION_BASES: frozenset[str] = frozenset(
    {KW_EXPLANATION, KW_CORRECT_EXPL, KW_ANSWER_EXPL}
)

#: Keywords that close an Incorrect/Other-Options block.
BLOCK_ENDERS: frozenset[str] = frozenset(
    {KW_TAKEHOME, KW_EXPLANATION, KW_OBJECTIVE, KW_CORRECT_EXPL, KW_OTHER_EXPL,
     KW_ANSWER_EXPL}
)

#: An option line: "A. ...", "B) ..." etc.
#:
#: Restricted to A-F (real MCQ option letters) so that medical abbreviations
#: which happen to begin a line -- "H. pylori", "S. aureus", "M. tuberculosis"
#: -- are NOT mistaken for an option and wrongly given an "Option " label.
OPTION_RE = re.compile(r"^[A-F][.)]\s")

#: An option line that already carries the inserted label (idempotency check and
#: the hook `app.export.verify` uses to find what we added).
LABELLED_OPTION_RE = re.compile(r"^Option [A-F][.)]\s")

#: The exact text of the run we insert in front of an incorrect-option letter.
OPTION_LABEL_TEXT = "Option "

# Tags that mean "this run/paragraph carries a picture, not just text".
DRAWING_TAGS = (qn("w:drawing"), qn("w:object"), qn("w:pict"))


#: Anything exposing ``.paragraphs``: a table cell or a whole document body.
#: The keyword/option rules only ever need that, so a flat (non-box) document can
#: be styled by passing the Document itself.
ParagraphHost = _Cell | DocxDocument


def _half_points(cfg: WordExportConfig) -> str:
    """``w:sz`` / ``w:szCs`` are in half-points -> 14pt becomes "28"."""
    return str(int(round(cfg.font_size_pt * 2)))


# --------------------------------------------------------------------------- #
# Small XML text helpers
# --------------------------------------------------------------------------- #


def run_text(r: _Element) -> str:
    """Text of a ``<w:r>``, mirroring python-docx semantics (br/cr -> ``\\n``,
    tab -> ``\\t``)."""
    parts: list[str] = []
    for child in r:
        tag = child.tag
        if tag == qn("w:t"):
            parts.append(child.text or "")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
        elif tag == qn("w:tab"):
            parts.append("\t")
    return "".join(parts)


def para_text(p: _Element) -> str:
    """Full text of a ``<w:p>`` element."""
    return "".join(t.text or "" for t in p.iter(qn("w:t")))


def run_has_drawing(r: _Element) -> bool:
    return any(r.find(".//" + t) is not None for t in DRAWING_TAGS)


def is_blank_p(el: _Element | None) -> bool:
    """True when `el` is an empty ``<w:p>`` (no text, no image)."""
    if el is None or el.tag != qn("w:p"):
        return False
    for t in DRAWING_TAGS:
        if el.find(".//" + t) is not None:
            return False
    return para_text(el).strip() == ""


def new_blank_p() -> _Element:
    return OxmlElement("w:p")


# --------------------------------------------------------------------------- #
# Keyword matching
# --------------------------------------------------------------------------- #


#: Characters that may separate the words of a heading. A bank writes
#: "Take Home Message", "Take-Home Message" and "Take Home Message" for the
#: same thing, and treating those as three different headings costs the block its
#: formatting -- and, in the AI path, its place in the explanation entirely.
_SEPARATORS = " \t -‐‑‒–—―−"


def _match_base(full: str, base: str) -> int:
    """Length of the prefix of `full` that spells `base`, or -1 for no match.

    Case-insensitive, and any run of `_SEPARATORS` in the source matches the
    single space in the canonical base. Returns a length measured in `full`, not
    in `base`, so a source that separates words differently still yields a label
    boundary that lands on a real character.
    """
    i = j = 0
    while j < len(base):
        if base[j] in _SEPARATORS:
            if i >= len(full) or full[i] not in _SEPARATORS:
                return -1
            while i < len(full) and full[i] in _SEPARATORS:
                i += 1
            while j < len(base) and base[j] in _SEPARATORS:
                j += 1
            continue
        if i >= len(full) or full[i].lower() != base[j].lower():
            return -1
        i += 1
        j += 1
    return i


def match_keyword(text: str) -> str | None:
    """Return the matched keyword base (see :data:`KEYWORD_BASES`) or None.

    A match requires the base to sit at the start of the (stripped) text and to
    be followed by ':', whitespace, or the end of the string -- so a stray word
    that merely *starts* with those letters is not matched.

    **Case-insensitive.** Real question banks write the same heading as
    "Explanation:", "EXPLANATION:" and "Other options:" from one file to the
    next, and treating those as different headings is what made the exporter add
    a second "Explanation:" above the source's own. The *canonical* spelling is
    returned so callers can compare bases, but no caller may write it back into
    the document -- the source's own casing is content and stays untouched.
    """
    t = text.strip()
    best: str | None = None
    for base in KEYWORD_BASES:
        end = _match_base(t, base)
        if end < 0:
            continue
        rest = t[end:]
        if rest == "" or rest[0] == ":" or rest[0].isspace():
            if best is None or len(base) > len(best):
                best = base
    return best


def label_length(full: str, base: str) -> int:
    """Number of leading chars of `full` belonging to the keyword label: the base
    as the source spelled it, plus its colon(s), plus any trailing spaces/tabs.

    Colons plural: a real file writes "Take-Home Message::", and taking only the
    first would leave a stray ":" opening the content line.
    """
    lead = len(full) - len(full.lstrip())
    end = _match_base(full[lead:], base)
    if end < 0:
        return len(base)
    end += lead
    while end < len(full) and full[end] == ":":
        end += 1
    while end < len(full) and full[end] in " \t":
        end += 1
    return end


# --------------------------------------------------------------------------- #
# Structural edits
# --------------------------------------------------------------------------- #


def strip_trailing_break_runs(pel: _Element) -> None:
    """Remove trailing *line-break* runs (``<w:br/>`` / ``<w:cr/>`` with no text)
    from a stand-alone keyword paragraph, e.g. "Explanation:" followed by a hard
    break.

    Such a trailing break would otherwise render as a spurious blank line on top
    of the proper blank paragraph we add. We remove ONLY runs that carry no
    actual text (no ``<w:t>``) so that no character of the content is ever
    changed -- trailing spaces and every other character are left exactly as they
    were.
    """
    runs = [c for c in pel if c.tag == qn("w:r")]
    for r in reversed(runs):
        if run_has_drawing(r):
            break
        has_text = any((t.text or "") != "" for t in r.findall(qn("w:t")))
        is_break_only = (
            r.find(qn("w:br")) is not None or r.find(qn("w:cr")) is not None
        )
        if not has_text and is_break_only:
            pel.remove(r)
        else:
            break


def split_simple_run(r: _Element, cut: int) -> _Element | None:
    """Split a *simple* run (exactly one ``<w:t>``, no br/tab/drawing) at char
    `cut`.

    Keeps ``text[:cut]`` in `r` and returns a new run holding ``text[cut:]`` with
    the same formatting. Returns None when the run is not simple or the cut is
    out of range -- callers then fall back to moving the whole run.
    """
    ts = [c for c in r if c.tag == qn("w:t")]
    complex_children = [
        c
        for c in r
        if c.tag in (qn("w:br"), qn("w:cr"), qn("w:tab")) or c.tag in DRAWING_TAGS
    ]
    if len(ts) != 1 or complex_children:
        return None
    txt = ts[0].text or ""
    if cut <= 0 or cut >= len(txt):
        return None
    new_r = copy.deepcopy(r)
    ts[0].text = txt[:cut]
    ts[0].set(qn("xml:space"), "preserve")
    new_t = [c for c in new_r if c.tag == qn("w:t")][0]
    new_t.text = txt[cut:]
    new_t.set(qn("xml:space"), "preserve")
    return new_r


def split_after_label(pel: _Element, label_len: int) -> _Element | None:
    """Move everything after the keyword label into a brand-new paragraph
    inserted right after `pel`.

    The new paragraph copies the original ``pPr`` so it keeps the same paragraph
    formatting. Returns the new paragraph, or None when there is nothing after
    the label.
    """
    runs = [c for c in pel if c.tag == qn("w:r")]
    acc = 0
    content_runs: list[_Element] = []
    for r in runs:
        if acc >= label_len:
            content_runs.append(r)
            continue
        t = run_text(r)
        if acc + len(t) <= label_len:
            acc += len(t)
        else:
            cut = label_len - acc
            new_r = split_simple_run(r, cut)
            content_runs.append(new_r if new_r is not None else r)
            acc = label_len

    if not content_runs:
        return None

    new_p = OxmlElement("w:p")
    pPr = pel.find(qn("w:pPr"))
    if pPr is not None:
        new_p.append(copy.deepcopy(pPr))
    for r in content_runs:
        if r.getparent() is pel:
            pel.remove(r)
        new_p.append(r)
    pel.addnext(new_p)
    return new_p


def set_run_bold(r: _Element) -> None:
    """Make one run bold, leaving every other property of it alone."""
    rPr = r.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        r.insert(0, rPr)
    get_or_add_ordered(rPr, "w:b", RPR_ORDER).set(qn("w:val"), "1")
    get_or_add_ordered(rPr, "w:bCs", RPR_ORDER).set(qn("w:val"), "1")


def bold_prefix(pel: _Element, length: int) -> None:
    """Bold the first `length` characters of a paragraph.

    A run straddling the boundary is split so the bold stops exactly where it
    should ("Educational Objective:" bold, its text not). When the run is too
    complex to split -- a picture, a tab, several ``<w:t>`` children -- the whole
    run is bolded instead: over-bolding a few characters is a cosmetic slip,
    while re-writing the run's contents to force the split would risk the text.
    """
    if length <= 0:
        return
    acc = 0
    for r in [c for c in pel if c.tag == qn("w:r")]:
        body = run_text(r)
        if not body:
            continue
        if acc + len(body) <= length:
            set_run_bold(r)
            acc += len(body)
            if acc >= length:
                return
            continue
        tail = split_simple_run(r, length - acc)
        if tail is not None:
            r.addnext(tail)
        set_run_bold(r)
        return


def drop_blank_before(pel: _Element) -> None:
    """Delete blank paragraphs immediately above `pel`.

    A blank ``<w:p>`` holds no characters, so removing it changes the layout and
    nothing else -- the verbatim check compares text and is unaffected.
    """
    while is_blank_p(pel.getprevious()):
        previous = pel.getprevious()
        previous.getparent().remove(previous)


def drop_blank_after(pel: _Element) -> None:
    """Delete blank paragraphs immediately below `pel`."""
    while is_blank_p(pel.getnext()):
        following = pel.getnext()
        following.getparent().remove(following)


def ensure_blank_before(pel: _Element) -> None:
    """Put one empty paragraph above `pel`, unless one is already there.

    The skip is what makes the rule idempotent: re-formatting an already
    formatted document must not stack a second blank line on the first.
    """
    if not is_blank_p(pel.getprevious()):
        pel.addprevious(new_blank_p())


def ensure_blank_after(pel: _Element) -> None:
    """Put one empty paragraph below `pel`, unless one is already there."""
    if not is_blank_p(pel.getnext()):
        pel.addnext(new_blank_p())


def apply_keyword_rules(cell: ParagraphHost) -> None:
    """House style for the labelled blocks of an explanation.

    For every "Explanation:", "Incorrect Options:", "Other Options:",
    "Take Home Message:" and "Educational Objective:" line in `cell`:

    * a keyword written inline with its text is split onto its own line;
    * the label itself is made **bold**;
    * the blank lines directly above and below it are removed.

    A blank line above and below each one -- rules 3, 4 and 5 of the original
    `format_docs.py`, which is the house style of record. The skip when one is
    already there is what keeps re-formatting a formatted file idempotent.

    Nothing here changes a character of content: the label is split at a boundary
    that already existed, and an empty paragraph holds no characters.
    """
    # Snapshot first: the rules insert and delete paragraphs, so iterating
    # `cell.paragraphs` live would visit what we just produced.
    key_paras = [(p, match_keyword(p.text)) for p in cell.paragraphs]
    for p, base in key_paras:
        if base is None:
            continue
        pel = p._p
        if pel.getparent() is None:      # already removed as a blank neighbour
            continue
        full = p.text
        llen = label_length(full, base)
        if full[llen:].strip() == "":
            # Stand-alone keyword line: a trailing hard break would render as a
            # second blank line on top of the one added below.
            strip_trailing_break_runs(pel)
        else:
            split_after_label(pel, llen)      # -> [keyword] [content]
        bold_prefix(pel, llen)
        ensure_blank_before(pel)
        ensure_blank_after(pel)


def prepend_option_label(pel: _Element) -> None:
    """Insert a bold "Option " run at the very start of the paragraph (rule 6)."""
    first_run = next((c for c in pel if c.tag == qn("w:r")), None)
    if first_run is not None and run_text(first_run).startswith(OPTION_LABEL_TEXT):
        return  # already labelled -- keeps the operation idempotent
    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rPr.append(OxmlElement("w:b"))
    r.append(rPr)
    t = OxmlElement("w:t")
    t.text = OPTION_LABEL_TEXT
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    if first_run is not None:
        first_run.addprevious(r)
    else:
        pel.append(r)


def label_incorrect_options(cell: ParagraphHost) -> None:
    """Bold "Option A." before the entries of an Incorrect/Other Options block,
    and set each entry off with a blank line -- never the answer choices under
    the stem.

    The block is delimited textually: it opens on an "Incorrect Options" /
    "Other Options" line and closes on "Take Home Message", "Explanation",
    "Educational Objective" or "Tags". Anything outside it is left completely
    alone, which is why the options listed under the question stem keep their
    bare "A." labels.

    Both halves of the label end up bold: the inserted "Option " and the source's
    own letter after it.

    A blank line goes above every entry -- rule 6 of the original
    `format_docs.py`. The blank *below* the last one is not added here; it comes
    free from the blank `apply_keyword_rules` puts above the heading that closes
    the block::

        Incorrect Options:
                                    <- from ensure_blank_after the heading
        Option A. ... is holocrine.
                                    <- blank
        Option C. ... is apocrine.
                                    <- blank
        Option D. ... is apocrine.
                                    <- from ensure_blank_before "Take Home..."
        Take Home Message:

    An empty ``<w:p>`` holds no characters, so none of this changes content --
    the verbatim check compares text and is unaffected.
    """
    in_region = False
    for p in cell.paragraphs:
        base = match_keyword(p.text)
        t = p.text.strip()
        if base in (KW_INCORRECT, KW_OTHER):
            in_region = True
            continue
        if base in BLOCK_ENDERS or t.startswith("Tags"):
            in_region = False
            continue
        if not in_region or t == "":
            # A blank line inside the block is not the end of it -- the rule
            # below puts them there, so treating one as a terminator would make
            # the second run over a document see a block of one entry.
            continue
        lead = len(p.text) - len(p.text.lstrip())
        if OPTION_RE.match(t):
            # Bold the letter before inserting anything, so the offset is still
            # the one measured against the source text.
            bold_prefix(p._p, lead + 2)      # the "A." / "B)" itself
            prepend_option_label(p._p)
        elif LABELLED_OPTION_RE.match(t):
            # Already labelled: either by an earlier run over this same file, or
            # because the source itself writes "Option A.". Re-bolding is
            # idempotent, and counting it as an entry is what keeps the blank
            # lines below stable instead of appearing and vanishing run by run.
            bold_prefix(p._p, lead + len(OPTION_LABEL_TEXT) + 2)
        else:
            continue
        ensure_blank_before(p._p)


# --------------------------------------------------------------------------- #
# Ordering helper
# --------------------------------------------------------------------------- #


def get_or_add_ordered(
    parent: _Element, tag: str, order: Sequence[str]
) -> _Element:
    """Return the child `tag` of `parent`, creating it at a schema-valid position
    (using the localname sequence `order`) if it does not yet exist.

    Word rejects -- silently, with no error and no visible effect -- any child
    inserted out of schema order, so every new element goes through here.
    """
    el = parent.find(qn(tag))
    if el is not None:
        return el
    el = OxmlElement(tag)
    local = tag.split(":")[-1]
    idx = order.index(local)
    ref = None
    for child in parent:
        cl = child.tag.split("}")[-1]
        if cl in order and order.index(cl) > idx:
            ref = child
            break
    if ref is not None:
        ref.addprevious(el)
    else:
        parent.append(el)
    return el


# --------------------------------------------------------------------------- #
# Font / spacing
# --------------------------------------------------------------------------- #


def set_font_on_rpr(rPr: _Element, cfg: WordExportConfig) -> None:
    """Force the house font + size on a ``<w:rPr>`` (run or paragraph-mark)."""
    rFonts = get_or_add_ordered(rPr, "w:rFonts", RPR_ORDER)
    for a in ("w:ascii", "w:hAnsi", "w:cs"):
        rFonts.set(qn(a), cfg.font_name)
    # Theme references win over explicit names in Word, so they must go.
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        if rFonts.get(qn(a)) is not None:
            del rFonts.attrib[qn(a)]

    half = _half_points(cfg)
    get_or_add_ordered(rPr, "w:sz", RPR_ORDER).set(qn("w:val"), half)
    get_or_add_ordered(rPr, "w:szCs", RPR_ORDER).set(qn("w:val"), half)


def set_run_font(r: _Element, cfg: WordExportConfig) -> None:
    rPr = r.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        r.insert(0, rPr)
    set_font_on_rpr(rPr, cfg)


def set_paragraph_mark_font(p: _Element, cfg: WordExportConfig) -> None:
    """The paragraph mark carries its own font; an unstyled mark leaves a
    visibly taller empty line, which is what makes "zero spacing" look broken."""
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p.insert(0, pPr)
    rPr = pPr.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        # w:rPr must precede w:sectPr inside w:pPr (last-paragraph-of-section case).
        sectPr = pPr.find(qn("w:sectPr"))
        if sectPr is not None:
            sectPr.addprevious(rPr)
        else:
            pPr.append(rPr)
    set_font_on_rpr(rPr, cfg)


def set_spacing_zero(p: _Element) -> None:
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p.insert(0, pPr)
    spacing = pPr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        pStyle = pPr.find(qn("w:pStyle"))
        rPr = pPr.find(qn("w:rPr"))
        if pStyle is not None:
            pStyle.addnext(spacing)
        elif rPr is not None:
            rPr.addprevious(spacing)
        else:
            pPr.insert(0, spacing)
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    spacing.set(qn("w:beforeAutospacing"), "0")
    spacing.set(qn("w:afterAutospacing"), "0")


def format_fonts_and_spacing(doc: DocxDocument, cfg: WordExportConfig) -> None:
    """House font + zero spacing on every paragraph and run in the body."""
    body = doc.element.body
    for p in body.iter(qn("w:p")):
        set_spacing_zero(p)
        set_paragraph_mark_font(p, cfg)
    for r in body.iter(qn("w:r")):
        set_run_font(r, cfg)


def set_document_defaults(doc: DocxDocument, cfg: WordExportConfig) -> None:
    """Backstop: make the house font the default at style / docDefaults level too,
    so paragraphs Word adds later (or content pasted in) inherit it."""
    try:
        normal = doc.styles["Normal"]
        normal.font.name = cfg.font_name
        normal.font.size = Pt(cfg.font_size_pt)
        set_font_on_rpr(normal.element.get_or_add_rPr(), cfg)
    except KeyError:
        # A document without a "Normal" style is legal; docDefaults below still
        # applies, so this is a warning, not a failure.
        log.warning("document has no 'Normal' style; skipped style-level default")

    styles_el = doc.styles.element
    docDefaults = styles_el.find(qn("w:docDefaults"))
    if docDefaults is None:
        docDefaults = OxmlElement("w:docDefaults")
        styles_el.insert(0, docDefaults)
    rPrDefault = docDefaults.find(qn("w:rPrDefault"))
    if rPrDefault is None:
        rPrDefault = OxmlElement("w:rPrDefault")
        docDefaults.insert(0, rPrDefault)
    rPr = rPrDefault.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        rPrDefault.append(rPr)
    set_font_on_rpr(rPr, cfg)


def apply_house_style(doc: DocxDocument, cfg: WordExportConfig) -> None:
    """`cfg.font_name` at `cfg.font_size_pt` with zero paragraph spacing
    everywhere, including paragraph marks and ``docDefaults``.

    Call this last: it also covers paragraphs added by the structural rules.
    """
    set_document_defaults(doc, cfg)
    format_fonts_and_spacing(doc, cfg)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def set_cell_fill(tc: _Element, fill_hex: str) -> None:
    """Set the background shading of a table cell (``<w:tc>``)."""
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = OxmlElement("w:tcPr")
        tc.insert(0, tcPr)
    shd = get_or_add_ordered(tcPr, "w:shd", TCPR_ORDER)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)


def set_run_bold_color(r: _Element, color: str = HEADER_TEXT_COLOR) -> None:
    """Make a run bold in `color` -- used for the header row of a table.

    Defaults to white because that is the only colour legible across the whole
    plausible range of `WordExportConfig.header_fill`; the question box passes
    black, since its own header row is white.
    """
    set_run_bold(r)
    rPr = r.find(qn("w:rPr"))
    if rPr is None:  # pragma: no cover -- set_run_bold always creates it
        rPr = OxmlElement("w:rPr")
        r.insert(0, rPr)
    get_or_add_ordered(rPr, "w:color", RPR_ORDER).set(qn("w:val"), color)


_BORDER_SIDES = ["top", "left", "bottom", "right", "insideH", "insideV"]


def set_table_borders(
    tbl: _Element, cfg: WordExportConfig, *, color: str | None = None
) -> None:
    """Give a table thin single borders in `color` (default `cfg.border_color`)."""
    hexc = color or cfg.border_color
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    borders = get_or_add_ordered(tblPr, "w:tblBorders", TBLPR_ORDER)
    for side in _BORDER_SIDES:
        el = borders.find(qn("w:" + side))
        if el is None:
            el = OxmlElement("w:" + side)
            # Keep the six sides in canonical order -- same reason as
            # get_or_add_ordered, one level deeper.
            ref = None
            for existing in borders:
                cl = existing.tag.split("}")[-1]
                if cl in _BORDER_SIDES and _BORDER_SIDES.index(
                    cl
                ) > _BORDER_SIDES.index(side):
                    ref = existing
                    break
            if ref is not None:
                ref.addprevious(el)
            else:
                borders.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")          # 4 eighths of a point = 0.5 pt
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), hexc)


#: Child order of ``<w:tcBorders>``.
_TC_BORDER_SIDES = [
    "top", "start", "left", "bottom", "end", "right", "insideH", "insideV",
    "tl2br", "tr2bl",
]


def set_cell_border(tc: _Element, side: str, val: str) -> None:
    """Set one side of a cell's own border to `val` ("nil" erases it).

    A cell border beats the table's ``insideH``/``insideV``, which is the only
    way to remove a single rule from a table that otherwise has all of them.
    """
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = OxmlElement("w:tcPr")
        tc.insert(0, tcPr)
    borders = get_or_add_ordered(tcPr, "w:tcBorders", TCPR_ORDER)
    el = borders.find(qn("w:" + side))
    if el is None:
        el = OxmlElement("w:" + side)
        ref = None
        for existing in borders:
            cl = existing.tag.split("}")[-1]
            if cl in _TC_BORDER_SIDES and _TC_BORDER_SIDES.index(
                cl
            ) > _TC_BORDER_SIDES.index(side):
                ref = existing
                break
        if ref is not None:
            ref.addprevious(el)
        else:
            borders.append(el)
    el.set(qn("w:val"), val)


def weld_rows(upper: _Element, lower: _Element) -> None:
    """Erase the rule between two vertically adjacent cells.

    For where one block of prose had to be cut over several rows so that Word can
    break it across pages: the reader must still see one box, not a box with a
    line ruled through the middle of an explanation.
    """
    set_cell_border(upper, "bottom", "nil")
    set_cell_border(lower, "top", "nil")


def set_table_width_pct(tbl: _Element, pct: float = 100.0) -> None:
    """Make a table occupy `pct` percent of the available width.

    Uniformity depends on it: without an explicit preferred width Word autofits
    each question box to its own longest line, so no two boxes end up the same
    size.
    """
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblW = get_or_add_ordered(tblPr, "w:tblW", TBLPR_ORDER)
    tblW.set(qn("w:type"), "pct")
    tblW.set(qn("w:w"), str(int(round(pct * 50))))   # fiftieths of a percent
    layout = get_or_add_ordered(tblPr, "w:tblLayout", TBLPR_ORDER)
    layout.set(qn("w:type"), "fixed")


def color_nested_table(tbl: _Element, cfg: WordExportConfig) -> None:
    """Apply the blue banded colour scheme to one table (Grid Table 4 look)."""
    set_table_borders(tbl, cfg)
    rows = tbl.findall(qn("w:tr"))
    for ri, tr in enumerate(rows):
        cells = tr.findall(qn("w:tc"))
        if ri == 0:
            fill = cfg.header_fill          # header row
        elif ri % 2 == 1:
            fill = cfg.band_fill            # first / third / ... body row
        else:
            fill = PLAIN_FILL               # second / fourth / ... body row
        for tc in cells:
            set_cell_fill(tc, fill)
            if ri == 0:
                for r in tc.findall(".//" + qn("w:r")):
                    set_run_bold_color(r)


def is_nested_table(tbl: _Element) -> bool:
    """True when `tbl` lives inside a table cell (i.e. it is an inner table)."""
    parent = tbl.getparent()
    while parent is not None:
        if parent.tag == qn("w:tc"):
            return True
        parent = parent.getparent()
    return False


def color_nested_tables(doc: DocxDocument, cfg: WordExportConfig) -> None:
    """Colour every nested table in the document; outer question boxes are left
    alone (they are the frame, not data)."""
    for tbl in doc.element.body.findall(".//" + qn("w:tbl")):
        if is_nested_table(tbl):
            color_nested_table(tbl, cfg)


# --------------------------------------------------------------------------- #
# Cell iteration
# --------------------------------------------------------------------------- #


def iter_unique_cells(doc: DocxDocument) -> Iterator[_Cell]:
    """Yield every top-level table cell exactly once, merged cells included once.

    NOTE: keep the actual ``<w:tc>`` elements in the `seen` set (not their
    ``id()``). A set holds real references, so the lxml proxies stay alive and
    their identity stays stable; using ``id()`` here is unsafe because transient
    proxies get garbage-collected and their ``id()`` reused, which would wrongly
    mark distinct cells as "already seen" and skip them.
    """
    seen = set()
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                tc = cell._tc
                if tc in seen:
                    continue
                seen.add(tc)
                yield cell
