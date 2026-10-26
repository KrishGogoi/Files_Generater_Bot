"""Verbatim proof, repair, and AI/rules reconciliation.

`app.parse.ai` and `app.parse.rules` answer the same question -- *which source
lines form this MCQ* -- by completely different means, and either can be wrong.
This module is the referee:

* :func:`verify_question` re-derives every part of a ``Question`` from its line
  indices and compares it with what the parser stored. That turns the user's
  first requirement -- "the wording must be exactly the source's wording" --
  from a hope into a check that either passes or points at the offending
  character.
* :func:`repair` snaps a question back onto its verbatim source slice, so a
  parser bug degrades into a logged, visible repair instead of silently
  rewritten exam text.
* :func:`reconcile` merges the two parses: the model is better at *structure*,
  the regexes are better at an explicit ``Ans. B)`` line, and wherever the two
  disagree the question is handed to a human rather than quietly resolved.

Nothing here invents text. Every replacement value comes from
``SourceDocument.text_of`` -- a verbatim slice of the source document.
"""

from __future__ import annotations

import copy
import difflib
import logging
import re
from typing import Sequence

from app.models import (
    OPTION_LABELS,
    AnswerOrigin,
    Flag,
    ParseResult,
    Parser,
    Question,
    SourceDocument,
    labels_are_sequential,
    normalise_label,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables and patterns
# --------------------------------------------------------------------------- #

#: Characters of context either side of a divergence, matching the style of
#: verify_verbatim() in FORMATTING_BOX_FILE.py so both reports read alike.
_CONTEXT = 45

#: Stem-similarity floor for pairing AI and rules questions when their line
#: ranges do not overlap at all (a chunk boundary can shift indices).
_PAIR_SIMILARITY = 0.85

#: Confidence ceiling once the two parsers contradict each other.
_DISAGREEMENT_CONFIDENCE = 0.6

#: Multiplier for a question only one of the two parsers saw at all.
_UNCORROBORATED = 0.85

_WS_RE = re.compile(r"\s+")

#: Leading list bullets. A parser that auto-labels bullet options A, B, C
#: (see PPT_FROM_NORMAL_FILE.py) legitimately drops these from the option text,
#: so the bullet-less form of the source line is also a "verbatim" form.
_BULLET_RE = re.compile("^[ \t]*[-–—•●◦‣·*]+[ \t]+")

#: A question-number header at the very start of a stem: "Q.1", "Q1.",
#: "Question 12)", "3." ... Parsers strip it because the exports write their own
#: "Question <n>" row; keeping it would duplicate the numbering. Anchored at
#: position 0 only (no re.MULTILINE) so a numbered sub-item inside the stem --
#: the "1. Ligamentum Flavum" trap -- is never touched.
_STEM_HEADER_RE = re.compile(
    r"""^[ \t]*(?:
          (?:Q(?:uestion)?|MCQ)\s*\.?\s*\d{1,3}\s*(?:[.)\]:–-]\s*|\s+)
        | \d{1,3}[ \t]*[.)\]:–-][ \t\r\n]*
        )""",
    re.VERBOSE | re.IGNORECASE,
)

#: Words that mean "look at the picture". Taken from rules.py when it is
#: importable so the two modules never disagree about REFERENCES_MISSING_IMAGE;
#: the literal below is only the stand-in for a broken sibling.
_IMAGE_WORDS_FALLBACK_RE = re.compile(
    r"\b(image|figure|photograph|shown below|arrow)\b", re.IGNORECASE
)


def _image_words_re() -> re.Pattern[str]:
    try:
        from app.parse import rules  # local import: see _strip_option_label

        return rules.IMAGE_REFERENCE_RE
    except (ImportError, AttributeError):
        return _IMAGE_WORDS_FALLBACK_RE

#: Flags repair() derives from scratch every time. Everything else in `Flag`
#: carries information repair cannot re-observe (what the model thought, what a
#: merge found) and is therefore preserved untouched.
_RECOMPUTED_FLAGS = frozenset(
    {
        Flag.FEW_OPTIONS,
        Flag.MANY_OPTIONS,
        Flag.DUPLICATE_OPTIONS,
        Flag.NON_SEQUENTIAL_LABELS,
        Flag.ANSWER_NOT_IN_OPTIONS,
        Flag.EMPTY_STEM,
        Flag.NO_ANSWER,
        Flag.AI_ANSWER,
        Flag.VERBATIM_MISMATCH,
        Flag.REFERENCES_MISSING_IMAGE,
    }
)

#: Exceptions a single malformed question can realistically raise while being
#: repaired. Caught per question so one bad row never aborts a whole run.
_QUESTION_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError)


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    """Collapse runs of whitespace -- the tolerance the user has accepted."""
    return _WS_RE.sub(" ", text).strip()


