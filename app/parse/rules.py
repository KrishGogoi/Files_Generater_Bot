"""Deterministic, rule-based MCQ parser.

Three jobs, one implementation:

1. **Fast path** -- most question banks are regular enough that no model is
   needed at all.
2. **Offline mode** -- with no API key the whole app runs on this file.
3. **Cross-check** -- `app.parse.reconcile` diffs this parse against the AI's
   and flags every disagreement for a human.

It therefore has to be genuinely good, not a token fallback. The shapes below
all come from the real corpus and every heuristic here exists because a naive
version of it produced wrong questions in a real user document.

The architectural rule holds here too: a `Question` is built from **line
indices** into the `SourceDocument`, and its text is the verbatim slice of
those lines. The only characters this module ever removes are the structural
prefixes that are not part of the wording:

* the question number (``"1. "``, ``"Q1. "``, ``"Question 1. "``) from the stem,
* the option label (``"A. "``, ``"(a) "``) from an option,
* the answer keyword (``"Ans. B) "``) from the answer.

Two failures in the original scripts are fixed here deliberately:

* ``^Question\\s+(\\d+)`` matched neither ``Q1.`` nor ``Q.1``, which silently
  skipped whole 60-question files -- see `QUESTION_HEADER_RE`.
* a bare ``N.`` inside a stem (``"1. Ligamentum Flavum"`` under
  ``"3. Order of structures pierced..."``) split one question into seven --
  see `_Scanner._header_opens_question`.

A third insight is carried over from ``FORMATTING_BOX_FILE.py``: a single
capital letter plus ``". "`` is *not* automatically an option label, because
medical abbreviations start lines too (``"H. pylori"``, ``"E. coli"``). That
script solved it by only accepting A-F; we accept A-H but require the label to
fit the expected label sequence (see `_plausible_option`), which is stricter
where it matters and looser where it does not.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, NamedTuple, Sequence

from app.models import (
    OPTION_LABELS,
    Answer,
    AnswerOrigin,
    Flag,
    LineKind,
    Option,
    ParseResult,
    Parser,
    Question,
    SourceDocument,
    labels_are_sequential,
)

log = logging.getLogger(__name__)

__all__ = [
    "QUESTION_HEADER_RE",
    "OPTION_RE",
    "INLINE_OPTIONS_RE",
    "ANSWER_RE",
    "SECTION_RE",
    "OPTIONS_HEADER_RE",
    "BULLET_OPTION_RE",
    "IMAGE_REFERENCE_RE",
    "QuestionHeader",
    "match_question_header",
    "strip_question_prefix",
    "strip_option_label",
    "strip_answer_marker",
    "strip_bullet",
    "assign_numbers",
    "parse_answer_line",
    "parse_inline_options",
    "strip_options_prefix",
    "detect_question_starts",
    "parse",
]

# --------------------------------------------------------------------------- #
# Character classes
# --------------------------------------------------------------------------- #

# Word joins non-breaking and thin spaces into pasted text constantly; treating
# them as ordinary space is the difference between a parsed and a missed line.
_SP = r"[ \t    ]"
_SPS = r"[\s    ]"
# Bullet glyphs seen in the corpus (hyphen family, asterisk, all the dots).
_BULLET = r"[-‐‑‒–—―*•‣⁃▪●◦·∙■▸➢]"
# Markdown emphasis leaks in from copy-pasted AI output; it wraps labels, never
# carries meaning, so it may be skipped *before* a label but never inside text.
_EMPH = r"[*_]{0,3}"
# Decorative glyphs some banks put in front of a heading -- the corpus uses "✅"
# in front of "Correct Answer:" and "❌" in front of "Incorrect". They are
# ornaments, so they may be skipped when looking for the keyword that follows;
# only ever at the very start of a line, never inside a body, because an arrow
# or a tick mid-sentence is content.
_ORNAMENT = (
    r"[←-⇿⌀-⏿■-➿⬀-⯿️"
    r"\U0001F300-\U0001FAFF]"
)
_LEAD = _SPS + r"*(?:" + _ORNAMENT + _SPS + r"*){0,3}"

# --------------------------------------------------------------------------- #
# Patterns  (the public five from CONTRACT.md, plus the helpers they need)
# --------------------------------------------------------------------------- #

#: Question header in every form the corpus uses. Named groups expose *which*
#: form matched -- ``qword`` is set for the explicit forms ("Question 1.",
#: "Q1.", "Q.1", "Q 1)") and empty for a bare number ("1.", "1)"). That
#: distinction is what lets explicit headers outrank bare numbers.
QUESTION_HEADER_RE = re.compile(
    r"^" + _SPS + r"*" + _EMPH + _SPS + r"*"
    r"(?:(?P<qword>question|ques|quest|qn|mcq|que|qs|q)" + _SPS + r"*"
    r"(?P<qsep>[.\-–:·]?)" + _SPS + r"*)?"
    r"(?P<num>\d{1,3})"
    r"(?P<delim>[.)\]:–\-]|(?=" + _SPS + r")|$)"
    + _SPS + r"*" + _EMPH + _SPS + r"*"
    r"(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

#: Option label: "A." "A)" "A]" "A:" "(A)" "a." "- A." "* (b)". A-H only --
#: past H a "letter + dot" line is almost always prose, never a choice.
OPTION_RE = re.compile(
    r"^" + _SPS + r"*(?:" + _BULLET + _SPS + r"*)?" + _EMPH + _SPS + r"*"
    r"(?:\((?P<plabel>[A-Ha-h])\)|(?P<blabel>[A-Ha-h])[.)\]:])"
    r"(?P<sep>" + _SPS + r"+|$|(?=\n))"
    r"(?P<body>.*)$",
    re.DOTALL,
)

#: Several options crammed onto one line: "(A) x (B) y (C) z" (and the
#: paren-less "A) x B) y"). Only trusted when the labels run A, B, C... -- see
#: `parse_inline_options`.
INLINE_OPTIONS_RE = re.compile(
    r"(?:(?<=" + _SP + r")|^)\(?(?P<label>[A-Ha-h])\)(?=" + _SP + r"|$)",
)

#: Stated answer: "Ans. B)", "Answer: (B)", "Ans: B", "Correct answer - B",
#: "Correct Choice:", "CorrectChoice:" (one word -- a real export writes it so).
#: ``(?![A-Za-z])`` after the keyword stops "Answers are..." matching, and the
#: same guard after the label stops "Ans. Both A and B" claiming label B.
ANSWER_RE = re.compile(
    r"^" + _LEAD + _EMPH + _SPS + r"*"
    r"(?P<kw>correct" + _SPS + r"*answer|correct" + _SPS + r"*option"
    r"|correct" + _SPS + r"*choice|correct" + _SPS + r"*response"
    r"|right" + _SPS + r"*answer|right" + _SPS + r"*option"
    r"|answer" + _SPS + r"*key|key" + _SPS + r"+answer"
    r"|answer|ans)(?![A-Za-z])"
    + _SPS + r"*[.:)\-–→]*" + _SPS + r"*"
    r"(?:\((?P<plabel>[A-Ha-h])\)|(?P<label>[A-Ha-h])(?![A-Za-z]))?"
    + _SPS + r"*[.:)\-–→]*" + _SPS + r"*"
    r"(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL,
)

#: A tick (or its textual cousins) that a writer puts ON the correct option
#: instead of writing an answer line: "C. The refractory period ✅",
#: "B) Aspirin (correct)", "*D. Halothane". Trailing forms need whitespace or
#: the line start before them so "Grade A*" stays intact; the leading tick
#: form is only the glyphs, because a leading "*" is markdown emphasis.
_MARK_LEAD_RE = re.compile(r"^" + _SPS + r"*[✅✔✓☑]+" + _SPS + r"*")
_MARK_TRAIL_RE = re.compile(
    r"(?:(?<=\s)|^)(?:[✅✔✓☑]+|\*{1,2}|\(\s*(?:correct|ans|answer|"
    r"correct" + _SPS + r"*answer|right)\s*\)|[←⟵<]-*" + _SPS + r"*correct"
    r"(?:" + _SPS + r"*answer)?)" + _SPS + r"*$",
    re.IGNORECASE,
)

# Section headers that end the option region and start explanation capture.
# "Strong" names stand alone; the weaker ones need a ':' / '-' so that a stem
# beginning "Discussion of the case..." is not mistaken for a header.
_SECTION_STRONG = (
    "explanation", "explaination", "explanations",
    "correct option explanation", "correct answer explanation",
    "other options explanation", "other option explanation",
    "incorrect options explanation", "answer explanation",
    "explanation of the answer", "explanation of answer",
    "incorrect options", "incorrect option",
    "other options", "other option",
    "why other options are wrong", "why other options are incorrect",
    "take home message", "take-home message", "takehome message",
    "tags", "tag",
)
_SECTION_WEAK = (
    "educational objective", "educationak objective", "learning objective",
    "key points", "key point", "key concept", "key concepts",
    "learning point", "learning points", "clinical pearl", "clinical pearls",
    "pearls", "mnemonic", "high yield points", "high-yield points",
    "rationale", "discussion", "solution", "references", "reference",
    "high yield", "note", "notes", "hint", "hints", "tips", "tip",
)
_ALT_STRONG = "|".join(re.escape(w) for w in sorted(_SECTION_STRONG, key=len, reverse=True))
_ALT_WEAK = "|".join(re.escape(w) for w in sorted(_SECTION_WEAK, key=len, reverse=True))
SECTION_RE = re.compile(
    r"^" + _LEAD + _EMPH + _SPS + r"*"
    r"(?:(?P<strong>" + _ALT_STRONG + r")" + _SPS + r"*(?P<spunct>[:.\-–→]|$)"
    r"|(?P<weak>" + _ALT_WEAK + r")" + _SPS + r"*(?P<wpunct>[:–→]|$))",
    re.IGNORECASE,
)

#: An "Options:" / "Choices:" / "Options are ...:" line is a separator, never
#: content. "Choices:" is how one real export labels the list, and a file that
#: labels its options is also a file whose stem may carry no number at all --
#: see `_Scanner._open_unnumbered`.
_OPTIONS_WORD = r"(?:o" + _SPS + r"*ptions?|choices?|alternatives?)"
OPTIONS_HEADER_RE = re.compile(
    r"^" + _SPS + r"*" + _EMPH + _SPS + r"*" + _OPTIONS_WORD + _SPS + r"*"
    r"(?:(?:are|is)\b[^:]{0,120}:|[:\-–]?)" + _SPS + r"*" + _EMPH + _SPS + r"*$",
    re.IGNORECASE,
)
_OPTIONS_PREFIX_RE = re.compile(
    r"^" + _SPS + r"*" + _EMPH + _SPS + r"*" + _OPTIONS_WORD + _SPS + r"*"
    r"[:\-–]" + _SPS + r"*",
    re.IGNORECASE,
)

#: Bullet option with no letter label -- auto-labelled A, B, C... on finalise.
BULLET_OPTION_RE = re.compile(
    r"^" + _SPS + r"*" + _BULLET + _SPS + r"*(?P<body>.+)$", re.DOTALL
)

#: Word's web-paste artifacts. Never content, in any document.
SKIP_LINE_RE = re.compile(
    r"^" + _SPS + r"*(?:top|bottom)" + _SPS + r"+of" + _SPS + r"+form" + _SPS + r"*$",
    re.IGNORECASE,
)

#: A stem that points at a picture cannot be answered without one.
#:
#: Shared with `app.parse.reconcile` and `app.parse.ai`, so the three modules
#: cannot disagree about REFERENCES_MISSING_IMAGE. Deliberately narrower than
#: "any word that could mean a picture": "marked sensitivity to radiation" and
#: "a slide of tissue" are ordinary medical prose, and flagging them hid the
#: real warnings among false ones. "marked" therefore only counts in the forms
#: an image caption takes -- "marked A", "marked as", "muscles marked is" --
#: never before an ordinary lower-case word.
IMAGE_REFERENCE_RE = re.compile(
    r"\b(?:image|images|figure|fig\.|photograph|photomicrograph|photo|diagram|"
    r"picture|illustration|"
    r"(?:shown|given|depicted|seen|displayed|provided)" + _SPS + r"+(?:below|above|"
    r"here|in" + _SPS + r"+the" + _SPS + r"+(?:image|figure|picture|photograph|photo|"
    r"diagram|slide|x-?ray|radiograph|scan|film|ecg|ekg|graph|chart|illustration))|"
    r"following" + _SPS + r"+(?:image|figure|picture|photograph|x-?ray|radiograph|scan)|"
    r"(?:x-?ray|radiograph|scan|micrograph|slide|ecg|ekg)" + _SPS + r"+(?:shown|given|"
    r"below|above|attached)|"
    r"marked(?=" + _SPS + r"*(?:as|with|by|in|is|are|on|here|below|above|"
    r"\d|[A-Z](?![a-z])|\?|$|[,.;:)]))|"
    r"arrow|arrows|labelled|labeled)\b",
    re.IGNORECASE,
)

# A line that stops mid-thought. Split in two because the two halves need
# different guards: trailing punctuation is suspicious at any length, whereas a
# dangling connector word needs a sentence to actually dangle from. Only words
# that (almost) never end a sentence are listed: "with shoes on" and "turned
# to" are ordinary endings, and flagging them buried the real cut-offs. "a" is
# checked case-sensitively on its own: a trailing capital "A" is a label
# ("Size A", "point A", "4 -> A"), not an article left hanging.
_TRUNCATED_PUNCT_RE = re.compile(r"(?:[-–—]|\.\.\.|…)" + _SPS + r"*$")
_TRUNCATED_WORD_RE = re.compile(
    r"\b(?:of|the|an|and|or|than|which|that|due|following|is|are|was|were)\b"
    + _SPS + r"*$",
    re.IGNORECASE,
)
_TRUNCATED_ARTICLE_RE = re.compile(r"(?<=\s)a" + _SPS + r"*$")

# Bare-number headers may run this far ahead of the expected number before we
# stop believing them (renumbering gaps are common, jumps of 10 are not).
_NUMBER_LOOKAHEAD = 2
# A stem longer than this that never found options is not a question; it is a
# page of prose that a stray "1." opened. Refuse to emit it.
_MAX_ORPHAN_STEM = 1500
# How many non-blank lines after a candidate stem may pass before its answer
# choices show up. A stem with four numbered statements, a two-line lead-in and
# four unlabelled options followed by "Answer: B" is eleven lines deep.
_STRUCTURE_WINDOW = 14
# Unlabelled options ("1 and 2" / "1, 2 and 3" / "Only 1" / "All of the above")
# are recovered from the tail of a stem only within these bounds.
_UNLABELLED_MIN, _UNLABELLED_MAX, _UNLABELLED_MAX_CHARS = 2, 8, 160

#: A short line that titles a section of the file rather than saying anything:
#: "FMGE MOCK 2 / USE SAME for 29th sept NEET GT :", "SECTION B", "PART II".
#: Detached from the question it would otherwise be glued to -- see
#: `_Scanner._detach_heading_before`.
_SECTION_TITLE_WORDS_RE = re.compile(
    r"\b(?:mock|section|part|paper|test|set|chapter|topic|unit|module|gt|"
    r"neet|fmge|ini|recall|series|batch|day|round|phase|block)\b",
    re.IGNORECASE,
)
_SECTION_TITLE_MAX_CHARS = 80

# --------------------------------------------------------------------------- #
# Public line-level helpers
# --------------------------------------------------------------------------- #


class QuestionHeader(NamedTuple):
    """Result of `match_question_header`.

    `explicit` is True for "Question 5" / "Q5" / "Q.5" and False for a bare
    "5." -- the whole sub-item fix depends on telling those apart.
    """

    number: int
    explicit: bool
    keyword: str
    delim: str
    rest: str


def match_question_header(text: str) -> QuestionHeader | None:
    """Recognise a question header line, or return None.

    Accepts ``Question 1.``, ``Question 1``, ``Q1.``, ``Q1)``, ``Q.1``,
    ``Q 1``, ``1.`` and ``1)``. A bare number needs a real delimiter -- a
    trailing space is not enough, or every "5 year old boy" line would open a
    question.
    """
    m = QUESTION_HEADER_RE.match(text)
    if not m:
        return None
    keyword = (m.group("qword") or "").strip()
    explicit = bool(keyword)
    delim = m.group("delim") or ""
    rest = m.group("rest") or ""
    try:
        number = int(m.group("num"))
    except ValueError:  # pragma: no cover - \d+ cannot fail, belt and braces
        return None
    if number <= 0:
        return None

    # "1.5 mg" / "12. 05. 2024" are numbers, not headers. Only genuinely
    # ambiguous without a keyword though: "Q1. 23-year-old G2P1 woman..." is
    # unmistakably question 1 whose stem merely starts with a digit, and
    # rejecting it here silently dropped the first question of a real user file.
    # A keyword form is therefore only rejected when nothing separates the
    # delimiter from the digits ("Q1.5"), which really is one decimal number.
    if rest[:1].isdigit() and delim in ".-–":
        separator = text[m.end("delim") : m.start("rest")]
        if not (explicit and separator.strip() != separator):
            return None
    if not explicit:
        # Bare numbers are only headers with a real ". ) ]" delimiter.
        # The empty-string check matters: `"" in ".)]"` is True in Python, so
        # without it a delimiter-less line like "5 year old boy presents with..."
        # opens a phantom question 5 -- precisely what this guard exists to stop.
        if not delim or delim not in ".)]":
            return None
    else:
        # "Q10-12 are based on the following" is a range, not question 10.
        if delim and delim in "-–" and rest[:1].isdigit():
            return None
    return QuestionHeader(number, explicit, keyword, delim, rest)


def strip_question_prefix(text: str) -> tuple[int | None, str]:
    """``("1. What is X?")`` -> ``(1, "What is X?")``; ``(None, text)`` if none.

    Used for the stem's first line so the export can print its own uniform
    "Question <n>" header without the source's numbering showing up twice.
    Every character after the prefix is returned untouched.

    A header nested in a header -- ``"1.MCQ 1: Match the Following"`` is a real
    line -- is stripped twice, but only when the inner one is an *explicit*
    form ("MCQ 1:", "Q1."): a bare inner number is content ("1. 2 mg of ...").
    """
    hdr = match_question_header(text)
    if hdr is None:
        return None, text
    inner = match_question_header(hdr.rest)
    if inner is not None and inner.explicit and inner.rest.strip():
        return hdr.number, inner.rest
    return hdr.number, hdr.rest


def strip_answer_marker(text: str) -> tuple[bool, str]:
    """``("C. Aspirin ✅")`` -> ``(True, "C. Aspirin")``; ``(False, text)`` otherwise.

    A tick on the option is how some writers state the key. It is an ornament,
    not wording, so it is removed from the option text -- and the fact that it
    was there is what `_finalise` turns into the answer when no answer line
    exists. The sanctioned stripper: `app.parse.reconcile` offers the same
    marker-less form when it proves an option verbatim.
    """
    out = text
    hit = False
    m = _MARK_LEAD_RE.match(out)
    if m and m.end() < len(out):
        out = out[m.end():]
        hit = True
    m = _MARK_TRAIL_RE.search(out)
    if m and m.start() > 0 and out[:m.start()].strip():
        out = out[:m.start()].rstrip()
        hit = True
    return hit, out


def strip_option_label(text: str) -> tuple[str | None, str]:
    """``("A. Aspirin")`` -> ``("A", "Aspirin")``; ``(None, text)`` if unlabelled.

    The ONLY sanctioned label stripper -- `app.parse.ai` uses it too, so that
    an AI-anchored option and a rule-parsed one are stripped identically.

    Deliberately context-free: it strips any A-H label that is followed by
    whitespace or end-of-line. The "is this really a label?" judgement needs
    the surrounding label sequence and therefore lives in the scanner
    (`_plausible_option`). The one guard kept here is the whitespace
    requirement, which is what keeps "e.g. the patient..." intact.
    """
    m = OPTION_RE.match(text)
    if not m:
        return None, text
    label = (m.group("plabel") or m.group("blabel") or "").upper()
    if label not in OPTION_LABELS:
        return None, text
    return label, m.group("body")


#: A label written with no punctuation at all -- "A Guilt" instead of "A. Guilt".
#: Never applied to a plain line; see the single guarded call site.
_BARE_LETTER_RE = re.compile(r"^\s*([A-H])\s+(?=\S)")


def _bare_letter_label(text: str, cur: "_Draft") -> tuple[str | None, str]:
    """Read "A Guilt" as option A -- only when the sequence expects that letter.

    Deliberately hostile to guessing: it fires solely for the next label the
    option run is waiting for, so a stem line such as "A patient presents with
    chest pain" can never be mistaken for an option. Its reason to exist is a
    real user file where "Options: A Guilt" lost option A entirely while the
    document went on to state "Answer: A".
    """
    m = _BARE_LETTER_RE.match(text)
    if not m:
        return None, text
    label = m.group(1).upper()
    expected = OPTION_LABELS[len(cur.options)] if len(cur.options) < len(OPTION_LABELS) else None
    if label != expected:
        return None, text
    return label, text[m.end():]


def strip_options_prefix(text: str) -> str:
    """Remove a leading "Options:" / "Option -" header, leaving the rest verbatim.

    The sanctioned stripper for this prefix: `app.parse.reconcile` uses it too,
    so a line like ``"Options: A. Functional Hallucinations"`` is read the same
    way when parsing and when proving the result verbatim.
    """
    return _OPTIONS_PREFIX_RE.sub("", text, count=1)


def strip_bullet(text: str) -> str:
    """Remove a leading bullet glyph, leaving the rest of the line verbatim."""
    m = BULLET_OPTION_RE.match(text)
    return m.group("body") if m else text


def parse_answer_line(text: str) -> tuple[str | None, str]:
    """``("Ans. B) Aspirin")`` -> ``("B", "Aspirin")``.

    Handles "Ans. B) body", "Answer: (B) body", "Ans: B", "Correct answer - B"
    and "Ans. B". The body is returned verbatim: only the keyword, the label
    and the punctuation joining them are consumed. ``(None, text)`` when the
    line is not an answer line at all.
    """
    m = ANSWER_RE.match(text)
    if not m:
        return None, text
    raw = m.group("plabel") or m.group("label")
    label = raw.upper() if raw else None
    if label is not None and label not in OPTION_LABELS:
        label = None
    return label, m.group("body")


def parse_inline_options(text: str) -> list[tuple[str, str]]:
    """Split "(A) x (B) y (C) z" into ``[("A", "x"), ("B", "y"), ...]``.

    Ported from ``PPT_FROM_BOX_FILE.parse_options_line``, with two additions:
    a leading "Options:" is dropped, and the run must actually start at A and
    stay in sequence -- otherwise a stem that happens to contain "(b)" would
    manufacture options.
    """
    body = strip_options_prefix(text)
    marks: list[tuple[int, str, int]] = []
    for m in INLINE_OPTIONS_RE.finditer(body):
        marks.append((m.start(), m.group("label").upper(), m.end()))
    if len(marks) < 2:
        return []
    for pos, (_, label, _) in enumerate(marks):
        if label != OPTION_LABELS[pos] if pos < len(OPTION_LABELS) else True:
            return []
    out: list[tuple[str, str]] = []
    for i, (_, label, text_start) in enumerate(marks):
        text_end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        chunk = body[text_start:text_end].strip()
        if not chunk:
            return []
        out.append((label, chunk))
    return out


# --------------------------------------------------------------------------- #
# Small predicates
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return " ".join(text.split())


def _option_like(body: str) -> bool:
    """Does `body` read like an answer choice rather than a sentence?

    This is the "H. pylori causes ulcers" guard: an option body that starts
    with a lower-case word and runs on for several more words is prose in which
    the leading capital was an abbreviation, not a label.
    """
    t = _norm(body)
    if not t:
        return True  # "A." with nothing after it is still a label
    if len(t) > 220:
        return False
    words = t.split()
    if t[0].islower() and len(words) >= 3:
        return False
    return True


def _looks_truncated(text: str) -> bool:
    t = text.rstrip()
    if not t:
        return False
    if _TRUNCATED_PUNCT_RE.search(t):
        return True
    # A single word cannot be dangling. This matters for real questions: an
    # image-labelling MCQ ("which muscle is marked?") has options that are
    # literally "A", "B", "C", "D", and "A" would otherwise match the connector
    # word "a" and flag a perfectly complete option as cut off.
    if len(t.split()) < 2:
        return False
    return bool(_TRUNCATED_WORD_RE.search(t) or _TRUNCATED_ARTICLE_RE.search(t))


def _blank_between(doc: SourceDocument, a: int, b: int) -> bool:
    """True when at least one blank line sits strictly between lines `a` and `b`."""
    lo, hi = (a, b) if a < b else (b, a)
    return any(
        (ln := doc.line(i)) is not None and ln.is_blank for i in range(lo + 1, hi)
    )


def _prev_nonblank(doc: SourceDocument, i: int) -> int:
    """Index of the nearest non-blank line before `i`, or -1."""
    j = i - 1
    while j >= 0:
        ln = doc.line(j)
        if ln is not None and not ln.is_blank:
            return j
        j -= 1
    return -1


def _looks_like_section_title(text: str) -> bool:
    """A short heading such as "FMGE MOCK 2:" or "SECTION B", not a sentence.

    Three signals, any one of which is enough on a line this short: it ends
    with a colon, it is written in capitals, or it names a section. Sentence
    punctuation at the end rules it out -- a heading does not end in a full
    stop, and the last line of an explanation nearly always does.
    """
    t = text.strip()
    if not t or len(t) > _SECTION_TITLE_MAX_CHARS:
        return False
    if t[-1] in ".?!":
        return False
    if strip_option_label(t)[0] is not None or parse_answer_line(t)[0] is not None:
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    if t.endswith(":"):
        return True
    upper = sum(1 for c in letters if c.isupper())
    if upper >= 0.6 * len(letters) and len(letters) >= 4:
        return True
    return bool(_SECTION_TITLE_WORDS_RE.search(t)) and len(t.split()) <= 8


def _label_ordinal(label: str) -> int:
    up = label.strip().upper()
    return OPTION_LABELS.index(up) if up in OPTION_LABELS else -1


# --------------------------------------------------------------------------- #
# Draft containers  (mutable while scanning; converted to Question on finalise)
# --------------------------------------------------------------------------- #

_REGION_STEM = "stem"
_REGION_OPTIONS = "options"
_REGION_EXPLANATION = "explanation"


@dataclass(slots=True)
class _OptDraft:
    label: str
    lines: list[int] = field(default_factory=list)
    #: Set only for options that share one source line ("(A) x (B) y"), where
    #: text cannot be a whole-line slice.
    inline_text: str | None = None


@dataclass(slots=True)
class _Draft:
    number: int | None = None
    explicit: bool = False
    header_line: int = -1
    first_line: int = -1
    last_line: int = -1
    stem_lines: list[int] = field(default_factory=list)
    options: list[_OptDraft] = field(default_factory=list)
    bullets: list[int] = field(default_factory=list)
    answer_label: str | None = None
    answer_text: str = ""
    answer_lines: list[int] = field(default_factory=list)
    explanation_lines: list[int] = field(default_factory=list)
    flags: set[Flag] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    region: str = _REGION_STEM
    saw_options_header: bool = False
    pending_answer: bool = False
    inline_options: bool = False
    #: The source printed no number for this question at all (it was opened on
    #: a "Choices:" header or an option run) -- see `_Scanner._open_unnumbered`.
    unnumbered: bool = False
    #: Labels of options that carried a tick / "(correct)" marker in the source.
    marked_labels: list[str] = field(default_factory=list)

    def touch(self, idx: int) -> None:
        if self.first_line < 0:
            self.first_line = idx
        self.last_line = max(self.last_line, idx)

    @property
    def n_options(self) -> int:
        return len(self.options)

    @property
    def has_answer(self) -> bool:
        return self.answer_label is not None or bool(self.answer_text)


# --------------------------------------------------------------------------- #
# The scanner
# --------------------------------------------------------------------------- #


class _Scanner:
    """Single-pass state machine over a list of line indices.

    Two things decide where a question starts, and they are weighed together:

    * **Numbering.** An explicit header ("Question 5", "Q5", "MCQ 5") always
      opens a question. A bare "5." opens one when it is the number the
      document is expected to reach next.
    * **Structure.** A line -- numbered or not -- followed by answer choices (or
      by an "Options:" header, or by an answer line) is a question stem, no
      matter what number it carries. This is what survives the chaotic files:
      numbering that restarts at 1 for a second section, a "2." that comes
      after a "5.", a stem with no number at all above a "Choices:" line.

    Numbering alone is not trusted inside an explanation, because that is
    where the phantoms live: an explanation that re-lists "1. ... 2. ... 3. ...
    4. A history of amaurosis fugax" hands the parser a "4." exactly when it
    is expecting question 4. Only the structure test can tell that line from
    the real question 4 further down -- and it does, because no options follow.

    The pre-pass that finds explicit headers still matters: in a document that
    numbers its questions "Q1 / Q2 / Q3", a bare number with nothing after it
    is never a header.
    """

    def __init__(self, doc: SourceDocument) -> None:
        self.doc = doc
        self.explicit_numbers: set[int] = set()
        self.has_explicit = False

    # -- pre-pass ---------------------------------------------------------- #

    def survey(self, indices: Sequence[int]) -> None:
        """Record which question numbers appear as *explicit* headers."""
        for i in indices:
            ln = self.doc.line(i)
            if ln is None or ln.is_blank:
                continue
            hdr = match_question_header(ln.stripped)
            if hdr is not None and hdr.explicit:
                self.explicit_numbers.add(hdr.number)
        self.has_explicit = bool(self.explicit_numbers)

    # -- structural lookahead --------------------------------------------- #

    def _structure_follows(
        self, indices: Sequence[int], pos: int, number: int | None
    ) -> bool:
        """Do answer choices follow the candidate stem at `indices[pos]`?

        Scans forward through at most `_STRUCTURE_WINDOW` non-blank lines and
        answers True on the first sign of a question's body: two options in
        sequence, an "Options:" / "Choices:" header, or a stated answer line
        (which is how a question with *unlabelled* options announces itself).

        It answers False -- "this line is not a stem" -- on anything that ends
        a question first: a section header, an explicit question header, or a
        bare number one higher than the candidate's own, which means the
        candidate was an item in a numbered list, not a question.
        """
        seen = 0
        want = 0
        scanned = 0
        in_sublist = False
        for i in indices[pos + 1:]:
            ln = self.doc.line(i)
            if ln is None or ln.is_blank:
                continue
            s = ln.stripped
            if SKIP_LINE_RE.match(s):
                continue
            scanned += 1
            if scanned > _STRUCTURE_WINDOW:
                return False
            if OPTIONS_HEADER_RE.match(s):
                return True
            if SECTION_RE.match(s):
                return False
            hdr = match_question_header(s)
            if hdr is not None:
                if hdr.explicit:
                    return False
                if number is not None and hdr.number == number + 1 and not in_sublist:
                    return False       # "1. foo" then "2. bar": a list, not a stem
                # A number that is not ours + 1 opens a sub-list under the
                # stem ("3." then "1) ... 6)"), and once inside one, a later
                # "4." is that list's fourth item, not our sibling.
                in_sublist = True
                continue
            label, body = parse_answer_line(s)
            if label is not None or body != s:
                return True
            olabel, _ = strip_option_label(ln.text)
            if olabel is None and not seen:
                olabel, _ = _bare_letter_after_options(ln.text)
            if olabel is None:
                if len(parse_inline_options(s)) >= 2:
                    return True
                continue
            ordinal = _label_ordinal(olabel)
            if ordinal == want:
                seen += 1
                want += 1
                if seen >= 2:
                    return True
            elif ordinal == 0:
                seen, want = 1, 1
        return False

    def _option_run_with_answer(
        self, indices: Sequence[int], pos: int
    ) -> bool:
        """Does an A, B, ... run starting at `indices[pos]` end in an answer line?

        The test that separates a new question with no printed number from an
        explanation's own "Other Options: A. ... B. ... D. ..." re-listing: the
        re-listing is followed by more prose or a heading, the question by its
        "Correct Answer" line.
        """
        want = 0
        scanned = 0
        for i in indices[pos:]:
            ln = self.doc.line(i)
            if ln is None or ln.is_blank:
                continue
            s = ln.stripped
            scanned += 1
            if scanned > _STRUCTURE_WINDOW + 4:
                return False
            label, _ = strip_option_label(ln.text)
            if label is not None and _label_ordinal(label) == want:
                want += 1
                continue
            if want < 2:
                return False
            alabel, body = parse_answer_line(s)
            if alabel is not None or body != s:
                return True
            if SECTION_RE.match(s) or match_question_header(s) is not None:
                return False
            if label is not None:
                return False           # the run restarted: not our run
            # a wrapped continuation of the last option: keep looking
        return False

    # -- start decision ---------------------------------------------------- #

    def _header_opens_question(
        self,
        hdr: QuestionHeader,
        cur: _Draft | None,
        expected_next: int,
        indices: Sequence[int],
        pos: int,
    ) -> bool:
        """The bare-``N.`` decision: numbering and structure weighed together.

        Explicit headers always win. For a bare number, "structure follows"
        (see `_structure_follows`) is sufficient on its own -- *except* inside
        a stem that has not reached its options yet, where a numbered line is
        presumed to be a sub-item unless it is both the expected next number
        and followed by choices, and not itself the continuation of a list.
        Numbering on its own is sufficient only outside an explanation.
        """
        if hdr.explicit:
            return True
        i = indices[pos]
        in_sequence = hdr.number == expected_next or (
            not self.has_explicit
            and expected_next <= hdr.number <= expected_next + _NUMBER_LOOKAHEAD
        )
        if self.has_explicit and hdr.number in self.explicit_numbers:
            in_sequence = False        # a bare copy of an explicit number
        structural = self._structure_follows(indices, pos, hdr.number)

        if cur is not None and cur.n_options < 2 and cur.region == _REGION_STEM:
            # The sub-item trap: "3. What is the order..." then "1. Ligamentum".
            if not (in_sequence and structural):
                return False
            prev = _prev_nonblank(self.doc, i)
            if prev >= 0 and prev != cur.header_line:
                prev_hdr = match_question_header(self.doc.lines[prev].stripped)
                if (
                    prev_hdr is not None
                    and not prev_hdr.explicit
                    and prev_hdr.number == hdr.number - 1
                ):
                    return False       # "3. baz" then "4. qux": still the list
            return True
        if structural:
            return True
        if cur is None:
            return in_sequence
        if cur.region == _REGION_EXPLANATION:
            return False               # numbering alone proves nothing here
        return in_sequence

    # -- unnumbered questions --------------------------------------------- #

    def _open_unnumbered(
        self, cur: _Draft | None, indices: Sequence[int], pos: int, s: str
    ) -> bool:
        """Should a question with no printed number open at `indices[pos]`?

        Two shapes, both from real files: a stem paragraph, a blank line, then
        "Choices:" and the options; or -- when the stem sat in a table the
        reader could not flatten -- the option run alone, ending in its answer
        line. Only asked when nothing is open or the open question is already
        complete (it has its options and is into its explanation).
        """
        if cur is not None and not (
            cur.region == _REGION_EXPLANATION and cur.n_options >= 2
        ):
            return False
        if OPTIONS_HEADER_RE.match(s):
            return True
        label, _ = strip_option_label(self.doc.lines[indices[pos]].text)
        if label is None or _label_ordinal(label) != 0:
            return False
        return self._option_run_with_answer(indices, pos)

    def _preceding_block(
        self, cur: _Draft | None, i: int, *, skip_blanks: bool
    ) -> list[int]:
        """The stem lines of an unnumbered question opening at line `i`.

        Walks back over the non-blank lines above `i` and stops at a blank
        line, at anything structural (an option, an answer, a section heading,
        a question header) and at any line the open question owns other than
        the tail of its explanation -- which is the only place a stem for the
        *next* question can have been filed by mistake.

        `skip_blanks` allows the gap that a "Choices:" header often has above
        it. It is refused for a bare option run inside an explanation: there,
        the paragraph above the gap is the previous question's closing text,
        and taking it would move an explanation into the next stem. A block
        that turns out to sit directly under a section heading is that
        section's body, not a stem, and is not taken either.
        """
        reclaimable: set[int] = set()
        if cur is not None:
            reclaimable.update(cur.explanation_lines)
            if cur.options:
                reclaimable.update(cur.options[-1].lines[1:])
        owned_elsewhere: set[int] = set()
        if cur is not None:
            owned_elsewhere.update(cur.stem_lines)
            owned_elsewhere.update(cur.answer_lines)
            for od in cur.options:
                owned_elsewhere.update(od.lines[:1])
        block: list[int] = []
        under_heading = False
        j = i - 1
        while j >= 0:
            ln = self.doc.line(j)
            if ln is None:
                break
            if ln.is_blank:
                if skip_blanks and not block:
                    j -= 1
                    continue
                break
            s = ln.stripped
            if SKIP_LINE_RE.match(s):
                j -= 1
                continue
            if j in owned_elsewhere:
                break
            if cur is not None and j not in reclaimable:
                break
            if (
                strip_option_label(ln.text)[0] is not None
                or parse_answer_line(s)[0] is not None
                or OPTIONS_HEADER_RE.match(s)
                or match_question_header(s) is not None
            ):
                break
            if SECTION_RE.match(s):
                under_heading = True
                break
            block.append(j)
            j -= 1
        if under_heading and cur is not None:
            return []
        block.reverse()
        return block

    @staticmethod
    def _reclaim(cur: _Draft | None, lines: Sequence[int]) -> None:
        """Take `lines` away from whatever part of `cur` was holding them."""
        if cur is None or not lines:
            return
        drop = set(lines)
        cur.explanation_lines = [x for x in cur.explanation_lines if x not in drop]
        if cur.options:
            cur.options[-1].lines = [x for x in cur.options[-1].lines if x not in drop]

    # -- section titles ---------------------------------------------------- #

    def _detach_heading_before(self, cur: _Draft | None, i: int) -> None:
        """Unglue a section title from the question above it.

        "FMGE MOCK 2 / USE SAME for 29th sept NEET GT :" sits between the last
        line of question 5 and the "1." that restarts the numbering. By the
        time the restart is recognised, that title has already been filed as
        the tail of question 5 -- and printed as part of its explanation, or
        worse, as a wrapped continuation of option D. So when a question opens,
        the line directly above it is looked at once more: a short title-like
        line, set off from the text above it by a blank line, is released.
        """
        if cur is None:
            return
        j = _prev_nonblank(self.doc, i)
        if j < 0 or not _looks_like_section_title(self.doc.lines[j].text):
            return
        prev = _prev_nonblank(self.doc, j)
        if prev >= 0 and not _blank_between(self.doc, prev, j):
            return
        if cur.explanation_lines and cur.explanation_lines[-1] == j:
            cur.explanation_lines.pop()
        elif cur.options and len(cur.options[-1].lines) > 1 and cur.options[-1].lines[-1] == j:
            cur.options[-1].lines.pop()
        elif not cur.options and len(cur.stem_lines) > 1 and cur.stem_lines[-1] == j:
            cur.stem_lines.pop()
        else:
            return
        cur.notes.append("a section title above the next question was left out")

    # -- option decision --------------------------------------------------- #

    def _plausible_option(
        self,
        label: str,
        body: str,
        cur: _Draft,
        indices: Sequence[int],
        pos: int,
    ) -> bool:
        """Is this letter really an option label for the open question?

        The sequence check is the load-bearing part -- it is what stops
        "H. pylori causes ulcers" from becoming option H while still allowing
        every legitimate A/B/C/D line through.
        """
        ordinal = _label_ordinal(label)
        if ordinal < 0:
            return False
        n = cur.n_options
        if n == 0:
            if ordinal == 0:
                return True
            # Options that do not start at A are only believable when the run
            # visibly continues (B, C, D...) or the writer announced them.
            if not _option_like(body):
                return False
            if cur.saw_options_header:
                return True
            return self._forward_run(indices, pos, ordinal) >= 3
        if ordinal == n:
            return True  # the sequence simply continues
        if ordinal == 0:
            return True  # restart of the run -- the caller re-homes the old one
        if ordinal < n or ordinal <= n + _NUMBER_LOOKAHEAD:
            return _option_like(body)  # duplicate or a small gap (A, B, D)
        return False

    def _forward_run(self, indices: Sequence[int], pos: int, ordinal: int) -> int:
        """Length of the label run starting at `pos` (this line included)."""
        run = 1
        want = ordinal + 1
        for i in indices[pos + 1:]:
            ln = self.doc.line(i)
            if ln is None or ln.is_blank:
                continue
            label, _ = strip_option_label(ln.text)
            if label is None or _label_ordinal(label) != want:
                break
            run += 1
            want += 1
        return run

    # -- main pass --------------------------------------------------------- #

    def run(
        self,
        indices: Sequence[int],
        *,
        expected_next: int = 1,
        single: bool = False,
        forced_number: int | None = None,
    ) -> tuple[list[_Draft], int]:
        """Scan `indices` and return the drafts plus the next expected number.

        `single=True` restricts the scan to one question (used for a box table,
        where the whole table is one question by construction).
        """
        doc = self.doc
        drafts: list[_Draft] = []
        cur: _Draft | None = None
        opened = False

        def finish() -> None:
            nonlocal cur
            if cur is not None:
                drafts.append(cur)
                cur = None

        for pos, i in enumerate(indices):
            ln = doc.line(i)
            if ln is None:
                continue
            raw = ln.text
            s = ln.stripped
            if not s:
                continue
            if SKIP_LINE_RE.match(s):
                continue

            hdr = match_question_header(s)

            # -- new question? ------------------------------------------- #
            if hdr is not None:
                if single:
                    open_it = not opened
                    if open_it and forced_number is not None:
                        hdr = hdr._replace(number=forced_number)
                else:
                    open_it = self._header_opens_question(
                        hdr, cur, expected_next, indices, pos
                    )
                if open_it:
                    self._detach_heading_before(cur, i)
                    finish()
                    cur = _Draft(
                        number=hdr.number,
                        explicit=hdr.explicit,
                        header_line=i,
                    )
                    cur.touch(i)
                    opened = True
                    expected_next = hdr.number + 1
                    if hdr.rest.strip():
                        cur.stem_lines.append(i)
                    continue
                if (
                    cur is not None
                    and not hdr.explicit
                    and cur.region == _REGION_STEM
                ):
                    # The classic false positive: a numbered list inside a stem.
                    cur.flags.add(Flag.NUMBERED_SUBITEMS)

            # -- a question the source never numbered ---------------------- #
            if not single and self._open_unnumbered(cur, indices, pos, s):
                block = self._preceding_block(
                    cur, i, skip_blanks=cur is None or bool(OPTIONS_HEADER_RE.match(s))
                )
                self._reclaim(cur, block)
                if cur is None or block or not OPTIONS_HEADER_RE.match(s):
                    self._detach_heading_before(cur, block[0] if block else i)
                    finish()
                    cur = _Draft(number=None, explicit=False, unnumbered=True)
                    for j in block:
                        cur.touch(j)
                        cur.stem_lines.append(j)
                    cur.touch(i)
                    opened = True
                    cur.notes.append("the source printed no number for this question")
                    if OPTIONS_HEADER_RE.match(s):
                        cur.saw_options_header = True
                        cur.region = _REGION_OPTIONS
                        continue
                    # fall through: this line is option A of the new question

            if cur is None:
                continue
            cur.touch(i)

            # -- an answer we were promised on the previous line ---------- #
            if cur.pending_answer:
                cur.pending_answer = False
                if self._absorb_answer_body(cur, i, s):
                    continue

            # -- section header ends the option region ------------------- #
            if SECTION_RE.match(s):
                cur.region = _REGION_EXPLANATION
                cur.explanation_lines.append(i)
                continue

            # -- stated answer ------------------------------------------- #
            if self._try_answer(cur, i, s):
                continue

            # -- "Options:" separator ------------------------------------ #
            if OPTIONS_HEADER_RE.match(s):
                cur.saw_options_header = True
                if cur.region == _REGION_STEM:
                    cur.region = _REGION_OPTIONS
                continue

            # -- options -------------------------------------------------- #
            if cur.region != _REGION_EXPLANATION and self._try_options(
                cur, i, raw, s, indices, pos
            ):
                continue

            # -- unlabelled / bullet options after an "Options:" header --- #
            if (
                cur.saw_options_header
                and not cur.options
                and cur.region == _REGION_OPTIONS
            ):
                cur.bullets.append(i)
                continue

            # -- everything else: continuation --------------------------- #
            if cur.region == _REGION_EXPLANATION:
                cur.explanation_lines.append(i)
            elif cur.region == _REGION_OPTIONS and cur.options and not cur.inline_options:
                # A non-label line while options are open continues the last
                # one (wrapped paragraphs in Word do this constantly).
                cur.options[-1].lines.append(i)
            elif cur.region == _REGION_OPTIONS and cur.inline_options:
                # Options came packed onto ONE line, so each is already complete
                # and nothing can be a wrapped continuation of the last. Claiming
                # this line for option D would attach source provenance the
                # option's text never came from, which verify_question would then
                # -- correctly -- report as a verbatim mismatch.
                cur.explanation_lines.append(i)
            elif cur.region == _REGION_OPTIONS and cur.bullets:
                cur.bullets.append(i)
            else:
                cur.stem_lines.append(i)

        finish()
        return drafts, expected_next

    # -- helpers used by run() --------------------------------------------- #

    def _try_answer(self, cur: _Draft, i: int, s: str) -> bool:
        """Record a stated answer. Returns True when the line was consumed."""
        label, body = parse_answer_line(s)
        if label is None and body == s:
            return False  # not an answer line at all
        if label is None:
            stripped = body.strip()
            if not stripped:
                # "Answer:" alone -- the label is on the next line.
                cur.pending_answer = True
                cur.answer_lines.append(i)
                cur.region = _REGION_EXPLANATION
                return True
            label = self._label_from_text(cur, stripped)
            if label is None and not cur.options and not cur.has_answer:
                # "Answer - 1 2- 3- 456" under options that carry no letters:
                # the text is kept, and `_finalise` matches it once the
                # unlabelled options have been recovered from the stem.
                cur.answer_text = stripped
                cur.answer_lines.append(i)
                cur.region = _REGION_EXPLANATION
                return True
            if label is None:
                # Answer-ish prose ("Answer depends on..."); keep it as
                # explanation rather than polluting the stem.
                cur.region = _REGION_EXPLANATION
                cur.explanation_lines.append(i)
                return True
        if cur.answer_label is None:
            cur.answer_label = label
            cur.answer_text = body.strip()
            cur.answer_lines.append(i)
        elif cur.answer_label == label:
            # The 5-row box format repeats the answer row verbatim; ignore it.
            if i not in cur.answer_lines:
                cur.answer_lines.append(i)
        else:
            cur.notes.append(
                f"second answer line disagreed (kept {cur.answer_label}, "
                f"saw {label})"
            )
            cur.explanation_lines.append(i)
        cur.region = _REGION_EXPLANATION
        return True

    def _absorb_answer_body(self, cur: _Draft, i: int, s: str) -> bool:
        """Resolve a label for a bare "Answer:" whose value is on the next line.

        Never overwrites a label the question already has: the first "Correct
        Answer" of a question is its key, and a second one that disagrees is
        far more likely to belong to a question the scanner failed to split off
        than to be a correction.
        """
        label, body = strip_option_label(s)
        if label is None:
            label = self._label_from_text(cur, s)
            body = s
        if label is None:
            if not cur.options and not cur.has_answer:
                cur.answer_text = s.strip()
                cur.answer_lines.append(i)
                return True
            return False
        if cur.answer_label is not None and cur.answer_label != label:
            cur.notes.append(
                f"second answer line disagreed (kept {cur.answer_label}, saw {label})"
            )
            cur.explanation_lines.append(i)
            return True
        cur.answer_label = label
        cur.answer_text = body.strip()
        cur.answer_lines.append(i)
        return True

    def _label_from_text(self, cur: _Draft, text: str) -> str | None:
        """Match an answer body against the option texts to recover its label."""
        want = _norm(text).casefold()
        if len(want) < 3:
            return None
        for od in cur.options:
            have = _norm(self._option_text(od)).casefold()
            if not have or len(have) < 3:
                continue
            if have == want or want.startswith(have) or have.startswith(want):
                return od.label
        return None

    def _option_text(self, od: _OptDraft) -> str:
        if od.inline_text is not None:
            return od.inline_text
        return _option_draft_text(self.doc, od)

    def _try_options(
        self,
        cur: _Draft,
        i: int,
        raw: str,
        s: str,
        indices: Sequence[int],
        pos: int,
    ) -> bool:
        """Consume one options line (per-paragraph or several inline)."""
        # Several options on one line: "(A) x (B) y (C) z".
        inline = parse_inline_options(s)
        if len(inline) >= 2 and not cur.options:
            for label, text in inline:
                cur.options.append(
                    _OptDraft(label=label, lines=[i], inline_text=text)
                )
            cur.inline_options = True
            cur.region = _REGION_OPTIONS
            return True

        label, body = strip_option_label(raw)
        if label is None:
            # "Options: A. Functional Hallucinations" -- the section header and
            # the first option share a line. Without this branch option A is
            # swallowed by the stem and a stated "Answer: A" then points at an
            # option that does not exist: 30 of the 60 questions in a real user
            # file were affected. inline_text keeps the stored text a verbatim
            # substring of the line rather than the whole line.
            after = strip_options_prefix(raw)
            if after != raw:
                plabel, pbody = strip_option_label(after)
                if plabel is None:
                    # "Options: A Guilt" -- the letter carries no punctuation at
                    # all. Only safe to read as a label here, behind the
                    # "Options:" header and only for the label the sequence
                    # expects, because "A patient presents..." would otherwise
                    # become option A everywhere.
                    plabel, pbody = _bare_letter_label(after, cur)
                if plabel is not None and self._plausible_option(
                    plabel, pbody, cur, indices, pos
                ):
                    cur.options.append(
                        _OptDraft(label=plabel, lines=[i], inline_text=pbody.strip())
                    )
                    cur.region = _REGION_OPTIONS
                    return True
            return False
        if not self._plausible_option(label, body, cur, indices, pos):
            return False

        ordinal = _label_ordinal(label)
        if ordinal == 0 and cur.n_options >= 2:
            # A second run starting at A: the first run was not a list of
            # choices but part of the stem (a match-the-columns grid, say).
            moved = [x for od in cur.options for x in od.lines]
            cur.options.clear()
            cur.stem_lines.extend(moved)
            cur.stem_lines.sort()
            cur.notes.append(
                "option labels restarted at A; the earlier run was kept as stem"
            )
        cur.options.append(_OptDraft(label=label, lines=[i]))
        cur.region = _REGION_OPTIONS
        return True


def _bare_letter_after_options(text: str) -> tuple[str | None, str]:
    """"Options: A Guilt" read as option A -- for the lookahead only.

    The scanner's own `_bare_letter_label` needs the open draft to know which
    letter is expected; the lookahead has no draft yet, so it accepts only the
    first label of a run, behind an "Options:" prefix.
    """
    after = strip_options_prefix(text)
    if after == text:
        return None, text
    label, body = strip_option_label(after)
    if label is not None:
        return label, body
    m = _BARE_LETTER_RE.match(after)
    if m and m.group(1).upper() == OPTION_LABELS[0]:
        return OPTION_LABELS[0], after[m.end():]
    return None, text


# --------------------------------------------------------------------------- #
# Box tables  (the most reliable shape in the corpus -- tried first)
# --------------------------------------------------------------------------- #

_REF_CELL_RE = re.compile(r"^tbl:(?P<t>\d+)/r:(?P<r>\d+)/c:(?P<c>\d+)")


def _cell_table_key(doc: SourceDocument, idx: int) -> tuple[int, int] | None:
    """A stable (table, column) key for a CELL line, or None if unknowable."""
    ln = doc.line(idx)
    if ln is None or ln.kind is not LineKind.CELL:
        return None
    if ln.ref:
        m = _REF_CELL_RE.match(ln.ref)
        if m:
            return int(m.group("t")), int(m.group("c"))
    return ln.block_id, 0


def _cell_blocks(doc: SourceDocument) -> list[list[int]]:
    """Group consecutive CELL lines into one block per source table.

    Blank body paragraphs between tables break a block, and the ``ref`` /
    ``block_id`` key breaks it when two tables are adjacent.
    """
    blocks: list[list[int]] = []
    cur: list[int] = []
    cur_key: tuple[int, int] | None = None
    for ln in doc.lines:
        key = _cell_table_key(doc, ln.idx)
        if key is None:
            if ln.is_blank:
                continue  # a blank line does not by itself end a table
            if cur:
                blocks.append(cur)
                cur, cur_key = [], None
            continue
        if cur and key != cur_key:
            blocks.append(cur)
            cur = []
        cur_key = key
        cur.append(ln.idx)
    if cur:
        blocks.append(cur)
    return blocks


def _sequential_option_count(doc: SourceDocument, indices: Iterable[int]) -> int:
    """How many options a block holds, counted as a strict A, B, C... run."""
    want = 0
    for i in indices:
        ln = doc.line(i)
        if ln is None or ln.is_blank:
            continue
        label, _ = strip_option_label(ln.text)
        if label is not None and _label_ordinal(label) == want:
            want += 1
    return want


def _parse_box_blocks(
    doc: SourceDocument, scanner: _Scanner
) -> tuple[list[_Draft], set[int]]:
    """Parse question-box tables directly; return their drafts and lines.

    A block qualifies when its first non-blank line is an explicit question
    header and it holds at least two sequential options -- i.e. the
    ``Q.1 / stem / A.-D. / Ans. X)`` shape from the real box files and the
    labelled ``Question 1 / Question: / Options: / Answer:`` variant. Anything
    else is left to the flat scan, which is the safer default.
    """
    drafts: list[_Draft] = []
    consumed: set[int] = set()
    for block in _cell_blocks(doc):
        first = next(
            (i for i in block if (ln := doc.line(i)) is not None and not ln.is_blank),
            None,
        )
        if first is None:
            continue
        head = doc.lines[first].stripped
        hdr = match_question_header(head)
        if hdr is None or not hdr.explicit:
            continue
        if _sequential_option_count(doc, block) < 2:
            continue
        sub, _ = scanner.run(
            block, expected_next=hdr.number, single=True, forced_number=hdr.number
        )
        if not sub:
            continue
        d = sub[0]
        if d.n_options < 2:
            continue  # do not consume lines we failed to make sense of
        d.notes.append("parsed from a single-column question-box table")
        drafts.append(d)
        consumed.update(block)
    return drafts, consumed


# --------------------------------------------------------------------------- #
# Draft -> Question
# --------------------------------------------------------------------------- #


def _option_draft_text(doc: SourceDocument, od: _OptDraft) -> str:
    if od.inline_text is not None:
        return od.inline_text
    if not od.lines:
        return ""
    _, body = strip_option_label(doc.text_of(od.lines[:1]))
    tail = [doc.line(i).text for i in od.lines[1:] if doc.line(i) is not None]
    _, clean = strip_answer_marker("\n".join([body, *tail]).strip())
    return clean.strip()


def _stem_text(doc: SourceDocument, d: _Draft) -> str:
    parts: list[str] = []
    for k, i in enumerate(d.stem_lines):
        ln = doc.line(i)
        if ln is None:
            continue
        text = ln.text
        if k == 0 and i == d.header_line:
            # Drop the source's own numbering: the export writes its own.
            _, text = strip_question_prefix(text)
        parts.append(text)
    return "\n".join(parts).strip()


def _unlabelled_candidate(doc: SourceDocument, i: int) -> bool:
    """Could line `i` be one of a run of options that carry no letters?"""
    ln = doc.line(i)
    if ln is None or ln.is_blank:
        return False
    s = ln.stripped
    if len(s) > _UNLABELLED_MAX_CHARS:
        return False
    if match_question_header(s) is not None:      # "1) Exposure to ..." is a statement
        return False
    if SECTION_RE.match(s) or OPTIONS_HEADER_RE.match(s):
        return False
    if parse_answer_line(s)[0] is not None:
        return False
    return True


def _recover_unlabelled_options(doc: SourceDocument, d: _Draft) -> None:
    """Options that carry no letters at all: "1 and 2 / 1,2 and 3 / Only 1 /
    All of the above" straight after the statements, then "Answer : B".

    Every such line has been filed as stem, because nothing marked it as a
    choice. What does mark the run is what surrounds it: an answer line (or an
    "Options:" header) below, and a stem above -- a numbered statement list, a
    blank line, or simply the question sentence. The run is the maximal tail
    of the stem made of short, unnumbered, unstructured lines, cut at the first
    blank line once it holds two candidates. Two to eight of them become
    options A, B, C ... in printed order.

    Deliberately conservative: a question with any labelled option, or without
    an answer line or "Options:" header, is left exactly as it was. The result
    always carries LOW_CONFIDENCE, because the letters are ours, not the
    source's.
    """
    if d.options or d.bullets or d.inline_options:
        return
    if not (d.answer_lines or d.saw_options_header):
        return
    run: list[int] = []
    for i in reversed(d.stem_lines):
        if i == d.header_line and not d.unnumbered:
            break                                   # the question sentence itself
        if not _unlabelled_candidate(doc, i):
            break
        if run and _blank_between(doc, i, run[-1]) and len(run) >= _UNLABELLED_MIN:
            break
        if len(run) >= _UNLABELLED_MAX:
            break
        run.append(i)
    run.reverse()
    if len(run) < _UNLABELLED_MIN or len(run) >= len(d.stem_lines):
        return                                      # a stem must remain
    if d.answer_label is not None and _label_ordinal(d.answer_label) >= len(run):
        return                                      # "Answer: D" over three lines
    d.stem_lines = [i for i in d.stem_lines if i not in set(run)]
    for pos, i in enumerate(run):
        d.options.append(_OptDraft(label=OPTION_LABELS[pos], lines=[i], inline_text=None))
    d.flags.add(Flag.LOW_CONFIDENCE)
    d.notes.append(
        f"options carried no letters in the source; labelled A-{OPTION_LABELS[len(run) - 1]} "
        "in printed order"
    )


def _build_options(doc: SourceDocument, d: _Draft) -> list[Option]:
    if d.options:
        out = []
        for od in d.options:
            if od.inline_text is None and od.lines:
                first = doc.line(od.lines[0])
                if first is not None:
                    label, body = strip_option_label(first.text)
                    marked, _ = strip_answer_marker(body if label else first.text)
                    if marked:
                        d.marked_labels.append(od.label.upper())
            out.append(
                Option(
                    label=od.label.upper(),
                    text=_option_draft_text(doc, od),
                    source_lines=list(od.lines),
                )
            )
        return out
    # Bullet-only options: auto-label A, B, C... exactly as
    # PPT_FROM_NORMAL_FILE did, but only after an explicit "Options:" header so
    # that a page of bulleted revision notes never turns into choices.
    out: list[Option] = []
    for pos, i in enumerate(d.bullets[: len(OPTION_LABELS)]):
        ln = doc.line(i)
        if ln is None:
            continue
        marked, text = strip_answer_marker(strip_bullet(ln.text).strip())
        if marked:
            d.marked_labels.append(OPTION_LABELS[pos])
        out.append(
            Option(
                label=OPTION_LABELS[pos],
                text=text.strip(),
                source_lines=[i],
            )
        )
    if out:
        d.notes.append("bullet options auto-labelled A-" + out[-1].label)
    return out


def _build_answer(doc: SourceDocument, d: _Draft, options: list[Option]) -> Answer:
    label = d.answer_label
    text = d.answer_text
    if label is None and text:
        # "Answer - 1 2- 3- 456": a body with no letter. Now that the options
        # exist, the letter is whichever option says the same thing.
        want = _norm(text).casefold()
        for o in options:
            have = _norm(o.text).casefold()
            if have and len(have) >= 3 and (
                have == want or want.startswith(have) or have.startswith(want)
            ):
                label = o.label
                break
    if label is None and not text and len(d.marked_labels) == 1:
        # No answer line, but exactly one option carried a tick.
        label = d.marked_labels[0]
        match = next((o for o in options if o.label == label), None)
        if match is not None:
            d.notes.append(f"answer {label} taken from a tick mark on the option")
            return Answer(
                label=label,
                text=match.text,
                origin=AnswerOrigin.SOURCE,
                confidence=1.0,
                source_lines=list(match.source_lines),
            )
        label = None
    if label is None and not text:
        return Answer()
    if label is not None and not text:
        match = next((o for o in options if o.label == label), None)
        if match is not None:
            text = match.text  # copied from the option, never rewritten
    if label is None:
        return Answer(
            label=None,
            text=text,
            origin=AnswerOrigin.NONE,
            confidence=0.0,
            source_lines=list(d.answer_lines),
        )
    return Answer(
        label=label,
        text=text,
        origin=AnswerOrigin.SOURCE,
        confidence=1.0,
        source_lines=list(d.answer_lines),
    )


def _letter_only_options(q: Question) -> bool:
    """Options that are just "A", "B", "C", "D" label parts of a picture.

    Letters only -- numeric options ("3", "6", "10") are answers to a sum.
    """
    if len(q.options) < 2:
        return False
    return all(
        len(o.text.strip()) == 1 and o.text.strip().isalpha() for o in q.options
    )


def _references_missing_image(q: Question) -> bool:
    """The stem points at a picture and the question carries none.

    A "shown below" with a grid attached is the grid, not a missing picture.
    """
    if q.images:
        return False
    if _letter_only_options(q):
        return True
    m = IMAGE_REFERENCE_RE.search(q.stem)
    if m is None:
        return False
    phrase = " ".join(m.group(0).lower().split())
    if q.tables and phrase in {"shown below", "shown above", "given below", "given above"}:
        return False
    return True


def _apply_flags(q: Question, d: _Draft) -> None:
    for f in sorted(d.flags, key=lambda x: x.value):
        q.flag(f)
    n = len(q.options)
    if n < 2:
        q.flag(Flag.FEW_OPTIONS)
    if n > 8:
        q.flag(Flag.MANY_OPTIONS)
    if q.options and not labels_are_sequential(q.option_labels):
        q.flag(Flag.NON_SEQUENTIAL_LABELS)
    if not q.answer_matches_an_option:
        q.flag(Flag.ANSWER_NOT_IN_OPTIONS)
    seen: set[str] = set()
    for o in q.options:
        key = o.normalised()
        if key and key in seen:
            q.flag(Flag.DUPLICATE_OPTIONS)
            break
        seen.add(key)
    if not q.stem.strip():
        q.flag(Flag.EMPTY_STEM)
    if not q.answer.is_known:
        q.flag(Flag.NO_ANSWER)
    if _looks_truncated(q.stem) or any(
        _looks_truncated(o.text) or not o.text.strip() for o in q.options
    ):
        q.flag(Flag.TRUNCATED)
    if _references_missing_image(q):
        # An answer cannot honestly be derived for a picture we do not have.
        q.flag(Flag.REFERENCES_MISSING_IMAGE)

    # Confidence is a structural self-assessment, not a model score.
    penalty = 0.0
    if q.has(Flag.FEW_OPTIONS):
        penalty += 0.30
    if q.has(Flag.EMPTY_STEM):
        penalty += 0.25
    if q.has(Flag.NON_SEQUENTIAL_LABELS):
        penalty += 0.15
    if q.has(Flag.DUPLICATE_OPTIONS):
        penalty += 0.10
    if q.has(Flag.LOW_CONFIDENCE):
        penalty += 0.20
    if q.has(Flag.NUMBERED_SUBITEMS):
        penalty += 0.05
    q.confidence = round(max(0.3, 1.0 - penalty), 2)


def _attach_assets(doc: SourceDocument, questions: list[Question], spans: list[tuple[int, int]]) -> None:
    """Give each question the images/tables anchored inside its line span."""
    for asset_list, target in ((doc.images, "images"), (doc.tables, "tables")):
        for asset in asset_list:
            anchor = getattr(asset, "anchor_line", -1)
            if anchor is None or anchor < 0:
                continue
            for q, (lo, hi) in zip(questions, spans):
                if lo <= anchor <= hi:
                    getattr(q, target).append(asset)
                    break


def _finalise(
    doc: SourceDocument, drafts: list[_Draft], warnings: list[str]
) -> list[Question]:
    questions: list[Question] = []
    kept_drafts: list[_Draft] = []
    for d in drafts:
        try:
            _recover_unlabelled_options(doc, d)
            options = _build_options(doc, d)
            q = Question(
                number=d.number or 0,
                stem=_stem_text(doc, d),
                stem_lines=list(d.stem_lines),
                options=options,
                answer=_build_answer(doc, d, options),
                explanation=doc.text_of(d.explanation_lines).strip(),
                explanation_lines=list(d.explanation_lines),
                source_path=doc.path,
                parser=Parser.RULES,
            )
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            # One malformed question must never abort a run.
            log.warning(
                "rules: dropping malformed question at line %s: %s", d.first_line, exc
            )
            warnings.append(f"line {d.first_line}: could not build question ({exc})")
            continue
        if d.inline_options:
            d.notes.append("options were parsed from a single inline source line")
        if d.notes:
            q.notes = "; ".join(d.notes)
        questions.append(q)
        kept_drafts.append(d)

    # Spans partition the document so images/tables land on the right question.
    spans: list[tuple[int, int]] = []
    for pos, d in enumerate(kept_drafts):
        start = d.first_line if d.first_line >= 0 else 0
        if pos + 1 < len(kept_drafts):
            end = max(start, kept_drafts[pos + 1].first_line - 1)
        else:
            end = max(start, len(doc.lines) - 1)
        spans.append((start, max(end, d.last_line)))
    _attach_assets(doc, questions, spans)

    for pos, (q, d) in enumerate(zip(questions, kept_drafts)):
        q.source_index = pos
        _apply_flags(q, d)

    survivors = _drop_incomplete(questions, kept_drafts, warnings)
    assign_numbers(survivors, warnings)
    return survivors


def assign_numbers(questions: Sequence[Question], warnings: list[str] | None = None) -> bool:
    """Number the questions 1..N in document order, remembering what the
    source printed in ``Question.source_number``.

    Chaotic files are the norm: a second section that restarts at 1, a "2."
    that follows a "5.", a question with no number at all. The exports write
    "Question <n>", and "Question 2" twice in one handout is an error whoever
    made it, so the count is ours and the printed number is kept beside it for
    anyone who wants it back (the preview screen offers exactly that switch).

    Returns True when at least one number changed. Idempotent: a second call
    finds ``source_number`` already recorded and leaves it alone.
    """
    changed = False
    for pos, q in enumerate(questions, start=1):
        if q.source_number <= 0 and q.number > 0:
            q.source_number = q.number
        if q.number != pos:
            q.number = pos
            changed = True
    if changed and warnings is not None:
        printed = [q.source_number for q in questions if q.source_number > 0]
        detail = "no numbers" if not printed else (
            f"source numbers ran {printed[0]}..{printed[-1]}"
            + (" with repeats" if len(set(printed)) != len(printed) else "")
        )
        warnings.append(
            f"questions renumbered 1..{len(questions)} in document order ({detail})"
        )
    return changed


def _drop_incomplete(
    questions: list[Question], drafts: list[_Draft], warnings: list[str]
) -> list[Question]:
    """Refuse to invent questions out of prose.

    A question needs >= 2 options. The exception is a document that is clearly
    an MCQ bank (two or more complete questions): there, a short question whose
    options are genuinely missing from the source is real and worth showing --
    flagged, not silently dropped. In a document that is *not* a question bank
    (revision notes, a syllabus with a numbered contents list) nothing
    qualifies and everything goes, which is the whole point.
    """
    complete = [q for q in questions if len(q.options) >= 2]
    bank = len(complete) >= 2
    out: list[Question] = []
    dropped = 0
    for q, d in zip(questions, drafts):
        if len(q.options) >= 2:
            out.append(q)
            continue
        keep = (
            bank
            and bool(q.stem.strip())
            and len(q.stem) <= _MAX_ORPHAN_STEM
        )
        if keep:
            out.append(q)
        else:
            dropped += 1
            log.debug(
                "rules: dropped question %s at line %s (%d options)",
                q.number, d.first_line, len(q.options),
            )
    if dropped:
        warnings.append(
            f"{dropped} block(s) had fewer than 2 options and were not "
            f"emitted as questions"
        )
    return out


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def _run(doc: SourceDocument) -> tuple[list[_Draft], list[str]]:
    warnings: list[str] = []
    scanner = _Scanner(doc)
    box_drafts, consumed = _parse_box_blocks(doc, scanner)

    flat_indices = [ln.idx for ln in doc.lines if ln.idx not in consumed]
    scanner.survey(flat_indices)
    start_number = 1
    if box_drafts:
        start_number = max((d.number or 0) for d in box_drafts) + 1
    flat_drafts, _ = scanner.run(flat_indices, expected_next=start_number)

    drafts = sorted(box_drafts + flat_drafts, key=lambda d: max(d.first_line, 0))
    if not drafts:
        warnings.append("no questions detected by the rule-based parser")
    return drafts, warnings


def detect_question_starts(doc: SourceDocument) -> list[int]:
    """Line indices where the scanner believes a new question begins.

    Reported before the "needs >= 2 options" filter, so callers can use them as
    chunk boundaries (`app.parse.ai.build_chunks`) without a question ever
    being cut in half.
    """
    drafts, _ = _run(doc)
    return sorted({d.first_line for d in drafts if d.first_line >= 0})


def parse(doc: SourceDocument) -> ParseResult:
    """Parse `doc` into questions using rules only -- no model, no network."""
    t0 = time.perf_counter()
    if not doc.lines:
        return ParseResult(
            questions=[],
            source=doc,
            parser=Parser.RULES,
            warnings=["document contained no text lines"],
        )
    drafts, warnings = _run(doc)
    questions = _finalise(doc, drafts, warnings)
    elapsed = time.perf_counter() - t0
    log.info(
        "rules: %d question(s) from %d line(s) of %s in %.3fs",
        len(questions), len(doc.lines), doc.path, elapsed,
    )
    return ParseResult(
        questions=questions,
        source=doc,
        parser=Parser.RULES,
        warnings=warnings,
        elapsed_s=elapsed,
    )
