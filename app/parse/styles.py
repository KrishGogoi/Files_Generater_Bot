"""Carry the source document's **bold** runs onto the parsed questions.

Formatting is deliberately kept out of the parsers. They decide *which lines*
make up a stem, an option or an explanation; this module runs once afterwards
and works out which characters of the text they produced were bold in the file.
One pass, one place -- so `app.parse.rules`, `app.parse.ai`,
`app.parse.reconcile` and `app.parse.merge` never have to think about it, and a
`Question` that has been through any combination of them still ends up styled.

The alignment is deliberately suspicious of itself. Stored text is *nearly* the
source text -- a question prefix has been stripped from the stem's first line,
an option label from the option's, and the rules parser calls ``.strip()`` on
the join -- so every line is located inside its source line by search, and a
line that cannot be found contributes nothing rather than guessing an offset.
Wrong bold in the middle of a word is worse than no bold at all.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from app.models import (
    Question,
    SourceDocument,
    Spans,
    clamp_spans,
    merge_spans,
)

log = logging.getLogger(__name__)

__all__ = ["annotate_bold", "spans_for_line"]


def spans_for_line(text: str, source_text: str, source_bold: Spans) -> Spans:
    """`source_bold`, re-based onto `text` found inside `source_text`.

    Returns () when `text` does not occur in the source line at all.
    """
    if not text or not source_bold:
        return ()
    at = source_text.find(text)
    if at < 0:
        return ()
    return clamp_spans(tuple((s - at, e - at) for s, e in source_bold), len(text))


def _align(
    stored: str, doc: SourceDocument, indices: Sequence[int]
) -> Spans:
    """Bold ranges of `stored`, whose lines came from `indices` in order.

    The source lines are consumed with a moving cursor, so a stored line that
    was dropped (a leading blank removed by ``.strip()``) or that lives further
    down the list than its position suggests is still matched to the right one.
    """
    if not stored or not indices:
        return ()
    sources = [doc.line(i) for i in indices]
    if not any(ln is not None and ln.bold for ln in sources):
        return ()          # nothing bold in this question: skip the work

    out: list[tuple[int, int]] = []
    base = 0
    cursor = 0
    for visual in stored.split("\n"):
        if visual:
            found = -1
            for j in range(cursor, len(sources)):
                ln = sources[j]
                if ln is not None and visual in ln.text:
                    found = j
                    break
            if found >= 0:
                ln = sources[found]
                assert ln is not None      # guarded by the loop above
                for s, e in spans_for_line(visual, ln.text, ln.bold):
                    out.append((base + s, base + e))
                cursor = found + 1
        base += len(visual) + 1            # +1 for the "\n" that joined them
    return merge_spans(out)


def annotate_bold(questions: Iterable[Question], doc: SourceDocument) -> int:
    """Fill `stem_bold`, `Option.bold` and `explanation_bold` from `doc`.

    Returns the number of questions that picked up at least one bold range.
    Never raises: a styling failure must not cost the user their questions.
    """
    if not getattr(doc, "lines", None):
        return 0
    if not any(ln.bold for ln in doc.lines):
        return 0                            # nothing bold anywhere in the file

    styled = 0
    for q in questions:
        # A question merged in from another file must not be annotated from this
        # document's line numbers -- they mean nothing to it.
        if q.source_path and doc.path and q.source_path != doc.path:
            continue
        try:
            q.stem_bold = _align(q.stem, doc, q.stem_lines)
            q.explanation_bold = _align(q.explanation, doc, q.explanation_lines)
            for opt in q.options:
                opt.bold = _align(opt.text, doc, opt.source_lines)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            log.debug("bold annotation skipped for question %s: %s", q.number, exc)
            continue
        if q.stem_bold or q.explanation_bold or any(o.bold for o in q.options):
            styled += 1
    if styled:
        log.debug("%s: bold runs carried onto %d question(s)", doc.path, styled)
    return styled