def _norm_stem(q: Question) -> str:
    return " ".join(q.stem.split()).casefold()


def _strip_option_label(text: str) -> tuple[str | None, str]:
    """Delegate to ``rules.strip_option_label``, the only sanctioned stripper.

    Imported at call time because rules.py is a sibling written by another
    agent: this module must import cleanly even before it lands, and the
    deterministic fallback below keeps repair() honest in the meantime.
    """
    try:
        from app.parse import rules  # local import: see docstring
    except ImportError:
        return _fallback_strip_option_label(text)
    try:
        label, body = rules.strip_option_label(text)
    except _QUESTION_ERRORS:  # a rules bug must not break verification
        log.debug("rules.strip_option_label failed on %r", text[:60], exc_info=True)
        return _fallback_strip_option_label(text)
    return (normalise_label(label) if label else None), body


_FALLBACK_OPTION_RE = re.compile(r"^[ \t]*[(\[]?\s*([A-Ha-h])\s*[).\]:–-]\s*(?=\S)")


def _fallback_strip_option_label(text: str) -> tuple[str | None, str]:
    """Conservative stand-in for ``rules.strip_option_label``.

    Deliberately narrow: it only strips a single leading ``A.`` / ``A)`` /
    ``(A)`` / ``a:`` when real text follows, so "B. cereus" as the *body* of
    option A survives intact.
    """
    m = _FALLBACK_OPTION_RE.match(text)
    if not m:
        return None, text
    return normalise_label(m.group(1)), text[m.end() :]


