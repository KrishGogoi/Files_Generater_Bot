r"""Input readers: turn any supported file into one ordered `SourceDocument`.

Everything downstream (rule parser, AI parser, exporters) reads *only*
`SourceDocument`, so no other module ever learns what a .docx is. The contract
that every reader in this package honours:

* `lines` are in **true document order** with `idx` contiguous from 0.
* One `SourceLine` per *visual* line: paragraph text is split on "\n" / "\v"
  (Word soft breaks, PowerPoint ``a:br``) because a whole option block very
  often lives inside a single paragraph.
* Text is **verbatim** -- never stripped, never whitespace-collapsed. Blank
  lines are kept: they are the parsers' main structural signal.
* No reader ever invents, reorders or merges a line, which is what makes
  `SourceDocument.text_of()` a provable slice of the original file. The whole
  "wording is preserved exactly" requirement rests on that.

`block_id` groups lines that came from one source block: all lines split out of
one paragraph share it, and (for docx) so do all lines flattened out of one
question-box table, which lets a parser tell "these belong together".
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Sequence

from ..models import (
    DocKind,
    ImageAsset,
    LineKind,
    SourceDocument,
    SourceLine,
    Spans,
    TableAsset,
)

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".docx", ".pdf", ".txt", ".pptx"})

#: suffix -> (relative module, function). Imported lazily so launching the GUI
#: never pays PyMuPDF / python-pptx import time for a format nobody opened.
_READERS: dict[str, tuple[str, str]] = {
    ".docx": (".docx_reader", "read_docx"),
    ".pdf": (".pdf_reader", "read_pdf"),
    ".txt": (".txt_reader", "read_txt"),
    ".pptx": (".pptx_reader", "read_pptx"),
}

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "ReadError",
    "UnsupportedFormat",
    "read_any",
    "read_docx",
    "read_pdf",
    "read_pptx",
    "read_txt",
]


class UnsupportedFormat(Exception):
    """The file's extension is not one this package can read."""


class ReadError(Exception):
    """The extension was supported but the file could not be read."""


# --------------------------------------------------------------------------- #
# Internal helpers shared by the four readers (not public API)
# --------------------------------------------------------------------------- #

#: Word/PowerPoint soft line breaks.  is what python-pptx returns for
#: <a:br>, legacy Word content stores a soft break as a literal VT too, and
#:  /  are what some HTML-to-Word converters leave behind.
_LINE_SPLIT_RE = re.compile("\r\n|\r|\n|\x0b|\u2028|\u2029")

#: "Q.1", "Q1", "Q 1)", "Qn 2", "Question 12.", or a lone "1." on its own
#: row. Used only to recognise the header row of a question-box table -- the
#: real question grammar lives in `app.parse.rules`.
_QUESTION_HEADER_HINT_RE = re.compile(
    r"^(?:q(?:uestion|ues|n)?\s*[.:)\-]?\s*\d+\b.*|\d+\s*[.)]\s*)$",
    re.IGNORECASE,
)


def _split_visual_lines(raw: str) -> list[str]:
    """Split one paragraph's text into the lines a reader would *see*.

    Always returns at least one element, so an empty paragraph still produces
    one blank `SourceLine` (blank lines are structure, not noise).
    """
    return _LINE_SPLIT_RE.split(raw)


def _visual_line_offsets(raw: str) -> list[tuple[int, str]]:
    """`_split_visual_lines` plus each part's start offset inside `raw`.

    Needed to carry character-indexed styling (bold runs) across the split: the
    separators are one or two characters wide, so the parts' offsets cannot be
    reconstructed from their lengths alone.
    """
    out: list[tuple[int, str]] = []
    pos = 0
    for m in _LINE_SPLIT_RE.finditer(raw):
        out.append((pos, raw[pos : m.start()]))
        pos = m.end()
    out.append((pos, raw[pos:]))
    return out


def _slice_spans(spans: Spans, start: int, length: int) -> Spans:
    """Re-base `spans` onto the window ``[start, start + length)``."""
    if not spans or length <= 0:
        return ()
    end = start + length
    out: list[tuple[int, int]] = []
    for s, e in spans:
        s2, e2 = max(s, start) - start, min(e, end) - start
        if s2 < e2:
            out.append((s2, e2))
    return tuple(out)


def _looks_like_question_header(text: str) -> bool:
    """True when `text` reads like the header row of a question box."""
    head = text.strip()
    if not head or len(head) > 40:
        return False
    return bool(_QUESTION_HEADER_HINT_RE.match(head))


