"""The document behind the Word preview screen -- pure Python, no Qt.

Two things live here so they can be tested without a display and shared with
the review table:

* **Edit semantics for a `Question`.** Every hand edit the app allows -- the
  stem, an option's wording, the correct letter, the explanation, adding or
  removing an option -- goes through one function here. Each stamps
  ``Parser.MANUAL``, records a note, and clears exactly the warning it
  resolves, so no screen can forget to. `app.ui.review` calls these; the Word
  preview calls these; they cannot drift apart.
* **`WordDocModel`** -- the ordered, tick-boxed list of questions that will
  become the Word file: include/exclude, reorder, the layout options, the
  numbering the export will print, and the honest answer to "did the user
  change anything?", which decides whether a box document may still be
  restyled in place or has to be rebuilt.

Nothing here writes a file and nothing here generates text: the words are the
user's own typing or the parser's verbatim slices, as everywhere else.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from app.models import (
    OPTION_LABELS,
    AnswerOrigin,
    Flag,
    Option,
    Parser,
    Question,
    WordExportConfig,
    normalise_label,
)

#: Appended to Question.notes so a manual edit stays visible after export, and
#: so the Origin column can honestly say "Manual" instead of "Source".
MANUAL_EDIT_NOTE = "Edited in review"
MANUAL_ANSWER_NOTE = "Answer set manually in review"
MANUAL_OPTIONS_NOTE = "Options changed in review"
MANUAL_EXPLANATION_NOTE = "Explanation edited in review"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _collapse(text: str) -> str:
    return " ".join(text.split())


def append_note(q: Question, note: str) -> None:
    """Record an edit once; notes are a breadcrumb trail, not a log file."""
    if note in q.notes:
        return
    q.notes = f"{q.notes}; {note}" if q.notes.strip() else note


def remove_flags(q: Question, *flags: Flag) -> None:
    drop = set(flags)
    if drop.intersection(q.flags):
        q.flags = [f for f in q.flags if f not in drop]


def duplicate_option_texts(q: Question) -> bool:
    seen: set[str] = set()
    for opt in q.options:
        key = opt.normalised()
        if not key:
            continue
        if key in seen:
            return True
        seen.add(key)
    return False


def _accept_text_edit(q: Question, note: str = MANUAL_EDIT_NOTE) -> None:
    """A hand-typed text edit is the user approving the wording.

    Once they have retyped the text there is nothing left to compare against
    the source slice, so the verbatim warning is resolved -- but the edit is
    recorded in ``notes`` so it is never invisible.
    """
    q.parser = Parser.MANUAL
    append_note(q, note)
    remove_flags(q, Flag.VERBATIM_MISMATCH)
    q.verbatim_ok = True


def _refresh_option_flags(q: Question) -> None:
    """Re-derive the option-count flags after the option list changed."""
    n = len(q.options)
    if n < 2:
        q.flag(Flag.FEW_OPTIONS)
    else:
        remove_flags(q, Flag.FEW_OPTIONS)
    if n > len(OPTION_LABELS):
        q.flag(Flag.MANY_OPTIONS)
    else:
        remove_flags(q, Flag.MANY_OPTIONS)
    if duplicate_option_texts(q):
        q.flag(Flag.DUPLICATE_OPTIONS)
    else:
        remove_flags(q, Flag.DUPLICATE_OPTIONS)
    remove_flags(q, Flag.NON_SEQUENTIAL_LABELS)   # labels are re-issued A, B, C
    if q.answer_matches_an_option:
        remove_flags(q, Flag.ANSWER_NOT_IN_OPTIONS)
    else:
        q.flag(Flag.ANSWER_NOT_IN_OPTIONS)


# --------------------------------------------------------------------------- #
# Edit semantics -- the ONLY way a screen changes a Question
# --------------------------------------------------------------------------- #


def edit_stem(q: Question, text: str) -> bool:
    """Replace the stem with the user's own wording. False when unchanged."""
    if text == q.stem:
        return False
    # A one-line cell editor cannot show newlines, so an unchanged multi-line
    # stem must not be silently flattened by simply committing the editor.
    if "\n" in q.stem and "\n" not in text and _collapse(q.stem) == _collapse(text):
        return False
    q.stem = text
    # The recorded bold ranges are offsets into the *old* wording; keeping
    # them would bold whatever characters now sit at those positions.
    q.stem_bold = ()
    if text.strip():
        remove_flags(q, Flag.EMPTY_STEM)
    else:
        q.flag(Flag.EMPTY_STEM)
    _accept_text_edit(q)
    return True


def edit_option_text(q: Question, index: int, text: str) -> bool:
    """Replace one option's body text (label prefixes are never re-added)."""
    if not 0 <= index < len(q.options):
        return False
    opt = q.options[index]
    if text == opt.text:
        return False
    opt.text = text
    opt.bold = ()      # see edit_stem: the old offsets no longer apply
    # The answer body is a copy of the option body -- keep them in step.
    if q.answer.label and q.answer.label.upper() == opt.label.upper():
        q.answer.text = text
    if q.has(Flag.DUPLICATE_OPTIONS) and not duplicate_option_texts(q):
        remove_flags(q, Flag.DUPLICATE_OPTIONS)
    elif duplicate_option_texts(q):
        q.flag(Flag.DUPLICATE_OPTIONS)
    _accept_text_edit(q)
    return True


