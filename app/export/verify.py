"""Proof -- not a promise -- that an export preserved the user's wording.

Two independent checks live here:

* :func:`verify_verbatim` compares two .docx files character by character. It is
  the generalised form of ``FORMATTING_BOX_FILE.verify_verbatim``: the only text
  it is allowed to ignore is the bold ``"Option "`` runs the formatter inserts on
  purpose (subtracted from *both* documents, so re-formatting an already
  formatted file still verifies clean).
* :func:`verify_questions_against_source` re-derives every question's text from
  its source line indices and reports the ones that are not a verbatim slice --
  the same invariant `app.parse.reconcile` enforces, checked again at the export
  boundary where it actually matters.

Neither function mutates anything. They answer a question; the caller decides.
"""

from __future__ import annotations

import logging
import re

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from lxml.etree import _Element

from app.export.docx_format import (
    BLOCK_ENDERS,
    KEYWORD_BASES,
    KW_INCORRECT,
    KW_OTHER,
    LABELLED_OPTION_RE,
    OPTION_LABEL_TEXT,
    iter_unique_cells,
    match_keyword,
)
from app.ingest.docx_reader import open_docx
from app.models import Question, SourceDocument

log = logging.getLogger(__name__)

#: Tolerance used when comparing a *derived* source slice with an already
#: label-stripped `Option.text`. This is a comparison aid only -- it never
#: produces exported text, so it does not compete with
#: `app.parse.rules.strip_option_label`, which remains the only sanctioned
#: stripper for content.
_LABEL_PREFIX_RE = re.compile(r"^\(?\s*[A-Ha-h]\s*[)\].:\-]\s*")

#: Same idea for a section keyword that the source keeps inline with its text
#: ("Explanation: because ...") while the parser stores only the body. Built from
#: `KEYWORD_BASES` so a keyword added there cannot be forgotten here.
_KEYWORD_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(b) for b in KEYWORD_BASES) + r")\s*:?\s*",
    re.IGNORECASE,   # sources write "EXPLANATION:" as often as "Explanation:"
)

_EXCERPT = 45   # chars of context shown on each side of a divergence


# --------------------------------------------------------------------------- #
# docx-level verification
# --------------------------------------------------------------------------- #


def concat_text(doc: DocxDocument, skip_runs: frozenset | set = frozenset()) -> str:
    """Exact concatenation of every ``<w:t>`` in document order, omitting the text
    of any run in `skip_runs`.

    Deliberately raw: no separators are inserted between paragraphs, because the
    comparison only has to be self-consistent between two documents, and adding
    separators would hide a paragraph split that moved a character.
    """
    parts: list[str] = []
    for t in doc.element.body.iter(qn("w:t")):
        anc = t.getparent()
        while anc is not None and anc.tag != qn("w:r"):
            anc = anc.getparent()
        if anc is not None and anc in skip_runs:
            continue
        parts.append(t.text or "")
    return "".join(parts)


def inserted_label_runs(doc: DocxDocument) -> set[_Element]:
    """The exact ``<w:r>`` elements that are the "Option " labels the formatter
    inserts (first run of an option line inside the Incorrect/Other-Options
    block, whose text is exactly ``"Option "``).

    Mirrors `docx_format.label_incorrect_options` so the verifier subtracts
    precisely what was added -- nothing more. Elements (not ``id()``) are stored
    for the same reason as in `docx_format.iter_unique_cells`.
    """
    skip: set[_Element] = set()
    # The body itself is scanned too: flat (non-box) documents keep their
    # Incorrect-Options block in body paragraphs, and `word_box.reformat_existing`
    # labels those as well. The two must stay symmetrical or verification would
    # report our own insertion as a content change.
    hosts = [doc, *iter_unique_cells(doc)]
    for cell in hosts:
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
            if not in_region or not LABELLED_OPTION_RE.match(t):
                continue
            runs = [c for c in p._p if c.tag == qn("w:r")]
            if not runs:
                continue
            first = "".join(x.text or "" for x in runs[0].iter(qn("w:t")))
            if first == OPTION_LABEL_TEXT:
                skip.add(runs[0])
    return skip


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _first_divergence(src: str, out: str, *, what: str = "CONTENT DIFFERS") -> str:
    """Human-readable location of the first differing character."""
    for i, (a, b) in enumerate(zip(src, out)):
        if a != b:
            lo = max(0, i - _EXCERPT)
            return (
                f"{what} near char {i}:\n"
                f"          source: ...{src[lo:i + _EXCERPT]!r}...\n"
                f"          output: ...{out[lo:i + _EXCERPT]!r}..."
            )
    return f"{what} in length (source {len(src)} vs output {len(out)} chars)"