def _dedupe(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def _strip_question_prefix(head: str) -> str:
    """Delegate to ``rules.strip_question_prefix``, the sanctioned stripper.

    It knows every header form the parser knows -- including a header nested
    in a header ("1.MCQ 1: Match the Following"), which the regex fallback
    below does not -- so the stem the parser stored is always among the forms
    offered here. Call-time import for the same reason as `_strip_option_label`.
    """
    try:
        from app.parse import rules  # local import: see _strip_option_label
    except ImportError:
        return _STEM_HEADER_RE.sub("", head, count=1)
    try:
        _, rest = rules.strip_question_prefix(head)
    except _QUESTION_ERRORS:  # a rules bug must not break verification
        log.debug("rules.strip_question_prefix failed on %r", head[:60], exc_info=True)
        return _STEM_HEADER_RE.sub("", head, count=1)
    return rest


def _derive_stem_forms(derived: str) -> list[str]:
    """Verbatim forms a stem's stored text may legitimately take.

    The header-stripped forms come first because every parser here drops the
    "Q.1" prefix; one is only *chosen* when it is the closest match, so a stem
    that really does begin with a number is not mangled. The prefix lives on
    the first line only, which is where the parsers remove it.
    """
    forms: list[str] = []
    head, sep, rest = derived.partition("\n")
    stripped_head = _strip_question_prefix(head)
    if stripped_head != head:
        if stripped_head.strip():
            forms.append(stripped_head + sep + rest)
        elif rest.strip():
            forms.append(rest)      # "5." alone on its line: the stem starts below
    stripped = _STEM_HEADER_RE.sub("", derived, count=1)
    if stripped != derived and stripped.strip():
        forms.append(stripped)
    forms.append(derived)
    return _dedupe(forms)


def _strip_answer_marker(text: str) -> str:
    """The marker-less form of an option line -- see ``rules.strip_answer_marker``."""
    try:
        from app.parse import rules  # local import: see _strip_option_label
    except ImportError:
        return text
    try:
        _, clean = rules.strip_answer_marker(text)
    except _QUESTION_ERRORS:  # a rules bug must not break verification
        log.debug("rules.strip_answer_marker failed on %r", text[:60], exc_info=True)
        return text
    return clean


#: Mirrors rules._BARE_LETTER_RE. Used for verification only, where offering an
#: extra candidate form is safe -- the sequence guard lives in the parser.
_BARE_LETTER_RE = re.compile(r"^\s*([A-H])\s+(?=\S)")


def _strip_options_prefix(text: str) -> str:
    """Delegate to ``rules.strip_options_prefix``, the sanctioned stripper.

    Call-time import for the same reason as `_strip_option_label`; the fallback
    is a no-op because failing to strip only costs one candidate form.
    """
    try:
        from app.parse import rules  # local import: see _strip_option_label
    except ImportError:
        return text
    try:
        return rules.strip_options_prefix(text)
    except _QUESTION_ERRORS:  # a rules bug must not break verification
        log.debug("rules.strip_options_prefix failed on %r", text[:60], exc_info=True)
        return text


def _inline_option_bodies(derived: str) -> dict[str, str]:
    """Bodies of options packed onto ONE source line, keyed by label.

    A line like ``"Options: A) x B) y C) z D) w"`` is a single source index
    shared by four `Option` objects, so comparing each one against the whole
    line fails for all four. Splitting it exactly the way the parser did is what
    makes those questions provably verbatim rather than falsely suspect.
    """
    try:
        from app.parse import rules  # local import: see _strip_option_label
    except ImportError:
        return {}
    try:
        pairs = rules.parse_inline_options(derived)
    except _QUESTION_ERRORS:  # a rules bug must not break verification
        log.debug("rules.parse_inline_options failed on %r", derived[:60], exc_info=True)
        return {}
    return dict(pairs)


def _derive_option_forms(
    derived: str, want_label: str | None = None
) -> tuple[str | None, list[str]]:
    """Return ``(label, forms)`` for the verbatim source text of one option.

    `forms` are the shapes the stored ``Option.text`` may legitimately have:
    this option's own segment when several share one source line, label
    stripped (the normal case), bullet stripped, or the raw slice.
    """
    forms: list[str] = []

    # Several options on one line -> prefer this option's own segment.
    inline = _inline_option_bodies(derived)
    if inline:
        wanted = normalise_label(want_label) if want_label else None
        if wanted and wanted in inline:
            forms.append(inline[wanted])
        else:
            # No label to go on: any segment is a legitimate reading, and
            # _best_form picks the one that actually matches.
            forms.extend(inline.values())

    label, body = _strip_option_label(derived)
    # An empty body is legitimate, not a failure: a source cell really can hold a
    # bare "B." for an option with no text, and the parser stores "" for it.
    if label is not None:
        forms.append(body)

    # "Options: A. Functional Hallucinations" -- header and first option on one
    # line. The parser stores just the option body, so that has to count as a
    # verbatim reading of this line.
    after_prefix = _strip_options_prefix(derived)
    if after_prefix != derived:
        plabel, pbody = _strip_option_label(after_prefix)
        if plabel is not None:
            label = label or plabel
            forms.append(pbody)
        else:
            # "Options: A Guilt" -- an unpunctuated label. The parser reads it as
            # option A behind the "Options:" header, so "Guilt" is a verbatim
            # reading of this line and has to be offered as a candidate form.
            bare = _BARE_LETTER_RE.match(after_prefix)
            if bare:
                label = label or normalise_label(bare.group(1))
                forms.append(after_prefix[bare.end():])
        forms.append(after_prefix)

    no_bullet = _BULLET_RE.sub("", derived, count=1)
    if no_bullet != derived and no_bullet.strip():
        label2, body2 = _strip_option_label(no_bullet)
        if label2 is not None and body2.strip():
            label = label or label2
            forms.append(body2)
        forms.append(no_bullet)

    forms.append(derived)

    # A tick on the correct option ("C. Aspirin ✅") is an ornament the parser
    # strips, so the marker-less reading of every form is verbatim too.
    for form in list(forms):
        clean = _strip_answer_marker(form)
        if clean != form and clean.strip():
            forms.append(clean)
    return label, _dedupe(forms)


def _best_form(stored: str, forms: Sequence[str]) -> str:
    """The verbatim form `stored` was meant to be.

    Exact (whitespace-collapsed) equality wins; otherwise the closest form by
    difflib ratio. Deterministic -- ``max`` keeps the first maximum and the form
    order is fixed -- which is what makes repair() idempotent.
    """
    if len(forms) == 1:
        return forms[0]
    target = _norm(stored)
    for f in forms:
        if _norm(f) == target:
            return f
    return max(
        forms,
        key=lambda f: difflib.SequenceMatcher(None, target, _norm(f)).ratio(),
    )


def _note(q: Question, text: str) -> None:
    """Append a note once. Substring-checked, so repair() stays idempotent."""
    if not text or text in q.notes:
        return
    q.notes = f"{q.notes} | {text}" if q.notes else text


def _length_compatible(la: int, lb: int, similarity: float) -> bool:
    """Cheap reject: two strings this different in length cannot reach `ratio`.

    ``ratio = 2*M/T`` with ``M <= min(la, lb)`` and ``T = la + lb``, so
    ``min/max >= s/(2-s)`` is a hard requirement.
    """
    if not la or not lb:
        return la == lb
    lo, hi = min(la, lb), max(la, lb)
    return lo * (2.0 - similarity) >= similarity * hi


# --------------------------------------------------------------------------- #
# verify_question
# --------------------------------------------------------------------------- #


def _parts(q: Question) -> list[tuple[str, str, list[int], bool, str | None]]:
    """(name, stored text, source indices, is_stem, label) for each verifiable part.

    The label rides along so that an option sharing a source line with its
    siblings can be matched against its own segment of that line.
    """
    out: list[tuple[str, str, list[int], bool, str | None]] = [
        ("stem", q.stem, list(q.stem_lines), True, None)
    ]
    for pos, o in enumerate(q.options):
        name = f"option {o.label.strip().upper()}" if o.label.strip() else f"option #{pos + 1}"
        out.append((name, o.text, list(o.source_lines), False, o.label))
    return out


def _divergence_detail(name: str, stored: str, forms: Sequence[str]) -> str:
    """First-divergence report, in the style of FORMATTING_BOX_FILE.verify_verbatim."""
    src = _norm(_best_form(stored, forms))
    got = _norm(stored)
    for i, (a, b) in enumerate(zip(src, got)):
        if a != b:
            return (
                f"{name}: CONTENT DIFFERS near char {i}:\n"
                f"          source: ...{src[max(0, i - _CONTEXT):i + _CONTEXT]!r}...\n"
                f"          stored: ...{got[max(0, i - _CONTEXT):i + _CONTEXT]!r}..."
            )
    i = min(len(src), len(got))
    return (
        f"{name}: CONTENT DIFFERS in length "
        f"(source {len(src)} vs stored {len(got)} chars) from char {i}:\n"
        f"          source: ...{src[max(0, i - _CONTEXT):i + _CONTEXT]!r}...\n"
        f"          stored: ...{got[max(0, i - _CONTEXT):i + _CONTEXT]!r}..."
    )


def verify_question(q: Question, doc: SourceDocument) -> tuple[bool, str]:
    """Re-derive stem/options from their line indices and compare.

    Equal after whitespace collapsing -- and, for an option, after removing the
    label prefix -- gives ``(True, "")``. Anything else gives
    ``(False, detail)`` where `detail` shows the first divergence with context
    either side.

    A part with no source indices (a hand-edited question) cannot be compared
    against anything, so it is reported in the note of an ``ok=True`` result
    rather than failed.
    """
    unverifiable: list[str] = []

    for name, stored, indices, is_stem, label in _parts(q):
        if not indices:
            if stored.strip():
                unverifiable.append(name)
            continue

        valid = [i for i in indices if doc.line(i) is not None]
        if not valid:
            return False, (
                f"{name}: source line indices {indices!r} are out of range "
                f"(document has {len(doc.lines)} lines)"
            )

        derived = doc.text_of(valid)
        forms = (
            _derive_stem_forms(derived)
            if is_stem
            else _derive_option_forms(derived, label)[1]
        )
        target = _norm(stored)
        if any(_norm(f) == target for f in forms):
            continue
        return False, _divergence_detail(name, stored, forms)

    if unverifiable:
        if len(unverifiable) == len(_parts(q)):
            return True, (
                "no source line indices: this question's text cannot be verified "
                "against the source (hand-edited?)"
            )
        return True, (
            "verified, except for parts with no source line indices: "
            + ", ".join(unverifiable)
        )
    return True, ""


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #


def _prune_indices(
    q: Question, doc: SourceDocument, indices: list[int], has_text: bool
) -> list[int]:
    """Drop out-of-range indices -- but never the last shred of evidence.

    If *every* index of a part is out of range while it still holds text, the
    indices are kept: dropping them would turn a provable verbatim mismatch
    into an unverifiable question, which is exactly the failure the user must
    be able to see.
    """
    valid = [i for i in indices if doc.line(i) is not None]
    if len(valid) == len(indices):
        return indices
    if not valid and has_text:
        return indices
    _note(
        q,
        f"dropped {len(indices) - len(valid)} out-of-range source line index"
        f"{'es' if len(indices) - len(valid) > 1 else ''}",
    )
    return valid


def _repair_stem(q: Question, doc: SourceDocument) -> None:
    q.stem_lines = _prune_indices(q, doc, list(q.stem_lines), bool(q.stem.strip()))
    valid = [i for i in q.stem_lines if doc.line(i) is not None]
    if not valid:
        return
    derived = doc.text_of(valid)
    if not derived.strip():
        # Pointing at blank lines proves nothing; keep the text so it is not
        # destroyed and let verify_question flag the mismatch.
        return
    target = _best_form(q.stem, _derive_stem_forms(derived))
    if _norm(target) != _norm(q.stem):
        _note(q, "repaired: stem snapped back to its verbatim source slice")
    q.stem = target


def _repair_options(q: Question, doc: SourceDocument) -> None:
    for o in q.options:
        o.source_lines = _prune_indices(
            q, doc, list(o.source_lines), bool(o.text.strip())
        )
        valid = [i for i in o.source_lines if doc.line(i) is not None]
        if not valid:
            continue
        derived = doc.text_of(valid)
        if not derived.strip():
            continue
        label, forms = _derive_option_forms(derived, o.label)
        target = _best_form(o.text, forms)
        if _norm(target) != _norm(o.text):
            _note(
                q,
                f"repaired: option {o.label.strip().upper() or '?'} snapped back "
                "to its verbatim source slice",
            )
        o.text = target
        if label and label != o.label.strip().upper():
            _note(q, f"re-derived option label {o.label.strip() or '(blank)'} -> {label}")
            o.label = label

    # Canonicalise, then fill any label the source never carried (bullet lists)
    # by position, so exports never print a bare ". text".
    for pos, o in enumerate(q.options):
        canon = normalise_label(o.label)
        if canon:
            o.label = canon
        elif pos < len(OPTION_LABELS):
            o.label = OPTION_LABELS[pos]
            _note(q, f"assigned option label {o.label} by position (none in source)")


def _repair_answer(q: Question, doc: SourceDocument) -> None:
    ans = q.answer
    valid = [i for i in ans.source_lines if doc.line(i) is not None]
    if len(valid) != len(ans.source_lines):
        _note(q, "dropped out-of-range answer source line index(es)")
        ans.source_lines = valid

    canon = normalise_label(ans.label or "")
    if canon:
        ans.label = canon
        if q.options and q.option(canon) is None:
            # A key pointing at an option that does not exist is a real defect,
            # not something to paper over.
            _note(q, f"answer label {canon} has no matching option")
            q.flag(Flag.LOW_CONFIDENCE)
    elif ans.origin is not AnswerOrigin.NONE:
        _note(q, f"answer label {ans.label!r} is not a valid option letter")


def _answer_is_known(q: Question) -> bool:
    """`Answer.is_known` plus the sanity check that the label is a real letter."""
    return (
        q.answer.origin is not AnswerOrigin.NONE
        and normalise_label(q.answer.label or "") is not None
    )


def _recompute_flags(q: Question, verbatim_ok: bool) -> list[Flag]:
    """Locally determinable flags, rebuilt from the question's current state.

    Returned in `Flag` declaration order: a stable order is what lets
    ``repair(repair(q)) == repair(q)`` hold as dataclass equality.
    """
    present = {f for f in q.flags if f not in _RECOMPUTED_FLAGS}

    n_opts = len(q.options)
    if n_opts < 2:
        present.add(Flag.FEW_OPTIONS)
    if n_opts > len(OPTION_LABELS):
        present.add(Flag.MANY_OPTIONS)

    bodies = [o.normalised() for o in q.options if o.normalised()]
    if len(bodies) != len(set(bodies)):
        present.add(Flag.DUPLICATE_OPTIONS)
    if n_opts and not labels_are_sequential(q.option_labels):
        present.add(Flag.NON_SEQUENTIAL_LABELS)
    if not q.answer_matches_an_option:
        present.add(Flag.ANSWER_NOT_IN_OPTIONS)

    if not q.stem.strip():
        present.add(Flag.EMPTY_STEM)
    if not _answer_is_known(q):
        present.add(Flag.NO_ANSWER)
    if q.answer.origin is AnswerOrigin.AI:
        present.add(Flag.AI_ANSWER)  # AI answers are new information: always marked
    if not verbatim_ok:
        present.add(Flag.VERBATIM_MISMATCH)

    # Preserve a parser's own judgement here (its keyword set may be wider),
    # but never claim a missing image when one is attached.
    if not q.images and (
        Flag.REFERENCES_MISSING_IMAGE in q.flags or _image_words_re().search(q.stem)
    ):
        present.add(Flag.REFERENCES_MISSING_IMAGE)

    return [f for f in Flag if f in present]


def repair(q: Question, doc: SourceDocument) -> Question:
    """Return a copy of `q` whose text is provably the source's text.

    Snaps the stem and every option back onto their verbatim source slice,
    re-derives option labels, drops out-of-range indices, recomputes the locally
    determinable flags and sets ``verbatim_ok`` / ``notes``. Idempotent.

    A ``Parser.MANUAL`` question is left alone apart from its flags: the user
    edited it deliberately, so their wording -- not the source's -- is the
    authority, and re-flagging it as a verbatim mismatch would fight the review
    table's own "clears the flag it fixes" behaviour.
    """
    out = copy.deepcopy(q)
    manual = out.parser is Parser.MANUAL

    if not manual:
        _repair_stem(out, doc)
        _repair_options(out, doc)
        _repair_answer(out, doc)
        expl_valid = [i for i in out.explanation_lines if doc.line(i) is not None]
        if len(expl_valid) != len(out.explanation_lines):
            _note(out, "dropped out-of-range explanation source line index(es)")
            out.explanation_lines = expl_valid

    ok, detail = verify_question(out, doc)
    if manual:
        out.verbatim_ok = True
        if not ok:
            _note(out, "manual edit: text intentionally differs from the source")
    else:
        out.verbatim_ok = ok
        if detail:
            _note(out, detail)
        if not ok:
            log.warning(
                "question %s (%s) is not a verbatim source slice: %s",
                out.number,
                out.source_path or doc.path,
                detail.splitlines()[0],
            )

    out.flags = _recompute_flags(out, out.verbatim_ok)
    return out


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #


def _key_lines(q: Question) -> set[int]:
    """The lines that identify a question: its stem, or anything it owns."""
    return set(q.stem_lines) or set(q.all_lines)


def _span(q: Question) -> tuple[int, int] | None:
    idx = _key_lines(q)
    return (min(idx), max(idx)) if idx else None


def _interval_overlap(a: tuple[int, int] | None, b: tuple[int, int] | None) -> int:
    if a is None or b is None:
        return 0
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def _pair_questions(
    ai_qs: Sequence[Question], ru_qs: Sequence[Question]
) -> dict[int, int]:
    """Map AI question index -> rules question index, one to one.

    Line ranges are the reliable key (both parsers indexed the same document);
    normalised-stem similarity is the fallback for the case where one parser put
    the stem's boundaries somewhere else entirely.
    """
    scored: list[tuple[float, int, int]] = []
    ai_keys = [_key_lines(q) for q in ai_qs]
    ru_keys = [_key_lines(q) for q in ru_qs]
    ai_spans = [_span(q) for q in ai_qs]
    ru_spans = [_span(q) for q in ru_qs]

    for i, a_set in enumerate(ai_keys):
        for j, r_set in enumerate(ru_keys):
            shared = len(a_set & r_set)
            if shared:
                # Shared stem lines beat any amount of mere interval overlap.
                scored.append((100.0 + shared, i, j))
                continue
            ov = _interval_overlap(ai_spans[i], ru_spans[j])
            if ov:
                scored.append((float(ov), i, j))

    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for _score, i, j in scored:
        if i in pairs or j in taken:
            continue
        pairs[i] = j
        taken.add(j)

    # Fallback: similar stems that share no line at all.
    free_ai = [i for i in range(len(ai_qs)) if i not in pairs]
    free_ru = [j for j in range(len(ru_qs)) if j not in taken]
    if free_ai and free_ru:
        sims: list[tuple[float, int, int]] = []
        ai_norm = {i: _norm_stem(ai_qs[i]) for i in free_ai}
        ru_norm = {j: _norm_stem(ru_qs[j]) for j in free_ru}
        for i in free_ai:
            a = ai_norm[i]
            if len(a) < 12:  # too little signal to match on
                continue
            for j in free_ru:
                b = ru_norm[j]
                if len(b) < 12 or not _length_compatible(len(a), len(b), _PAIR_SIMILARITY):
                    continue
                sm = difflib.SequenceMatcher(None, a, b)
                if sm.real_quick_ratio() < _PAIR_SIMILARITY:
                    continue
                if sm.quick_ratio() < _PAIR_SIMILARITY:
                    continue
                ratio = sm.ratio()
                if ratio >= _PAIR_SIMILARITY:
                    sims.append((ratio, i, j))
        sims.sort(key=lambda t: (-t[0], t[1], t[2]))
        for _ratio, i, j in sims:
            if i in pairs or j in taken:
                continue
            pairs[i] = j
            taken.add(j)

    return pairs


def _adopt_rules_options(q: Question, rq: Question) -> None:
    """Take the rule-based options when the AI parse produced none worth having.

    "AI wins on structure" only means anything when the AI actually found a
    structure; zero options is a failure, not an opinion.
    """
    q.options = copy.deepcopy(rq.options)
    _note(q, "options taken from the rule-based parse (the AI parse found none)")


def _merge_answer(q: Question, aq: Question, rq: Question) -> int:
    """Fill in / cross-check the answer. Returns the disagreement count."""
    a_ans, r_ans = aq.answer, rq.answer
    a_label = normalise_label(a_ans.label or "")
    r_label = normalise_label(r_ans.label or "")
    a_stated = a_ans.origin is AnswerOrigin.SOURCE and a_label is not None
    r_stated = r_ans.origin is AnswerOrigin.SOURCE and r_label is not None

    if not r_stated:
        return 0  # rules has nothing better to offer

    if a_stated and a_label == r_label:
        return 0  # both read the same key: nothing to do

    if a_stated:
        # Two source-stated keys that differ. The regex reads an explicit
        # "Ans. B)" line more reliably than the model, so it wins the field --
        # but the human is told, because a contradiction here is serious.
        q.answer = copy.deepcopy(r_ans)
        q.flag(Flag.LOW_CONFIDENCE)
        q.confidence = min(q.confidence, _DISAGREEMENT_CONFIDENCE)
        _note(
            q,
            f"answer disagreement: AI read {a_label} from the source, rules read "
            f"{r_label}; kept {r_label} (explicit answer line)",
        )
        return 1

    if a_ans.origin is AnswerOrigin.AI and a_label and a_label != r_label:
        # The model reasoned its way to a different letter than the document
        # states. Keep the document's, flag the contradiction.
        q.answer = copy.deepcopy(r_ans)
        q.flag(Flag.LOW_CONFIDENCE)
        q.confidence = min(q.confidence, _DISAGREEMENT_CONFIDENCE)
        _note(
            q,
            f"answer disagreement: AI derived {a_label} but the source states "
            f"{r_label}; kept {r_label}",
        )
        return 1

    q.answer = copy.deepcopy(r_ans)
    _note(q, f"answer {r_label} taken from the source line the rules parse found")
    return 0


def _merge_pair(aq: Question, rq: Question) -> tuple[Question, int]:
    """AI structure + rules fill-in for one paired question."""
    q = copy.deepcopy(aq)
    disagreements = 0

    n_ai, n_ru = len(aq.options), len(rq.options)
    if n_ai < 2 <= n_ru:
        _adopt_rules_options(q, rq)
        q.flag(Flag.LOW_CONFIDENCE)
        q.confidence = min(q.confidence, _DISAGREEMENT_CONFIDENCE)
        disagreements += 1
    elif n_ai != n_ru and n_ru >= 2:
        q.flag(Flag.LOW_CONFIDENCE)
        q.confidence = min(q.confidence, _DISAGREEMENT_CONFIDENCE)
        _note(q, f"option count differs: AI {n_ai} vs rules {n_ru}")
        disagreements += 1
    elif n_ai != n_ru:
        # rules found 0/1 options where the AI found several: a rules miss, not
        # a real contradiction. Recorded, not flagged.
        _note(q, f"rule-based parse found only {n_ru} option(s) here")

    disagreements += _merge_answer(q, aq, rq)

    if rq.has(Flag.NUMBERED_SUBITEMS):
        q.flag(Flag.NUMBERED_SUBITEMS)
    if not q.explanation.strip() and rq.explanation.strip():
        q.explanation = rq.explanation
        q.explanation_lines = list(rq.explanation_lines)
    if not q.images and rq.images:
        q.images = list(rq.images)
    if not q.tables and rq.tables:
        q.tables = list(rq.tables)
    return q, disagreements


def reconcile(
    ai: ParseResult, rules: ParseResult, doc: SourceDocument
) -> ParseResult:
    """Merge the AI and rule-based parses of the same document.

    The AI wins on structure (stem and option boundaries -- what it is good at).
    The rules parse fills in questions the AI dropped entirely and supplies a
    source-stated answer the AI missed. Where the two disagree on option count
    or on a stated answer label the confidence drops and ``Flag.LOW_CONFIDENCE``
    is set: a disagreement between two independent parsers is precisely the
    signal a human should see, so it is never silently resolved.

    Every returned question goes through :func:`repair`, so ``verbatim_ok`` is
    always accurate. ``parser=Parser.AI_RULES``; document order is preserved.
    """
    ai_qs = list(ai.questions)
    ru_qs = list(rules.questions)
    pairs = _pair_questions(ai_qs, ru_qs)
    paired_rules = set(pairs.values())

    staged: list[tuple[int, Question]] = []
    disagreements = 0

    for i, aq in enumerate(ai_qs):
        j = pairs.get(i)
        if j is None:
            q = copy.deepcopy(aq)
            if ru_qs:
                q.confidence = round(q.confidence * _UNCORROBORATED, 3)
                _note(q, "not corroborated by the rule-based parse")
        else:
            try:
                q, n = _merge_pair(aq, ru_qs[j])
            except _QUESTION_ERRORS:
                log.exception("could not merge question %s; keeping the AI parse", aq.number)
                q, n = copy.deepcopy(aq), 0
                _note(q, "internal error while reconciling; kept the AI parse")
            disagreements += n
        staged.append((len(staged), q))

    for j, rq in enumerate(ru_qs):
        if j in paired_rules:
            continue
        q = copy.deepcopy(rq)
        _note(q, "recovered from the rule-based parse (the AI parse missed it)")
        if ai_qs:
            q.confidence = round(q.confidence * _UNCORROBORATED, 3)
            if q.has(Flag.NUMBERED_SUBITEMS):
                # A bare "N." sub-item promoted to a question is the classic
                # phantom; the AI not seeing it is corroborating evidence.
                q.flag(Flag.LOW_CONFIDENCE)
                _note(q, "possible phantom question split from a numbered sub-list")
        staged.append((len(staged), q))

    # Document order: first stem line, then the order they were staged in.
    staged.sort(key=lambda pair: (_first_line(pair[1]), pair[0]))

    out: list[Question] = []
    for pos, (_stage, q) in enumerate(staged):
        q.source_index = pos
        if q.number <= 0:
            q.number = pos + 1
        if q.parser is not Parser.MANUAL:
            q.parser = Parser.AI_RULES
        if not q.source_path:
            q.source_path = doc.path
        try:
            out.append(repair(q, doc))
        except _QUESTION_ERRORS:
            log.exception("repair failed for question %s; keeping it unrepaired", q.number)
            _note(q, "internal error during verbatim repair; text NOT verified")
            q.verbatim_ok = False
            q.flag(Flag.VERBATIM_MISMATCH)
            out.append(q)

    warnings = _merge_warnings(ai.warnings, rules.warnings)
    n_ai_only = sum(1 for i in range(len(ai_qs)) if i not in pairs)
    n_rules_only = len(ru_qs) - len(paired_rules)
    warnings.append(
        f"reconcile: {len(pairs)} question(s) agreed by both parsers, "
        f"{n_ai_only} AI-only, {n_rules_only} rules-only, "
        f"{disagreements} disagreement(s) flagged for review"
    )
    if not ai_qs:
        warnings.append(
            "reconcile: the AI parse produced no questions; this result is "
            "rule-based only"
        )
    n_bad = sum(1 for q in out if not q.verbatim_ok)
    if n_bad:
        warnings.append(
            f"{n_bad} question(s) could not be proved to be verbatim slices of "
            "the source - see the Notes column"
        )

    log.info(
        "reconciled %d AI + %d rules questions -> %d (%d disagreement(s))",
        len(ai_qs),
        len(ru_qs),
        len(out),
        disagreements,
    )
    return ParseResult(
        questions=out,
        source=doc,
        parser=Parser.AI_RULES,
        warnings=warnings,
        elapsed_s=ai.elapsed_s + rules.elapsed_s,
        ai_calls=ai.ai_calls + rules.ai_calls,
    )


def _first_line(q: Question) -> int:
    idx = _key_lines(q)
    return min(idx) if idx else 1_000_000_000


def _merge_warnings(*groups: Sequence[str]) -> list[str]:
    """Carry warnings from every input, dropping exact repeats.

    Both parses see the same reader warnings, so without the de-dupe the UI
    would show every ingest problem twice.
    """
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for w in group:
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
    return out