def edit_explanation(q: Question, text: str) -> bool:
    """Replace the explanation block with the user's own wording."""
    if text == q.explanation:
        return False
    if "\n" in q.explanation and "\n" not in text and _collapse(q.explanation) == _collapse(text):
        return False
    q.explanation = text
    q.explanation_bold = ()
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_EXPLANATION_NOTE)
    return True


def clear_answer(q: Question) -> bool:
    """Forget the correct letter. False when there was none to forget."""
    if q.answer.label is None and q.answer.origin is AnswerOrigin.NONE:
        return False
    q.answer.label = None
    q.answer.text = ""
    q.answer.origin = AnswerOrigin.NONE
    q.answer.confidence = 0.0
    q.answer.rationale = ""
    q.answer.source_lines = []
    remove_flags(q, Flag.AI_ANSWER, Flag.LOW_CONFIDENCE, Flag.ANSWER_NOT_IN_OPTIONS)
    q.flag(Flag.NO_ANSWER)
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_ANSWER_NOTE)
    return True


def set_answer_label(q: Question, raw: str) -> tuple[bool, str]:
    """Mark `raw` as the correct option, or clear the answer when blank.

    Returns ``(changed, error)``. Rejects anything that is not one of *this*
    question's option labels: a wrong key silently accepted here would be
    worse than no key at all.
    """
    if not raw.strip():
        return clear_answer(q), ""
    letter = normalise_label(raw)
    opt = q.option(letter) if letter else None
    if opt is None:
        labels = ", ".join(q.option_labels) or "none detected"
        return False, (
            f"“{raw.strip()}” is not an option of question {q.number}. "
            f"Valid letters: {labels}."
        )
    if (
        q.answer.label == opt.label
        and q.answer.origin is AnswerOrigin.SOURCE
        and not q.has(Flag.LOW_CONFIDENCE)
    ):
        return False, ""      # already the trusted key: nothing to confirm
    # Ticking an AI-derived or doubtful key is the user confirming it.
    q.answer.label = opt.label
    q.answer.text = opt.text  # copied from the option, never re-worded
    q.answer.origin = AnswerOrigin.SOURCE  # a human read the source: trusted
    q.answer.confidence = 1.0
    q.answer.rationale = ""
    remove_flags(
        q, Flag.NO_ANSWER, Flag.LOW_CONFIDENCE, Flag.AI_ANSWER, Flag.ANSWER_NOT_IN_OPTIONS
    )
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_ANSWER_NOTE)
    return True, ""


def add_option(q: Question, text: str = "") -> Option | None:
    """Append an option with the next free letter. None past H."""
    if len(q.options) >= len(OPTION_LABELS):
        return None
    opt = Option(label=OPTION_LABELS[len(q.options)], text=text)
    q.options.append(opt)
    _relabel(q)
    _refresh_option_flags(q)
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_OPTIONS_NOTE)
    return opt


def remove_option(q: Question, index: int) -> bool:
    """Delete one option and close the gap in the letters.

    The answer follows its option, not its letter: removing B when C is the
    key leaves the key on the same wording, now lettered B. Removing the key
    itself clears the answer, which is then flagged rather than guessed.
    """
    if not 0 <= index < len(q.options):
        return False
    answer_opt = q.option(q.answer.label) if q.answer.label else None
    removed = q.options.pop(index)
    if answer_opt is removed:
        clear_answer(q)
    _relabel(q)
    if answer_opt is not None and answer_opt is not removed:
        q.answer.label = answer_opt.label
    _refresh_option_flags(q)
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_OPTIONS_NOTE)
    return True


def move_option(q: Question, index: int, delta: int) -> bool:
    """Swap an option with a neighbour; the answer follows its wording."""
    target = index + delta
    if not (0 <= index < len(q.options) and 0 <= target < len(q.options)):
        return False
    answer_opt = q.option(q.answer.label) if q.answer.label else None
    q.options[index], q.options[target] = q.options[target], q.options[index]
    _relabel(q)
    if answer_opt is not None:
        q.answer.label = answer_opt.label
    q.parser = Parser.MANUAL
    append_note(q, MANUAL_OPTIONS_NOTE)
    return True


def _relabel(q: Question) -> None:
    for pos, opt in enumerate(q.options):
        if pos < len(OPTION_LABELS):
            opt.label = OPTION_LABELS[pos]


def was_edited(q: Question) -> bool:
    """True once any hand edit touched this question, on any screen."""
    return q.parser is Parser.MANUAL


# --------------------------------------------------------------------------- #
# The document model
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Entry:
    """One question's place in the document that will be written."""

    uid: int
    question: Question
    included: bool = True


