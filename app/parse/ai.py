"""Span-anchored AI parser: the model returns line indices, never text.

Why this shape? The user's hard requirement is that question wording is
*preserved exactly and only reformatted*. Any design where a model re-emits the
question text can silently paraphrase, "fix" a typo, translate a drug name or
drop a clause -- and no amount of prompting makes that guarantee provable. So
the model is only ever asked a structural question: **which lines** form the
stem, which form each option, which line states the answer. Every string in the
resulting `Question` then comes from `SourceDocument.text_of(...)`, a verbatim
slice, which `app.parse.reconcile` can re-derive and prove.

Consequences visible in this module:

* `EXTRACTION_SCHEMA` contains no field that can carry stem or option text. The
  only free-text fields are `answer.rationale` and `notes`, neither of which is
  ever exported as question content.
* Option labels come from the source line via `rules.strip_option_label`; the
  model's letter is only a fallback for unlabelled (bulleted) choices.
* Quality flags are computed locally from the source, not asked for.

Long documents are split into chunks on structural boundaries and sent
concurrently -- that concurrency is where the wall-clock speed comes from. If a
chunk's API call fails for any reason, that chunk falls back to the deterministic
parser and the run continues: an API outage degrades accuracy, never the app.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from app.models import (
    OPTION_LABELS,
    Answer,
    AnswerOrigin,
    Flag,
    ImageAsset,
    Option,
    ParseResult,
    Parser,
    Question,
    SourceDocument,
    SourceLine,
    TableAsset,
    labels_are_sequential,
    normalise_label,
)
from app.ingest import _looks_like_question_header
from app.parse import rules
from app.parse.providers import Provider, ProviderError

log = logging.getLogger(__name__)

#: Lines per request. 400 lines of dense MCQ text is ~12k input tokens, well
#: inside a free-tier request, and small enough that one truncated reply costs
#: little.
MAX_CHUNK_LINES = 400
#: Lines re-sent at the head of the next chunk. Chunks are cut on structural
#: boundaries so a question should never straddle one; the overlap is insurance,
#: and duplicates are removed by first stem line.
CHUNK_OVERLAP = 12
#: Per-request wall clock. Generous: a dense chunk with thinking enabled can take
#: well over a minute, and a fallback to rules is worse than waiting.
REQUEST_TIMEOUT_S = 180.0
DEFAULT_CONCURRENCY = 4

#: Stems that talk about a picture cannot be answered without it -- see
#: `Flag.REFERENCES_MISSING_IMAGE` and `app.parse.answers`. The one pattern the
#: rule parser uses, so the two never disagree about the flag.
_IMAGE_WORDS_RE = rules.IMAGE_REFERENCE_RE
#: Boundary heuristics for chunking only. Deliberately local rather than reusing
#: `rules.QUESTION_HEADER_RE`: this is a "where may I cut" hint, and it must not
#: break if the rule parser tunes its own regex groups.
_Q_HEADER_HINT_RE = re.compile(r"^\s*q(?:ues|uestion)?\s*[.:\-)]?\s*\d{1,3}\b", re.I)
_NUM_HEADER_HINT_RE = re.compile(r"^\s*\d{1,3}\s*[.)\]](?:\s|$)")
#: A numbered line *inside* a stem ("1. Ligamentum Flavum") -- the classic
#: false-positive that turns one question into six.
_SUBITEM_RE = re.compile(r"^\s*\(?\d{1,2}\)?\s*[.)\]]?\s+\S")

# --------------------------------------------------------------------------- #
# Schema -- indices only
# --------------------------------------------------------------------------- #


def _int_array(description: str) -> dict:
    """A fresh `array<integer>` node.

    A function rather than a shared constant because the Gemini SDK mutates the
    schema dict it is given (it inlines `$defs` in place), so no sub-tree may be
    aliased into two places.
    """
    return {
        "type": "array",
        "items": {"type": "integer"},
        "description": description,
    }


EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "description": (
        "A line-index map of every multiple-choice question in the supplied "
        "lines. Contains no question text: the caller slices the source."
    ),
    "properties": {
        "questions": {
            "type": "array",
            "description": (
                "Every question found, in document order. Empty array when the "
                "supplied lines contain no multiple-choice questions."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "number": {
                        "type": "integer",
                        "description": (
                            "The question number printed in the document "
                            "('Q.7' -> 7). Use 0 when the document prints none."
                        ),
                    },
                    "stem_lines": _int_array(
                        "Indices of the lines forming the question text, in "
                        "document order, including continuation lines and any "
                        "numbered list that belongs to the stem."
                    ),
                    "options": {
                        "type": "array",
                        "description": "The answer choices, in printed order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "enum": list(OPTION_LABELS),
                                    "description": (
                                        "The choice letter ONLY: 'A', never "
                                        "'A.' or '(A)'."
                                    ),
                                },
                                "lines": _int_array(
                                    "Indices of the lines of this choice, "
                                    "including wrapped continuation lines."
                                ),
                            },
                            "required": ["label", "lines"],
                            "propertyOrdering": ["label", "lines"],
                        },
                    },
                    "answer": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "nullable": True,
                                "description": (
                                    "Letter of the correct choice as STATED by "
                                    "the document. null when the document "
                                    "states no answer."
                                ),
                            },
                            "stated_in_source": {
                                "type": "boolean",
                                "description": (
                                    "true only when the document itself prints "
                                    "the key (e.g. 'Ans. B)')."
                                ),
                            },
                            "source_lines": _int_array(
                                "Indices of the line(s) that state the answer. "
                                "Empty when nothing is stated."
                            ),
                            "confidence": {
                                "type": "number",
                                "description": (
                                    "0-1 confidence that this reading of the "
                                    "stated answer is right."
                                ),
                            },
                            "rationale": {
                                "type": "string",
                                "description": (
                                    "Leave empty during extraction; answers are "
                                    "derived in a later pass."
                                ),
                            },
                        },
                        "required": [
                            "label",
                            "stated_in_source",
                            "source_lines",
                            "confidence",
                            "rationale",
                        ],
                        "propertyOrdering": [
                            "label",
                            "stated_in_source",
                            "source_lines",
                            "confidence",
                            "rationale",
                        ],
                    },
                    "explanation_lines": _int_array(
                        "Indices of an explanation block belonging to this "
                        "question ('Explanation:', 'Incorrect Options:', 'Take "
                        "Home Message:'). Empty when absent."
                    ),
                    "notes": {
                        "type": "string",
                        "description": (
                            "At most one short clause, only when something is "
                            "unusual (e.g. 'stem contains a numbered "
                            "sub-list'). Never question text."
                        ),
                    },
                },
                "required": [
                    "number",
                    "stem_lines",
                    "options",
                    "answer",
                    "explanation_lines",
                    "notes",
                ],
                "propertyOrdering": [
                    "number",
                    "stem_lines",
                    "options",
                    "answer",
                    "explanation_lines",
                    "notes",
                ],
            },
        }
    },
    "required": ["questions"],
    "propertyOrdering": ["questions"],
}

# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a precise structural analyser for medical exam papers. You are given one
document rendered as numbered lines:

L0| first line of the document
L1| second line
L2|

Your ONLY job is to report WHICH LINE INDICES form each multiple-choice
question. The calling program builds all text by slicing the source lines you
point at, so an index is the only thing that can be right or wrong.

You must NEVER rewrite, retype, translate, summarise, shorten, expand, correct,
complete, reorder or "improve" any text. You never output question text at all.

Return JSON matching the schema. For every question, in document order:

- number: the number printed in the document ("Q.7" -> 7). 0 if none is printed.
- stem_lines: indices of the lines forming the question text, in document order.
  Include wrapped continuation lines, a lead-in like "Choose the correct
  statements", and any numbered list of items that belongs to the stem.
  EXCLUDE a line whose whole content is just the question label ("Q.1",
  "Question 1", "3."). INCLUDE the line when the number sits on the same line as
  the stem text ("1. Which nerve is injured?").
- options: one entry per printed answer choice, in printed order. "label" is the
  LETTER ONLY ("A", never "A." or "(A)"); the program strips the printed prefix
  from the source line itself. If choices are bulleted or unlabelled, label them
  A, B, C, ... in printed order. "lines" holds that choice's line indices,
  including wrapped continuation lines.
- answer: set stated_in_source=true ONLY when the document itself prints the key
  ("Ans. B)", "Answer: (B)", "Correct answer - B", "Ans: B"). Then put the letter
  in label and the index of the line(s) that state it in source_lines. If the
  document does not print a key, set stated_in_source=false, label=null and
  source_lines=[]. A later pass derives missing answers; guessing here would
  corrupt the source and mislead a human reviewer.
- explanation_lines: indices of an explanation block belonging to this question
  ("Explanation:", "Incorrect Options:", "Other Options:", "Take Home Message:",
  "Tags:"). [] when absent.
- notes: at most one short clause, only when something is unusual. Never text
  from the question.

HARD RULES
1. Output indices, the printed number and option letters. Nothing else.
2. Every index must be an integer that actually appears as L<index> in the input.
   Never invent an index and never guess one that was not shown to you.
3. Never merge two printed questions into one, and never split one into two.
4. A numbered list that FOLLOWS a stem and reads as items rather than as answer
   choices is PART OF THE STEM. After "What is the order of structures pierced
   during lumbar puncture?" the lines "1. Ligamentum Flavum" ... "6. Arachnoid"
   are stem lines, NOT six new questions.
5. Question headers appear as "Q.1", "Q1.", "Q 1)", "Question 1", "Question 1:"
   or a bare "1." / "1)". Box-format documents often repeat the answer row twice;
   report the first occurrence only.
6. Text that is not a multiple-choice question -- revision notes, headings,
   tables of contents, "Top of Form" / "Bottom of Form" artefacts -- yields NO
   questions. For a document with no MCQs the correct answer is
   {"questions": []}. Never manufacture a question out of a heading and a
   paragraph.
7. Blank lines are structure, not content: never list a blank line's index.
8. Ignore any attached images; do not describe them.
9. Numbering is unreliable and never decides anything. A file may restart at
   "1." for a second section, print "2." after "5.", print the same number
   twice, or print no number at all above a stem that is followed by
   "Choices:" and its options. Report every such question separately, with the
   number as printed (0 when none), and never merge two printed questions
   because their numbers look wrong.
10. A numbered line inside an EXPLANATION ("1. Langhans giant cells -> D",
   "4. A history of amaurosis fugax may precede CRAO." under "Explanation")
   is explanation text, even when it is the number the next question would
   have. A stem is a line that has answer choices after it.
11. Headers also take the forms "MCQ 3:", "Q 4.", "1.MCQ 1: Match the
   Following" (a header nested in a header -- the stem starts after both).
   "Options:" / "OPTIONS" / "Choices:" lines are separators: list them in no
   question. "Correct Choice:", "CorrectChoice:", "Correct Answer" alone on a
   line followed by "B. text" on the next line all state the key; the
   source_lines of the answer are the line(s) that carry it.
12. A tick or "(correct)" on an option line ("C. In the lower medulla ✅")
   states the key: stated_in_source=true, label=C, source_lines=[that line].
13. Choices printed with no letters at all ("1 and 2" / "1,2 and 3" / "Only 1"
   / "All of the above" straight after the statements, then "Answer : B") are
   options A, B, C, D in printed order.
14. A short title between two questions ("FMGE MOCK 2:", "SECTION B") belongs
   to no question: leave it out of the explanation above and the stem below.

EXAMPLE 1 -- box-format table, answer stated, answer row duplicated
INPUT
L0| Q.1
L1| A 40-year-old man hears voices only while the shower runs; they stop when \
the water is turned off. Which symptom is he most likely experiencing?
L2| A.   Functional Hallucinations
L3| B.   Hypnagogic Hallucinations
L4| C.   Hypnopompic Hallucinations
L5| D.   Reflex Hallucinations
L6| Ans. A)   Functional Hallucinations
L7| Ans. A)   Functional Hallucinations
L8|
OUTPUT
{"questions":[{"number":1,"stem_lines":[1],"options":[{"label":"A","lines":[2]},\
{"label":"B","lines":[3]},{"label":"C","lines":[4]},{"label":"D","lines":[5]}],\
"answer":{"label":"A","stated_in_source":true,"source_lines":[6],\
"confidence":1.0,"rationale":""},"explanation_lines":[],\
"notes":"answer row repeated in source"}]}

EXAMPLE 2 -- flat list, bare numbers, NO answer key anywhere
INPUT
L0| 1. Which among the following muscles marked is an abductor of vocal cord?
L1|
L2| A. A
L3| B. C
L4| C. B
L5| D. D
L6|
L7| 2. A female patient presents with recurrent epistaxis and a nasal mass. CT \
shows localized bony invasion. Which is the least likely diagnosis?
L8|
L9| A. Juvenile angiofibroma
L10| B. Antrochoanal polyp
L11| C. Inverted papilloma with malignant transformation
L12| D. Olfactory esthesioneuroblastoma
OUTPUT
{"questions":[{"number":1,"stem_lines":[0],"options":[{"label":"A","lines":[2]},\
{"label":"B","lines":[3]},{"label":"C","lines":[4]},{"label":"D","lines":[5]}],\
"answer":{"label":null,"stated_in_source":false,"source_lines":[],\
"confidence":0.0,"rationale":""},"explanation_lines":[],"notes":""},\
{"number":2,"stem_lines":[7],"options":[{"label":"A","lines":[9]},\
{"label":"B","lines":[10]},{"label":"C","lines":[11]},{"label":"D","lines":[12]}],\
"answer":{"label":null,"stated_in_source":false,"source_lines":[],\
"confidence":0.0,"rationale":""},"explanation_lines":[],"notes":""}]}

EXAMPLE 3 -- numbered sub-items inside a stem (ONE question, not six)
INPUT
L0| 2. Choose the correct statements from the following:
L1| A. Low BP stimulates baroreceptors
L2| B. Cardiac output lowers with deepening plane of anesthesia
L3|
L4| 3. What is the order of structures pierced during lumbar puncture?
L5| 1. Ligamentum Flavum
L6| 2. Supraspinatus ligament
L7| 3. Interspinous ligament
L8| 4. Dura
L9| 5. Sub dural
L10| 6. Arachnoid
OUTPUT
{"questions":[{"number":2,"stem_lines":[0],"options":[{"label":"A","lines":[1]},\
{"label":"B","lines":[2]}],"answer":{"label":null,"stated_in_source":false,\
"source_lines":[],"confidence":0.0,"rationale":""},"explanation_lines":[],\
"notes":""},{"number":3,"stem_lines":[4,5,6,7,8,9,10],"options":[],\
"answer":{"label":null,"stated_in_source":false,"source_lines":[],\
"confidence":0.0,"rationale":""},"explanation_lines":[],\
"notes":"stem contains a numbered sub-list; no answer choices printed"}]}
"""