def verify_verbatim(src_path: str, dst_path: str) -> tuple[bool, str]:
    """Confirm the output's text equals the source's, apart from the
    intentionally-inserted "Option " labels. Returns ``(ok, detail)``.

    `ok` is True when the text is either character-exact or differs only by
    whitespace (which the user has accepted -- blank lines are inserted by
    design). Any real textual difference returns ``(False, first divergence)``.
    """
    try:
        src_doc = open_docx(src_path)
        out_doc = open_docx(dst_path)
    except (PackageNotFoundError, OSError, ValueError, KeyError) as exc:
        # Verification must never be the thing that crashes an export run.
        log.warning("verify_verbatim could not open a document: %s", exc)
        return False, f"could not open document for verification: {exc}"

    src_skip = inserted_label_runs(src_doc)
    out_skip = inserted_label_runs(out_doc)
    src = concat_text(src_doc, src_skip)
    out = concat_text(out_doc, out_skip)

    n_labels = len(src_skip) + len(out_skip)
    ignored = (
        "" if not n_labels else f" (ignoring {n_labels} inserted 'Option ' label(s))"
    )

    if src == out:
        return True, f"character-exact (text identical to source){ignored}"

    sn = _collapse(src)
    on = _collapse(out)
    if sn == on:
        return True, f"identical apart from whitespace{ignored}"

    return False, _first_divergence(sn, on)


# --------------------------------------------------------------------------- #
# Question-level verification
# --------------------------------------------------------------------------- #


def _slice_matches(derived: str, stored: str, *, strip: re.Pattern[str] | None) -> bool:
    """True when `stored` is `derived` modulo whitespace and an optional prefix."""
    d = _collapse(derived)
    s = _collapse(stored)
    if d == s:
        return True
    if strip is not None and _collapse(strip.sub("", d, count=1)) == s:
        return True
    return False


def _question_detail(q: Question, doc: SourceDocument) -> str:
    """"" when the question is a verbatim slice of `doc`, else what diverged."""
    # -- stem ------------------------------------------------------------- #
    if not q.stem_lines:
        if _collapse(q.stem):
            return f"stem has no source lines (parser={q.parser.value})"
    else:
        derived = doc.text_of(q.stem_lines)
        if not _slice_matches(derived, q.stem, strip=None):
            return "stem: " + _first_divergence(
                _collapse(derived), _collapse(q.stem), what="STEM DIFFERS"
            )

    # -- options ---------------------------------------------------------- #
    for opt in q.options:
        if not opt.source_lines:
            if _collapse(opt.text):
                return f"option {opt.label}: no source lines"
            continue
        derived = doc.text_of(opt.source_lines)
        # The stored text has had its "A." prefix removed by the parser, so the
        # label prefix of the source slice is expected to be absent.
        if not _slice_matches(derived, opt.text, strip=_LABEL_PREFIX_RE):
            return f"option {opt.label}: " + _first_divergence(
                _collapse(derived), _collapse(opt.text), what="OPTION DIFFERS"
            )

    # -- explanation (only when the parser recorded where it came from) ---- #
    if q.explanation_lines:
        derived = doc.text_of(q.explanation_lines)
        if not _slice_matches(derived, q.explanation, strip=_KEYWORD_PREFIX_RE):
            return "explanation: " + _first_divergence(
                _collapse(derived), _collapse(q.explanation), what="EXPLANATION DIFFERS"
            )
    return ""


def verify_questions_against_source(
    questions: list[Question], doc: SourceDocument
) -> list[tuple[Question, str]]:
    """Return ``(question, detail)`` for every question whose text is not a
    verbatim slice of `doc`.

    An empty list means every question's stem, options and (when line-anchored)
    explanation re-derive exactly from the source. Manually edited questions
    (`Parser.MANUAL`) legitimately diverge and are reported like any other -- the
    caller can filter on `Question.parser` if it wants to forgive them.
    """
    bad: list[tuple[Question, str]] = []
    for q in questions:
        try:
            detail = _question_detail(q, doc)
        except (AttributeError, TypeError, IndexError) as exc:
            # A malformed question must not abort verification of the rest.
            log.warning("could not verify question %s: %s", q.number, exc)
            detail = f"verification error: {exc}"
        if detail:
            bad.append((q, detail))
    return bad