#: Content that the in-place restyle would leave exactly as the source has it.
#: Any layout choice off this shape needs the box writer.
_FULL_CONTENT = ("include_answer", "include_explanation", "repeat_answer_in_explanation")


@dataclass(slots=True)
class WordDocModel:
    """Order, inclusion, numbering and layout choices for one Word export.

    The `Question` objects are the caller's own (the same ones the review table
    shows), so an edit made through the functions above is visible everywhere
    at once. The config is a private copy: Cancel must change nothing.
    """

    entries: list[Entry] = field(default_factory=list)
    cfg: WordExportConfig = field(default_factory=WordExportConfig)
    _original_uids: list[int] = field(default_factory=list)
    _edited_uids: set[int] = field(default_factory=set)
    _next_uid: int = 1

    @classmethod
    def from_questions(
        cls, questions: Sequence[Question], cfg: WordExportConfig
    ) -> "WordDocModel":
        model = cls(cfg=copy.deepcopy(cfg))
        for q in questions:
            model.entries.append(Entry(uid=model._next_uid, question=q))
            model._next_uid += 1
        model._original_uids = [e.uid for e in model.entries]
        return model

    # -- lookups ----------------------------------------------------------- #

    def entry(self, uid: int) -> Entry | None:
        return next((e for e in self.entries if e.uid == uid), None)

    def index_of(self, uid: int) -> int:
        return next((i for i, e in enumerate(self.entries) if e.uid == uid), -1)

    def included(self) -> list[Entry]:
        return [e for e in self.entries if e.included]

    def display_number(self, uid: int) -> int:
        """The number the export will print for this question.

        Counting (1..N over the *included* questions, in the current order)
        when `cfg.renumber`; otherwise the source's own number, falling back
        to the count for a question the source never numbered.
        """
        n = 0
        for e in self.entries:
            if not e.included:
                if e.uid == uid:
                    return 0
                continue
            n += 1
            if e.uid == uid:
                if self.cfg.renumber:
                    return n
                src = e.question.source_number
                return src if src > 0 else n
        return 0

    def source_numbering_is_clean(self) -> bool:
        """True when the source printed exactly 1..N in order."""
        printed = [e.question.source_number for e in self.entries]
        return printed == list(range(1, len(printed) + 1))

    # -- mutations --------------------------------------------------------- #

    def set_included(self, uid: int, included: bool) -> bool:
        e = self.entry(uid)
        if e is None or e.included == included:
            return False
        e.included = included
        return True

    def move(self, uid: int, delta: int) -> bool:
        """Shift a question `delta` places (negative = up). False at the edge."""
        i = self.index_of(uid)
        j = i + delta
        if i < 0 or delta == 0 or not 0 <= j < len(self.entries):
            return False
        e = self.entries.pop(i)
        self.entries.insert(j, e)
        return True

    def note_edit(self, uid: int) -> None:
        """Record that a question was changed through the edit functions."""
        self._edited_uids.add(uid)

    # -- state ------------------------------------------------------------- #

    @property
    def order_changed(self) -> bool:
        return [e.uid for e in self.entries] != self._original_uids

    @property
    def any_excluded(self) -> bool:
        return any(not e.included for e in self.entries)

    @property
    def any_edited(self) -> bool:
        return bool(self._edited_uids) or any(
            was_edited(e.question) for e in self.entries
        )

    @property
    def modified(self) -> bool:
        """Did the user change the questions or the document at all?"""
        return self.order_changed or self.any_excluded or self.any_edited

    def content_is_full(self) -> bool:
        return all(bool(getattr(self.cfg, name)) for name in _FULL_CONTENT)

    def needs_rebuild(self) -> bool:
        """Must the export be built from these questions, not restyled in place?

        The in-place restyle keeps the source file's own rows, pictures and
        numbering exactly as they are. It is therefore only honest when the
        user changed nothing: every question still in, in the source's order,
        with the source's wording, the answer and explanation rows all on, and
        numbering that the source already had right. Anything else, and the
        document has to be written from the questions on screen.
        """
        if self.modified or not self.content_is_full():
            return True
        if self.cfg.renumber:
            return not self.source_numbering_is_clean()
        return any(e.question.source_number <= 0 for e in self.entries)

    # -- output ------------------------------------------------------------ #

    def export_questions(self) -> list[Question]:
        """Copies of the included questions carrying the numbers to print."""
        out: list[Question] = []
        for e in self.entries:
            if not e.included:
                continue
            out.append(replace(e.question, number=self.display_number(e.uid)))
        return out

    def counts(self) -> tuple[int, int, int]:
        """(total, excluded, need attention among the included)."""
        total = len(self.entries)
        excluded = sum(1 for e in self.entries if not e.included)
        attention = sum(1 for e in self.entries if e.included and e.question.needs_attention)
        return total, excluded, attention


def uids_in_order(entries: Iterable[Entry]) -> list[int]:
    return [e.uid for e in entries]
