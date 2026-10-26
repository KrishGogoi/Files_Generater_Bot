"""Core data model for Krishna's Super Bot.

The one rule that shapes everything here: **content is never regenerated.**

Every piece of question text carries the source line indices it came from
(`*_lines` fields). Text is produced by slicing `SourceDocument.lines`, never by
an AI writing prose. The AI's only job is to say *which lines* form the stem,
which form each option, and which option is correct. That makes the
"preserve wording exactly" requirement structural rather than aspirational --
`app.parse.reconcile` can prove it by re-deriving text from the indices.

The single exception is `Answer` with `origin == AnswerOrigin.AI`: when a source
document states no answer key, the correct label is genuinely new information.
It is always flagged so the UI and the exports can mark it as AI-derived.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Sequence

# --------------------------------------------------------------------------- #
# Enums / constants
# --------------------------------------------------------------------------- #

#: Canonical option labels, in order. Index position == ordinal.
OPTION_LABELS = tuple("ABCDEFGH")

#: Half-open ``(start, end)`` character ranges into a piece of text.
#:
#: Used for the bold runs carried over from the source document. Offsets are
#: character positions into the *stored* string (the one an exporter writes), so
#: a span survives being sliced out of the source and is independent of how the
#: text is later split into lines.
Spans = tuple[tuple[int, int], ...]


class DocKind(str, Enum):
    """What sort of file a `SourceDocument` was read from."""

    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    PPTX = "pptx"


class LineKind(str, Enum):
    """Where inside the source a `SourceLine` came from.

    Matters for export: a line read from a table cell round-trips differently
    from a body paragraph, and PDF lines have no XML identity at all.
    """

    PARAGRAPH = "paragraph"   # docx body <w:p> / pptx text frame / txt line
    CELL = "cell"             # docx or pptx table cell
    PDF_LINE = "pdf_line"     # a text line recovered from a PDF page


class AnswerOrigin(str, Enum):
    """Provenance of the correct answer -- never silently mixed."""

    SOURCE = "source"   # the document stated it (e.g. "Ans. B) ...")
    AI = "ai"           # the model derived it; NEW information, always flagged
    NONE = "none"       # unknown; nothing stated and nothing derived


class Parser(str, Enum):
    """Which engine produced a `Question`."""

    AI = "ai"
    RULES = "rules"
    AI_RULES = "ai+rules"   # AI structure, rule-based repair applied
    MANUAL = "manual"       # edited by the user in the review table


class Flag(str, Enum):
    """Machine-checkable quality warnings surfaced in the review table.

    These drive the UI's colour coding and the "needs attention" filter. They
    are warnings, never silent drops -- a flagged question is still exported.
    """

    NO_ANSWER = "no_answer"                       # no key in source, none derived
    AI_ANSWER = "ai_answer"                        # answer is AI-derived, not stated
    LOW_CONFIDENCE = "low_confidence"              # model was unsure
    FEW_OPTIONS = "few_options"                    # < 2 options detected
    MANY_OPTIONS = "many_options"                  # > 8 options detected
    DUPLICATE_OPTIONS = "duplicate_options"        # two options with identical text
    NON_SEQUENTIAL_LABELS = "non_sequential_labels"  # e.g. A, B, D
    ANSWER_NOT_IN_OPTIONS = "answer_not_in_options"
    """The stated answer letter matches none of the parsed options.

    Always blocking: an answer key pointing at an option that does not exist
    would be exported as authoritative, and silently wrong exam material is the
    worst thing this app could produce."""
    REFERENCES_MISSING_IMAGE = "references_missing_image"
    """Stem says "in the image shown" but no image was attached -- an answer
    cannot honestly be derived, so it must not be guessed."""
    VERBATIM_MISMATCH = "verbatim_mismatch"        # text != source slice
    EMPTY_STEM = "empty_stem"
    NUMBERED_SUBITEMS = "numbered_subitems"
    """Stem contains its own numbered list (e.g. "1. Ligamentum flavum"), the
    classic false-positive for a bare `^\\d+[.)]` question splitter."""
    MERGED_DUPLICATE = "merged_duplicate"          # near-identical Q found while merging
    TRUNCATED = "truncated"                        # source line looked cut off


# --------------------------------------------------------------------------- #
# Source-side model (produced by app.ingest.*)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceLine:
    """One verbatim line of the source document.

    `text` is exactly what the document contained, with no normalisation beyond
    stripping the line terminator. Leading/trailing spaces are preserved so that
    exports can reproduce them; comparisons use `.strip()` where appropriate.
    """

    idx: int
    text: str
    kind: LineKind = LineKind.PARAGRAPH
    block_id: int = 0
    #: Opaque locator for the originating element, used by docx exporters to
    #: find the source <w:p> again (e.g. "p:41" or "tbl:3/r:1/c:0/p:2").
    ref: str | None = None
    #: Page (PDF) or slide (PPTX) number, 1-based; None for docx/txt.
    page: int | None = None
    #: Character ranges of `text` the source showed in **bold**. Empty when the
    #: format carries no such information (PDF, TXT) or nothing was bold.
    bold: Spans = ()

    @property
    def stripped(self) -> str:
        return self.text.strip()

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class ImageAsset:
    """A picture pulled out of the source, kept as raw bytes.

    `anchor_line` is the index of the nearest preceding non-blank `SourceLine`,
    which is how images get attributed to a question.
    """

    data: bytes
    width: int
    height: int
    fmt: str = "png"
    anchor_line: int = -1
    page: int | None = None

    @property
    def sha1(self) -> str:
        """Content hash -- used to drop the duplicate images Word often embeds."""
        return hashlib.sha1(self.data).hexdigest()

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 1.0


@dataclass(frozen=True, slots=True)
class TableAsset:
    """A grid (e.g. a match-the-columns table) extracted verbatim, cell by cell."""

    rows: tuple[tuple[str, ...], ...]
    anchor_line: int = -1
    page: int | None = None

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def is_empty(self) -> bool:
        return not any(c.strip() for row in self.rows for c in row)


@dataclass(slots=True)
class SourceDocument:
    """A file flattened into an ordered line list plus its images and tables.

    Every reader in `app.ingest` returns this shape, so downstream parsing is
    completely independent of the input format.
    """

    path: str
    kind: DocKind
    lines: list[SourceLine] = field(default_factory=list)
    images: list[ImageAsset] = field(default_factory=list)
    tables: list[TableAsset] = field(default_factory=list)
    #: Non-fatal reader problems (unreadable image, missing PDF text layer, ...).
    warnings: list[str] = field(default_factory=list)

    # -- text access ------------------------------------------------------- #

    def line(self, idx: int) -> SourceLine | None:
        return self.lines[idx] if 0 <= idx < len(self.lines) else None

    def text_of(self, indices: Iterable[int], *, sep: str = "\n") -> str:
        """Join the verbatim text of `indices`, skipping out-of-range ones.

        This is the ONLY sanctioned way to build question text. Indices are used
        in the order given (not re-sorted) so callers stay in control.
        """
        out: list[str] = []
        for i in indices:
            ln = self.line(i)
            if ln is not None:
                out.append(ln.text)
        return sep.join(out)

    def numbered_text(self, start: int = 0, end: int | None = None) -> str:
        """The document rendered as ``L<idx>| <text>`` -- the exact form handed
        to the AI so it can answer in line indices instead of prose."""
        end = len(self.lines) if end is None else end
        return "\n".join(
            f"L{ln.idx}| {ln.text}" for ln in self.lines[start:end]
        )

    @property
    def full_text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


# --------------------------------------------------------------------------- #
# Question-side model (produced by app.parse.*)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Option:
    """One answer choice. `text` excludes the "A." / "(A)" label prefix."""

    label: str
    text: str
    source_lines: list[int] = field(default_factory=list)
    images: list[ImageAsset] = field(default_factory=list)
    #: Bold ranges of `text`, as the source had them (see `Spans`).
    bold: Spans = ()

    def normalised(self) -> str:
        """Casefolded, whitespace-collapsed text for duplicate detection."""
        return " ".join(self.text.split()).casefold()


@dataclass(slots=True)
class Answer:
    """The correct choice, with explicit provenance.

    `label` is the option letter. `text` is the answer body as stated by the
    source when `origin is SOURCE`; for an AI-derived answer it is copied from
    the matching `Option.text` so it still contains no generated prose.
    `rationale` is the one field allowed to hold model-written words -- it is
    shown in the review table and never written into an export as question text.
    """

    label: str | None = None
    text: str = ""
    origin: AnswerOrigin = AnswerOrigin.NONE
    confidence: float = 0.0
    rationale: str = ""
    source_lines: list[int] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.label is not None and self.origin is not AnswerOrigin.NONE

    @property
    def is_ai_derived(self) -> bool:
        return self.origin is AnswerOrigin.AI


@dataclass(slots=True)
class Question:
    """One fully-resolved MCQ.

    Invariant enforced by `app.parse.reconcile.verify_question`: for a question
    with `verbatim_ok is True`, `stem` equals the source text of `stem_lines`
    modulo the label prefix, and likewise for every option.
    """

    number: int = 0
    #: The number the source printed for this question (0 when it printed
    #: none). `number` is the 1..N count in document order that the exports
    #: write; this is what it was before `app.parse.rules.assign_numbers`
    #: renumbered, kept so a user can switch back to the file's own numbering.
    source_number: int = 0
    stem: str = ""
    stem_lines: list[int] = field(default_factory=list)
    #: Bold ranges of `stem` / `explanation`, as the source had them. Filled by
    #: `app.parse.styles.annotate_bold` once, after parsing, so no parser has to
    #: think about formatting.
    stem_bold: Spans = ()
    options: list[Option] = field(default_factory=list)
    answer: Answer = field(default_factory=Answer)
    explanation: str = ""
    explanation_lines: list[int] = field(default_factory=list)
    explanation_bold: Spans = ()
    images: list[ImageAsset] = field(default_factory=list)
    tables: list[TableAsset] = field(default_factory=list)

    source_path: str = ""
    source_index: int = 0          # ordinal within its source file
    parser: Parser = Parser.RULES
    confidence: float = 1.0
    flags: list[Flag] = field(default_factory=list)
    verbatim_ok: bool = True
    #: Human-readable detail for VERBATIM_MISMATCH / debugging.
    notes: str = ""

    # -- convenience -------------------------------------------------------- #

    def flag(self, f: Flag) -> None:
        if f not in self.flags:
            self.flags.append(f)

    def has(self, f: Flag) -> bool:
        return f in self.flags

    @property
    def option_labels(self) -> list[str]:
        return [o.label for o in self.options]

    def option(self, label: str) -> Option | None:
        want = label.strip().upper()
        return next((o for o in self.options if o.label.upper() == want), None)

    def tables_between_stem_and_options(self) -> list["TableAsset"]:
        """Only the grids anchored between the start of the stem and the first option.

        A "match the columns" grid belongs to the question and has to be shown. A
        table further down the document is part of an explanation -- a units
        conversion chart, a reference table -- and putting that on a question
        slide is just noise. Anchor position is what separates the two, so the
        window runs from the stem's first line up to (not including) the first
        option line.

        Returns an empty list when the window cannot be established, because
        "between the stem and the options" is undefined without both ends.
        """
        if not self.tables or not self.stem_lines:
            return []
        first_option = min(
            (i for o in self.options for i in o.source_lines), default=-1
        )
        if first_option < 0:
            return []
        start = min(self.stem_lines)
        return [
            t for t in self.tables if start <= t.anchor_line < first_option
        ]

    def images_after_options(self) -> list["ImageAsset"]:
        """The images in `images` anchored below the options block.

        These sit in the answer / explanation area of the source. Everything
        else -- a picture between the stem and the first option, or one picture
        per option in a picture-choice question -- belongs to the question itself.

        An image anchors to the nearest non-blank line above it, so a picture
        right under the last option anchors to that option's line whether it is
        that option's own picture or the first thing after the options. It counts
        as the option's when the option has no text of its own, or when another
        option also carries a picture (a picture-choice question); otherwise it is
        after the options.

        Returns an empty list when the option lines are unknown, because "below
        the options" is undefined without them.
        """
        opt_lines = [i for o in self.options for i in o.source_lines]
        if not self.images or not opt_lines:
            return []
        first, last = min(opt_lines), max(opt_lines)
        last_option = next(o for o in self.options if last in o.source_lines)
        picture_options = (
            any(first <= img.anchor_line < last for img in self.images)
            or not last_option.text.strip()
        )
        return [
            img for img in self.images
            if img.anchor_line > last
            or (img.anchor_line == last and not picture_options)
        ]

    @property
    def all_lines(self) -> list[int]:
        idx: list[int] = list(self.stem_lines)
        for o in self.options:
            idx.extend(o.source_lines)
        idx.extend(self.answer.source_lines)
        idx.extend(self.explanation_lines)
        return sorted(set(idx))

    @property
    def needs_attention(self) -> bool:
        """True when a human should look at this before export."""
        blocking = {
            Flag.NO_ANSWER, Flag.LOW_CONFIDENCE, Flag.FEW_OPTIONS,
            Flag.VERBATIM_MISMATCH, Flag.EMPTY_STEM,
            Flag.REFERENCES_MISSING_IMAGE, Flag.DUPLICATE_OPTIONS,
            Flag.ANSWER_NOT_IN_OPTIONS,
        }
        return bool(blocking.intersection(self.flags)) or not self.verbatim_ok

    @property
    def answer_matches_an_option(self) -> bool:
        """False when a stated answer letter is not among this question's options.

        Checked by every parser rather than assumed: a source that says
        "Answer: A" while listing only B/C/D is a real shape in the wild
        (a mis-typed option label), and it must never pass silently.
        """
        if not self.answer.label:
            return True
        return self.option(self.answer.label) is not None

    def dedupe_key(self) -> str:
        """Stable key for cross-file duplicate detection (stem + options)."""
        stem = " ".join(self.stem.split()).casefold()
        opts = "|".join(sorted(o.normalised() for o in self.options))
        return hashlib.sha1(f"{stem}||{opts}".encode()).hexdigest()

    def renumbered(self, n: int) -> "Question":
        return replace(self, number=n)


@dataclass(slots=True)
class ParseResult:
    """Everything one parsing run produced, plus how it went."""

    questions: list[Question] = field(default_factory=list)
    source: SourceDocument | None = None
    parser: Parser = Parser.RULES
    warnings: list[str] = field(default_factory=list)
    #: Seconds spent, and how many AI calls it cost -- shown in the UI footer.
    elapsed_s: float = 0.0
    ai_calls: int = 0

    @property
    def n_flagged(self) -> int:
        return sum(1 for q in self.questions if q.needs_attention)

    def __len__(self) -> int:
        return len(self.questions)


# --------------------------------------------------------------------------- #
# Export configuration
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BrandConfig:
    """Cerebellum Academy deck/document branding, editable on the home screen."""

    subject: str = ""
    faculty: str = ""
    date_str: str = ""
    logo_path: str = ""


@dataclass(slots=True)
class WordExportConfig:
    """Uniform-layout settings for the box-format Word export.

    Defaults reproduce the house style from the original FORMATTING_BOX_FILE.py:
    Calibri 14, zero paragraph spacing, one bordered table per question.
    """

    font_name: str = "Calibri"
    font_size_pt: int = 14
    include_answer: bool = True
    include_explanation: bool = True
    #: Write "Ans. X) text" using the source's own wording when available.
    mark_ai_answers: bool = True
    ai_answer_marker: str = "  [AI-derived]"
    #: Blue banded scheme for nested tables (Grid Table 4 - Accent 1 look).
    color_nested_tables: bool = True
    header_fill: str = "4472C4"
    band_fill: str = "D9E1F2"
    #: Every border of every row. Black: the export is printed and photocopied,
    #: and a pale rule is the first thing a copier loses.
    border_color: str = "000000"

    # -- the question box itself -------------------------------------------- #
    #: Subject / faculty / date / logo ahead of the first box. Off: the handout
    #: is the questions, and a cover block is one more thing to delete.
    title_block: bool = False
    #: Shading and font colour of the "Question n" row. Plain black on white --
    #: a reversed blue band costs toner and reads as decoration.
    question_fill: str = "FFFFFF"
    question_text_color: str = "000000"
    #: Repeat the answer line, bold, at the top of the explanation row so the
    #: key is visible while the explanation is being read.
    repeat_answer_in_explanation: bool = True
    #: Reproduce the source document's bold runs in the output.
    preserve_bold: bool = True
    #: Number the boxes 1..N in document order (the count the parser assigned)
    #: rather than with the numbers the source printed. Chaotic files restart
    #: at 1 or repeat a number, so counting is the default; the preview screen
    #: offers the switch for a file whose own numbering must be kept.
    renumber: bool = True


#: How a question slide labels its stem. Chosen in the pre-flight panel that
#: opens before a deck is built, so it is a per-deck decision rather than a
#: buried preference.
QUESTION_PREFIX_STYLES: tuple[str, ...] = ("number", "q", "none")


@dataclass(slots=True)
class PptxExportConfig:
    """Slide-deck settings, mirroring the original PPT builders' house style."""

    slide_w_cm: float = 33.867
    slide_h_cm: float = 19.05
    question_font: str = "Arial Rounded MT Bold"
    question_size_pt: int = 28
    options_font: str = "Arial Rounded MT Bold"
    options_size_pt: int = 24
    min_font_pt: int = 14

    # -- what actually goes on the slides (the pre-flight tick boxes) ------- #
    #: "number" -> "Q1. <stem>", "q" -> "Q. <stem>", "none" -> "<stem>".
    question_prefix: str = "number"
    include_question_text: bool = True
    include_options: bool = True
    #: True -> answer gets its own slide after each question (WITH_ANSWERS
    #: behaviour); False -> answer banner on the question slide.
    answer_on_separate_slide: bool = True
    include_answers: bool = True
    #: Images below the options (answer / explanation area). Images between the
    #: stem and the options, and picture options, are always placed.
    include_images_after_options: bool = True
    question_color: tuple[int, int, int] = (0xFF, 0x00, 0x00)
    options_color: tuple[int, int, int] = (0x00, 0x00, 0x00)
    rule_color: tuple[int, int, int] = (0x1A, 0xB7, 0xEA)
    title_slide: bool = True
    thankyou_slide: bool = True


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def merge_spans(spans: Iterable[tuple[int, int]]) -> Spans:
    """Sort and coalesce ranges, dropping empty ones.

    Touching ranges are merged so each bold stretch becomes exactly one run in
    the output rather than several adjacent ones.
    """
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return tuple(out)


def clamp_spans(spans: Spans, length: int) -> Spans:
    """Trim `spans` to ``[0, length)``, dropping anything wholly outside it.

    Exporters call this on the way out: a question can be edited in the review
    table after `app.parse.styles` annotated it, which leaves the recorded ranges
    pointing past the end of the new text.
    """
    if not spans or length <= 0:
        return ()
    return merge_spans(
        (max(0, s), min(e, length)) for s, e in spans if s < length and e > 0
    )


def normalise_label(raw: str) -> str | None:
    """Turn "(a)", "A.", "b)", " C " into "A"/"B"/"C"; None if not a label."""
    s = raw.strip().strip("()[].)-: \t").upper()
    return s if len(s) == 1 and s in OPTION_LABELS else None


def labels_are_sequential(labels: Sequence[str]) -> bool:
    """True when labels run A, B, C, ... with no gaps or repeats."""
    up = [l.strip().upper() for l in labels]
    return up == list(OPTION_LABELS[: len(up)])