_USER_TEMPLATE = """\
Document: {name}
Lines L{first}-L{last} of {total}{context}

{body}

Report every multiple-choice question in these lines as JSON line indices only.
"""

# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def build_chunks(
    doc: SourceDocument, *, max_lines: int = MAX_CHUNK_LINES,
    overlap: int = CHUNK_OVERLAP,
) -> list[tuple[int, int]]:
    """Split `doc` into `(start, end)` half-open line ranges.

    Cuts land on structure -- a question header (cut before it) or a blank line
    (cut after it) -- so a question is never sliced in half. `overlap` lines are
    replayed at the head of the next chunk as insurance; `parse` removes the
    duplicates by first stem line.
    """
    n = len(doc.lines)
    if n <= 0:
        return []

    max_lines = max(20, int(max_lines))
    # Overlap must stay well under half a chunk or the loop cannot advance.
    overlap = max(0, min(int(overlap), max_lines // 4))
    if n <= max_lines:
        return [(0, n)]

    chunks: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + max_lines, n)
        if end < n:
            end = _cut_point(doc, start, end, floor=start + max(1, max_lines // 2))
        chunks.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _cut_point(doc: SourceDocument, start: int, end: int, *, floor: int) -> int:
    """Best place to end a chunk in `(floor, end]`, searching backwards.

    Preference order: an explicit "Q1."-style header (cut before it) > a blank
    line (cut after it) > a bare "1."-style header. A bare number is last
    because it may be a sub-item of the current stem, and cutting there is
    exactly the split we are trying to avoid.
    """
    header_cut: int | None = None
    blank_cut: int | None = None
    numeric_cut: int | None = None

    for i in range(end - 1, max(floor, 0) - 1, -1):
        line = doc.lines[i]
        if line.is_blank:
            if blank_cut is None:
                blank_cut = i + 1
            continue
        text = line.text
        if _Q_HEADER_HINT_RE.match(text):
            header_cut = i
            break
        if numeric_cut is None and _NUM_HEADER_HINT_RE.match(text):
            numeric_cut = i

    for candidate in (header_cut, blank_cut, numeric_cut):
        if candidate is not None and start < candidate <= end:
            return candidate
    return end


# --------------------------------------------------------------------------- #
# payload -> Question
# --------------------------------------------------------------------------- #


def _indices(raw: Any, offset: int, doc: SourceDocument) -> list[int]:
    """Coerce a model index list into sorted, in-range, non-blank line indices.

    Out-of-range and duplicate indices are dropped rather than raising: a single
    hallucinated number must not cost us the question.
    """
    n = len(doc.lines)
    seen: set[int] = set()
    out: list[int] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for value in raw:
        if isinstance(value, bool):     # bool is an int subclass; not an index
            continue
        try:
            idx = int(value) + offset
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < n) or idx in seen:
            continue
        line = doc.lines[idx]
        if line.is_blank:               # blank lines carry no content
            continue
        seen.add(idx)
        out.append(idx)
    # Sorted so the verbatim slice reads in document order even if the model
    # listed the indices out of order.
    return sorted(out)


def _split_option_label(text: str) -> tuple[str | None, str]:
    """`rules.strip_option_label`, guarded at the module boundary."""
    try:
        label, body = rules.strip_option_label(text)
    except (AttributeError, TypeError, ValueError) as exc:
        log.debug("strip_option_label failed on %r: %s", text[:40], exc)
        return None, text
    if not isinstance(body, str):
        return None, text
    return (label if isinstance(label, str) else None), body


def _stem_from_lines(doc: SourceDocument, stem_lines: Iterable[int]) -> str:
    """Verbatim stem slice with the source's OWN numbering removed.

    The exports print their own uniform label ("Q1."), so leaving the document's
    "1." in place renders as "Q1. 1. A 55-year-old woman...". The rule-based
    parser has always stripped it in `_stem_text`; this is the same step for the
    model-anchored path, and it is the only edit made to a stem -- a prefix is
    removed, never a word rewritten.
    """
    text = doc.text_of(stem_lines)
    if not text:
        return text
    head, _, rest = text.partition("\n")
    try:
        _, stripped = rules.strip_question_prefix(head)
    except (AttributeError, TypeError, ValueError) as exc:
        log.debug("strip_question_prefix failed on %r: %s", head[:40], exc)
        return text
    if not stripped.strip():
        # The header line held nothing but the number: drop the line entirely
        # rather than leaving a blank first paragraph on the slide.
        return rest
    return f"{stripped}\n{rest}" if rest else stripped


def _split_answer_line(text: str) -> tuple[str | None, str]:
    """`rules.parse_answer_line`, guarded at the module boundary."""
    try:
        label, body = rules.parse_answer_line(text)
    except (AttributeError, TypeError, ValueError) as exc:
        log.debug("parse_answer_line failed on %r: %s", text[:40], exc)
        return None, text
    if not isinstance(body, str):
        return None, text
    return (label if isinstance(label, str) else None), body


def _build_option(
    doc: SourceDocument, raw_label: Any, lines: list[int], position: int
) -> Option:
    """One `Option`, text sliced from the source and label taken from it.

    The printed prefix ("A.", "(a)") is removed by the deterministic stripper,
    never by the model. The model's letter is used only when the source line
    carries no label at all (bulleted choices).
    """
    text = doc.text_of(lines)
    stripped_label, body = _split_option_label(text)
    if body.strip():
        text = body
    try:
        # A tick on the option is an ornament, not wording (rules.py does the
        # same for its own options, and reconcile accepts either form).
        _, text = rules.strip_answer_marker(text)
    except (AttributeError, TypeError, ValueError) as exc:
        log.debug("strip_answer_marker failed on %r: %s", text[:40], exc)

    label = stripped_label or normalise_label(str(raw_label or ""))
    if not label:
        label = OPTION_LABELS[position] if position < len(OPTION_LABELS) else "?"
    return Option(label=label, text=text, source_lines=list(lines))


def _build_answer(
    doc: SourceDocument, raw: Any, options: list[Option], offset: int
) -> Answer:
    """The stated answer, or an explicitly unknown one.

    `stated_in_source=false` means we record nothing at all -- not even a label
    the model volunteered. An unstated answer is derived later by
    `app.parse.answers`, which marks it `AnswerOrigin.AI` so a human can see
    that it is new information.
    """
    if not isinstance(raw, dict):
        return Answer()

    stated = bool(raw.get("stated_in_source"))
    source_lines = _indices(raw.get("source_lines"), offset, doc)
    if not stated or not source_lines:
        return Answer()

    line_text = doc.text_of(source_lines)
    parsed_label, body = _split_answer_line(line_text)
    if parsed_label is None and body == line_text:
        # Not an answer line at all -- the key may be a ticked option
        # ("C. In the lower medulla ✅"), whose letter and body are its own.
        opt_label, opt_body = _split_option_label(line_text)
        if opt_label is not None:
            parsed_label = opt_label
            try:
                _, body = rules.strip_answer_marker(opt_body)
            except (AttributeError, TypeError, ValueError):
                body = opt_body
        else:
            body = ""
    label = parsed_label or normalise_label(str(raw.get("label") or ""))
    if not label:
        # The document pointed at an answer line we cannot read a letter from;
        # treat it as unknown rather than inventing one.
        return Answer()

    text = body if body.strip() else ""
    if not text:
        match = next((o for o in options if o.label.upper() == label.upper()), None)
        if match is not None:
            text = match.text          # copied from the option, never rewritten

    try:
        confidence = float(raw.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = min(1.0, max(0.0, confidence))
    if confidence <= 0.0:
        # The document states the key, so absence of a number is a schema
        # artefact, not doubt.
        confidence = 1.0
    return Answer(
        label=label,
        text=text,
        origin=AnswerOrigin.SOURCE,
        confidence=confidence,
        rationale="",
        source_lines=source_lines,
    )


def _attach_assets(doc: SourceDocument, q: Question) -> None:
    """Give the question the images/tables anchored inside its line span."""
    span = q.all_lines
    if not span:
        return
    lo, hi = span[0], span[-1]
    for img in doc.images:
        if lo <= img.anchor_line <= hi and img not in q.images:
            q.images.append(img)
    # Tables only count when they sit between the stem and the first option --
    # that is a "match the columns" grid belonging to the question. A table
    # anchored in the explanation is a reference chart and must not be dragged
    # onto the question slide.
    first_option = min((i for o in q.options for i in o.source_lines), default=-1)
    table_hi = first_option if first_option >= 0 else lo
    for tbl in doc.tables:
        if lo <= tbl.anchor_line < table_hi and tbl not in q.tables:
            q.tables.append(tbl)


def apply_local_flags(q: Question, doc: SourceDocument) -> None:
    """Set every quality flag that can be decided from the source alone.

    Deliberately computed here rather than requested from the model: a
    deterministic check cannot hallucinate, and `answers.py` depends on
    `REFERENCES_MISSING_IMAGE` being trustworthy before it will decline to
    answer a question.
    """
    if not q.stem.strip():
        q.flag(Flag.EMPTY_STEM)

    if len(q.options) < 2:
        q.flag(Flag.FEW_OPTIONS)
    elif len(q.options) > len(OPTION_LABELS):
        q.flag(Flag.MANY_OPTIONS)

    normalised = [o.normalised() for o in q.options if o.text.strip()]
    if len(normalised) != len(set(normalised)):
        q.flag(Flag.DUPLICATE_OPTIONS)

    if q.options and not labels_are_sequential(q.option_labels):
        q.flag(Flag.NON_SEQUENTIAL_LABELS)

    if not q.answer_matches_an_option:
        q.flag(Flag.ANSWER_NOT_IN_OPTIONS)

    if not q.answer.is_known:
        q.flag(Flag.NO_ANSWER)

    # A stem that points at a picture we do not have cannot be answered honestly.
    if _IMAGE_WORDS_RE.search(q.stem) and not q.images:
        q.flag(Flag.REFERENCES_MISSING_IMAGE)

    # More than one numbered item inside the stem == the sub-list trap survived
    # correctly (one question, its own list) -- worth surfacing to the reviewer.
    subitems = sum(
        1
        for i in q.stem_lines[1:]
        if (line := doc.line(i)) is not None and _SUBITEM_RE.match(line.text)
    )
    if subitems >= 2:
        q.flag(Flag.NUMBERED_SUBITEMS)


def spans_to_questions(
    doc: SourceDocument, payload: dict, offset: int = 0
) -> list[Question]:
    """Turn one model payload into `Question` objects.

    `offset` is added to every index the model returned. `parse` renders chunks
    with absolute `L<idx>` numbers (see `SourceDocument.numbered_text`) and so
    passes 0; a caller that renders a chunk with chunk-local numbering passes the
    chunk start instead.

    All text is produced by `doc.text_of(...)`. A malformed question is skipped
    with a log line -- one bad entry never costs the rest of the payload.
    """
    if not isinstance(payload, dict):
        return []
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        # Some models answer the bare array; providers wrap that as "items".
        raw_questions = payload.get("items")
    if not isinstance(raw_questions, list):
        return []

    out: list[Question] = []
    for position, item in enumerate(raw_questions):
        try:
            question = _question_from_item(doc, item, offset)
        except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
            log.warning("skipping malformed question #%d in payload: %s",
                        position, exc)
            continue
        if question is not None:
            out.append(question)
    return out


def _question_from_item(
    doc: SourceDocument, item: Any, offset: int
) -> Question | None:
    if not isinstance(item, dict):
        return None

    stem_lines = _indices(item.get("stem_lines"), offset, doc)

    options: list[Option] = []
    raw_options = item.get("options")
    if isinstance(raw_options, list):
        for position, raw in enumerate(raw_options):
            if not isinstance(raw, dict):
                continue
            lines = _indices(raw.get("lines"), offset, doc)
            if not lines:
                continue
            options.append(_build_option(doc, raw.get("label"), lines, position))

    if not stem_lines and not options:
        return None      # nothing anchored to the source: not a question

    try:
        number = int(item.get("number") or 0)
    except (TypeError, ValueError):
        number = 0

    explanation_lines = _indices(item.get("explanation_lines"), offset, doc)
    notes = item.get("notes")
    question = Question(
        number=max(0, number),
        stem=_stem_from_lines(doc, stem_lines),
        stem_lines=stem_lines,
        options=options,
        answer=_build_answer(doc, item.get("answer"), options, offset),
        explanation=doc.text_of(explanation_lines),
        explanation_lines=explanation_lines,
        source_path=doc.path,
        parser=Parser.AI,
        confidence=1.0,
        notes=str(notes)[:240] if isinstance(notes, str) else "",
    )
    _attach_assets(doc, question)
    apply_local_flags(question, doc)
    # Structural doubt lowers confidence; the answer's own confidence lives on
    # `Question.answer.confidence`.
    if question.has(Flag.EMPTY_STEM) or question.has(Flag.FEW_OPTIONS):
        question.confidence = 0.6
    return question


# --------------------------------------------------------------------------- #
# Parsing a whole document
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _ChunkOutcome:
    """What one chunk produced, so results can be reassembled in order."""

    order: int
    questions: list[Question]
    warning: str = ""
    ai_calls: int = 0


def parse(
    doc: SourceDocument,
    provider: Provider | None,
    *,
    settings: Any = None,
    progress: Callable[[int, int], None] | None = None,
) -> ParseResult:
    """Parse `doc` with the AI, chunk by chunk, concurrently.

    Never raises for provider trouble: a failing chunk is re-parsed with
    `app.parse.rules` and a warning is recorded, so the worst case is
    rule-quality output rather than no output.
    """
    started = time.monotonic()
    result = ParseResult(source=doc, parser=Parser.AI)

    if not doc.lines:
        result.warnings.append("document contains no text lines")
        result.elapsed_s = time.monotonic() - started
        return result

    if provider is None:
        # Callers should not do this, but degrading is far friendlier than
        # raising in a background worker thread.
        log.info("ai.parse called without a provider; using the rule parser")
        fallback = rules.parse(doc)
        fallback.warnings.append("no AI provider configured - used rule parser")
        fallback.elapsed_s = time.monotonic() - started
        return fallback

    chunks = build_chunks(doc)
    total = len(chunks)
    workers = _worker_count(settings, total)
    log.info(
        "AI parse: %d lines in %d chunk(s), %d worker(s), provider=%s model=%s",
        len(doc.lines), total, workers, provider.name, provider.model,
    )

    outcomes: list[_ChunkOutcome] = []
    done = 0
    _report(progress, done, total)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai") as pool:
        futures = {
            pool.submit(_parse_chunk, doc, provider, order, start, end): order
            for order, (start, end) in enumerate(chunks)
        }
        for future in as_completed(futures):
            order = futures[future]
            try:
                outcomes.append(future.result())
            except Exception as exc:      # noqa: BLE001 - a worker must not kill the run
                log.exception("chunk %d failed unexpectedly", order)
                outcomes.append(
                    _ChunkOutcome(
                        order=order,
                        questions=[],
                        warning=f"chunk {order + 1}: unexpected failure ({exc})",
                    )
                )
            done += 1
            _report(progress, done, total)

    outcomes.sort(key=lambda o: o.order)
    collected: list[Question] = []
    for outcome in outcomes:
        result.ai_calls += outcome.ai_calls
        if outcome.warning:
            result.warnings.append(outcome.warning)
        collected.extend(outcome.questions)

    result.questions = _dedupe_and_order(collected)
    rescued = _absorb_trailing_lines(doc, result.questions)
    if rescued:
        log.info("recovered %d trailing source line(s) no question had claimed",
                 rescued)
    _finalise_numbering(result.questions)
    result.elapsed_s = time.monotonic() - started
    log.info(
        "AI parse produced %d question(s) in %.2fs (%d call(s), %d flagged)",
        len(result.questions), result.elapsed_s, result.ai_calls, result.n_flagged,
    )
    return result


def _absorb_trailing_lines(doc: SourceDocument, questions: list[Question]) -> int:
    """Append to each question's explanation the lines it left unclaimed before
    the next question begins. Returns how many lines were recovered.

    `explanation_lines` comes from the model, and the prompt shows it the
    headings an explanation block is made of -- "Explanation:", "Incorrect
    Options:", "Take Home Message:". A document that spells one of them even
    slightly differently ("Take-Home Message:") can end the block early, and
    every line after it then belongs to no question at all: not exported, not
    flagged, simply gone. Real files do this -- `Ch 06 proteins Corrected.docx`
    lost all 17 of its Take-Home sections that way, while the rule-based parser
    kept every one.

    No prompt wording makes that impossible, so the fix is not prompt wording.
    The *span* decides instead: a question owns the lines from its own last line
    up to the line before the next question starts, and anything non-blank in
    there is appended to its explanation.

    Deliberately trailing-only. Absorbing an unclaimed line from the *middle* of
    a question would move stem or option text below the answer, turning a
    silent loss into a silent reordering; a line the model skipped mid-question
    is a different bug and is left for the flags to catch. The worst this can do
    is carry one line more than the model intended, which is recoverable by
    reading, unlike a line that was never written.

    The scan also stops at a question header. "Question 2" is a line no question
    claims -- the exporter writes its own header row -- so without that stop it
    would be swept into question 1's explanation, which is precisely the kind of
    silent reordering this is meant to avoid.
    """
    if not questions:
        return 0
    # A question's span ends where the next one's first line begins.
    starts = [min(q.all_lines) if q.all_lines else 0 for q in questions]
    rescued = 0
    for position, question in enumerate(questions):
        owned = question.all_lines
        if not owned:
            continue
        begin = min(owned)
        later = [s for s in starts[position + 1:] if s > begin]
        stop = min(later) if later else len(doc.lines)
        extra: list[int] = []
        for i in range(max(owned) + 1, stop):
            line = doc.line(i)
            if line is None or not line.text.strip():
                continue
            if _looks_like_question_header(line.text):
                break               # the next question has begun
            extra.append(i)
        if not extra:
            continue
        question.explanation_lines = sorted(set(question.explanation_lines) | set(extra))
        question.explanation = doc.text_of(question.explanation_lines)
        rescued += len(extra)
    return rescued


def _parse_chunk(
    doc: SourceDocument, provider: Provider, order: int, start: int, end: int
) -> _ChunkOutcome:
    """One provider call, with a deterministic fallback for that chunk."""
    body = doc.numbered_text(start, end)
    context = (
        ""
        if start == 0 and end >= len(doc.lines)
        else "\n(This is one part of a longer document; report only questions "
             "that appear in these lines.)"
    )
    user = _USER_TEMPLATE.format(
        name=_display_name(doc.path),
        first=start,
        last=max(start, end - 1),
        total=len(doc.lines),
        context=context,
        body=body,
    )

    try:
        payload = provider.complete_json(
            SYSTEM_PROMPT,
            user,
            EXTRACTION_SCHEMA,
            timeout=REQUEST_TIMEOUT_S,
        )
    except ProviderError as exc:
        log.warning("chunk %d (L%d-L%d) AI call failed: %s", order, start, end - 1, exc)
        questions = _rules_fallback(doc, start, end)
        return _ChunkOutcome(
            order=order,
            questions=questions,
            warning=(
                f"lines L{start}-L{end - 1}: AI unavailable ({exc}); "
                f"used the rule-based parser ({len(questions)} question(s))"
            ),
            ai_calls=1,
        )

    questions = spans_to_questions(doc, payload, 0)
    if not questions and _looks_like_mcq_text(doc, start, end):
        # The model returned nothing for lines that clearly contain questions:
        # cross-check with the deterministic parser rather than losing them.
        recovered = _rules_fallback(doc, start, end)
        if recovered:
            log.info(
                "chunk %d: AI found nothing, rules recovered %d question(s)",
                order, len(recovered),
            )
            return _ChunkOutcome(
                order=order,
                questions=recovered,
                warning=(
                    f"lines L{start}-L{end - 1}: AI returned no questions; "
                    f"recovered {len(recovered)} with the rule-based parser"
                ),
                ai_calls=1,
            )
    return _ChunkOutcome(order=order, questions=questions, ai_calls=1)


def _rules_fallback(doc: SourceDocument, start: int, end: int) -> list[Question]:
    """Run the deterministic parser over `doc.lines[start:end]` only.

    The slice is re-indexed from 0 because `SourceDocument.line()` is positional;
    every index the rule parser reports is then shifted back to absolute so the
    questions stay comparable with AI output and provable against the source.
    """
    try:
        window = _sub_document(doc, start, end)
        parsed = rules.parse(window)
    except Exception as exc:      # noqa: BLE001 - the fallback must never raise
        log.exception("rule-based fallback failed for L%d-L%d", start, end)
        log.debug("fallback error detail: %s", exc)
        return []

    out: list[Question] = []
    for question in parsed.questions:
        try:
            _shift_indices(question, start)
            question.source_path = doc.path
            # Re-derive from the real document so text and assets are anchored
            # to absolute indices.
            question.stem = _stem_from_lines(doc, question.stem_lines)
            for option in question.options:
                text = doc.text_of(option.source_lines)
                _, body = _split_option_label(text)
                option.text = body if body.strip() else text
            question.explanation = doc.text_of(question.explanation_lines)
            question.images = []
            question.tables = []
            _attach_assets(doc, question)
        except (TypeError, ValueError, AttributeError, IndexError) as exc:
            log.warning("could not re-anchor a fallback question: %s", exc)
            continue
        out.append(question)
    return out


def _sub_document(doc: SourceDocument, start: int, end: int) -> SourceDocument:
    """A standalone `SourceDocument` for one line window, re-indexed from 0."""
    lines: list[SourceLine] = [
        replace(line, idx=i) for i, line in enumerate(doc.lines[start:end])
    ]
    images: list[ImageAsset] = [
        replace(img, anchor_line=img.anchor_line - start)
        for img in doc.images
        if start <= img.anchor_line < end
    ]
    tables: list[TableAsset] = [
        replace(tbl, anchor_line=tbl.anchor_line - start)
        for tbl in doc.tables
        if start <= tbl.anchor_line < end
    ]
    return SourceDocument(
        path=doc.path, kind=doc.kind, lines=lines, images=images, tables=tables
    )


def _shift_indices(q: Question, offset: int) -> None:
    """Move every line index on `q` by `offset`, in place."""
    if not offset:
        return
    q.stem_lines = [i + offset for i in q.stem_lines]
    for option in q.options:
        option.source_lines = [i + offset for i in option.source_lines]
    q.answer.source_lines = [i + offset for i in q.answer.source_lines]
    q.explanation_lines = [i + offset for i in q.explanation_lines]


def _looks_like_mcq_text(doc: SourceDocument, start: int, end: int) -> bool:
    """Do these lines obviously contain MCQs?

    Used only to decide whether an empty AI reply deserves a cross-check. Two or
    more option-looking lines plus a header is a strong signal; revision notes
    (`notes_formatted.docx`) score zero, which is exactly why we must not simply
    always run the fallback.
    """
    headers = 0
    option_like = 0
    for line in doc.lines[start:end]:
        text = line.text.strip()
        if not text:
            continue
        if _Q_HEADER_HINT_RE.match(text) or _NUM_HEADER_HINT_RE.match(text):
            headers += 1
        label, body = _split_option_label(text)
        if label and body.strip():
            option_like += 1
    return headers >= 1 and option_like >= 2


def _dedupe_and_order(questions: Iterable[Question]) -> list[Question]:
    """Drop questions recovered twice from the chunk overlap, keep source order.

    Identity is the first stem line: two payload entries anchored at the same
    source line are the same printed question. The richer copy wins, because the
    chunk that saw the whole question beats the one that saw its tail.
    """
    best: dict[int, Question] = {}
    unanchored: list[Question] = []
    for q in questions:
        if not q.stem_lines:
            unanchored.append(q)
            continue
        key = q.stem_lines[0]
        current = best.get(key)
        if current is None or _richness(q) > _richness(current):
            best[key] = q

    ordered = [best[k] for k in sorted(best)]
    ordered.extend(unanchored)
    return ordered


def _richness(q: Question) -> tuple[int, int, int]:
    """Comparable "how complete is this reading" score for dedupe."""
    return (
        len(q.options),
        1 if q.answer.is_known else 0,
        len(q.stem_lines) + len(q.explanation_lines),
    )


def _finalise_numbering(questions: list[Question]) -> None:
    """Renumber only when the source printed no numbers of its own.

    The user's files are answer banks whose numbering is meaningful; silently
    renumbering "Q.17" to 3 because a chunk started late would be a data loss.
    """
    printed = [q.number for q in questions if q.number > 0]
    renumber = len(printed) < len(questions) or len(set(printed)) != len(printed)
    for ordinal, q in enumerate(questions, start=1):
        q.source_index = ordinal - 1
        if q.source_number <= 0 and q.number > 0:
            q.source_number = q.number     # what the file printed, kept for the user
        if renumber:
            q.number = ordinal


def _worker_count(settings: Any, total: int) -> int:
    raw = getattr(settings, "max_concurrency", None) if settings is not None else None
    try:
        wanted = int(raw) if raw else DEFAULT_CONCURRENCY
    except (TypeError, ValueError):
        wanted = DEFAULT_CONCURRENCY
    return max(1, min(wanted if wanted > 0 else DEFAULT_CONCURRENCY, max(1, total)))


def _report(progress: Callable[[int, int], None] | None, done: int, total: int) -> None:
    """Progress callbacks come from the UI; a bad one must not kill the run."""
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:      # noqa: BLE001 - defensive: it is someone else's callback
        log.exception("progress callback raised")


def _display_name(path: str) -> str:
    cleaned = (path or "").replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or "document"