class _DocBuilder:
    """Accumulator that keeps line indices, anchors and dedupe in one place.

    Every reader funnels through this so that `idx` is guaranteed contiguous
    and image/table anchors follow one rule instead of four.
    """

    __slots__ = ("path", "kind", "lines", "images", "tables", "warnings",
                 "_seen_sha1", "_last_nonblank")

    def __init__(self, path: str, kind: DocKind) -> None:
        self.path = path
        self.kind = kind
        self.lines: list[SourceLine] = []
        self.images: list[ImageAsset] = []
        self.tables: list[TableAsset] = []
        self.warnings: list[str] = []
        self._seen_sha1: set[str] = set()
        self._last_nonblank: int = -1

    # -- lines ------------------------------------------------------------- #

    def add_line(
        self,
        text: str,
        *,
        kind: LineKind = LineKind.PARAGRAPH,
        block_id: int = 0,
        ref: str | None = None,
        page: int | None = None,
        bold: Spans = (),
    ) -> int:
        """Append one verbatim line and return its index."""
        idx = len(self.lines)
        line = SourceLine(
            idx=idx, text=text, kind=kind, block_id=block_id, ref=ref, page=page,
            bold=bold,
        )
        self.lines.append(line)
        if not line.is_blank:
            self._last_nonblank = idx
        return idx

    def add_text_lines(
        self,
        raw: str,
        *,
        kind: LineKind = LineKind.PARAGRAPH,
        block_id: int = 0,
        ref: str | None = None,
        page: int | None = None,
        bold: Spans = (),
    ) -> list[int]:
        """Split `raw` on soft breaks and append every visual line.

        `bold` is indexed against `raw` as a whole and is cut up to follow the
        split, so each line ends up with the ranges that belong to it.
        """
        if not bold:
            return [
                self.add_line(part, kind=kind, block_id=block_id, ref=ref, page=page)
                for part in _split_visual_lines(raw)
            ]
        return [
            self.add_line(
                part, kind=kind, block_id=block_id, ref=ref, page=page,
                bold=_slice_spans(bold, offset, len(part)),
            )
            for offset, part in _visual_line_offsets(raw)
        ]

    @property
    def anchor(self) -> int:
        """Index of the last non-blank line so far (-1 when there is none)."""
        return self._last_nonblank

    def nonblank_at_or_before(self, idx: int) -> int:
        """Nearest non-blank line at or before `idx`; -1 if the doc starts blank."""
        start = min(idx, len(self.lines) - 1)
        for i in range(start, -1, -1):
            if not self.lines[i].is_blank:
                return i
        return -1

    # -- assets ------------------------------------------------------------ #

    def add_image(
        self,
        data: bytes,
        width: int,
        height: int,
        fmt: str = "png",
        *,
        anchor: int | None = None,
        page: int | None = None,
    ) -> bool:
        """Append an image unless its bytes were already seen.

        Word re-embeds the same picture for every alternate-content fallback and
        every repeat use, so identical bytes are dropped: a question would
        otherwise pick up several copies of one figure.
        """
        asset = ImageAsset(
            data=data,
            width=int(width),
            height=int(height),
            fmt=(fmt or "png").lower().lstrip("."),
            anchor_line=self.anchor if anchor is None else anchor,
            page=page,
        )
        digest = asset.sha1
        if digest in self._seen_sha1:
            log.debug("%s: dropped duplicate image %s", self.path, digest[:10])
            return False
        self._seen_sha1.add(digest)
        self.images.append(asset)
        return True

    def add_table(
        self,
        rows: Sequence[Sequence[str]],
        *,
        anchor: int | None = None,
        page: int | None = None,
    ) -> bool:
        """Append a grid, skipping one that holds no text at all."""
        clean = tuple(tuple(str(c) for c in row) for row in rows)
        if not any(c.strip() for row in clean for c in row):
            return False
        self.tables.append(
            TableAsset(
                rows=clean,
                anchor_line=self.anchor if anchor is None else anchor,
                page=page,
            )
        )
        return True

    def warn(self, message: str) -> None:
        """Record a non-fatal problem; the UI shows these, nothing raises."""
        log.warning("%s: %s", self.path, message)
        self.warnings.append(message)

    # -- result ------------------------------------------------------------ #

    def finish(self) -> SourceDocument:
        """Resolve leading anchors and return the finished document."""
        lead = next((ln.idx for ln in self.lines if not ln.is_blank), -1)
        if lead < 0 and self.lines:
            lead = 0
        # An asset that precedes every line (a figure at the very top) anchors
        # forward to the first real line instead of dangling at -1.
        self.images = [
            replace(a, anchor_line=lead) if a.anchor_line < 0 else a
            for a in self.images
        ]
        self.tables = [
            replace(t, anchor_line=lead) if t.anchor_line < 0 else t
            for t in self.tables
        ]
        return SourceDocument(
            path=self.path,
            kind=self.kind,
            lines=self.lines,
            images=self.images,
            tables=self.tables,
            warnings=self.warnings,
        )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def read_any(path: str | Path) -> SourceDocument:
    """Read `path` with the reader that matches its extension.

    Raises `UnsupportedFormat` for an extension we do not handle and
    `ReadError` (with the file name) for anything that goes wrong inside a
    reader, so callers have exactly two failure modes to show the user.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        known = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedFormat(
            f"{p.name}: unsupported file type '{suffix or p.name}' "
            f"(supported: {known})"
        )
    if not p.is_file():
        raise ReadError(f"{p.name}: not a readable file ({p})")

    module_name, func_name = _READERS[suffix]
    try:
        reader = getattr(import_module(module_name, __name__), func_name)
        doc = reader(p)
    except (UnsupportedFormat, ReadError):
        raise
    except Exception as exc:
        # WHY blanket: python-docx, python-pptx and MuPDF each raise their own
        # private exception hierarchies (plus zipfile/lxml errors) for a corrupt
        # file. The caller only needs "this file could not be read".
        raise ReadError(f"{p.name}: {type(exc).__name__}: {exc}") from exc

    log.info(
        "read %s (%s): %d lines, %d images, %d tables, %d warnings",
        p.name, doc.kind.value, len(doc.lines), len(doc.images),
        len(doc.tables), len(doc.warnings),
    )
    return doc


def __getattr__(name: str) -> object:
    """Expose the four reader functions without importing their libraries early."""
    for module_name, func_name in _READERS.values():
        if name == func_name:
            value = getattr(import_module(module_name, __name__), func_name)
            globals()[name] = value          # cache: __getattr__ runs once
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
