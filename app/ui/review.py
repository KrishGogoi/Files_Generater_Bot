"""Review & export screen -- where the user catches the AI's mistakes.

This is the accuracy gate of the whole app, so the design bias is "impossible to
miss, easy to fix":

* every machine warning is translated into a plain-English sentence (a user
  should never have to know what ``REFERENCES_MISSING_IMAGE`` means),
* rows that need a human are tinted amber, rows whose text drifted from the
  source are tinted red *and* raise a persistent banner,
* the answer letter, the stem and every option are editable in place; an edit
  stamps ``Parser.MANUAL`` and clears exactly the warning it resolves.

Nothing here re-generates question text: edits are the user's own words, and
everything else is the verbatim slice produced upstream by
``SourceDocument.text_of``. Imports from ``app.parse`` / ``app.export`` are done
lazily inside methods so this module stays importable (and the GUI stays fast to
start) while its sibling modules are still being written.
"""

from __future__ import annotations

import logging
import os
from typing import NamedTuple, Sequence

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.models import (
    AnswerOrigin,
    Flag,
    ImageAsset,
    Question,
    labels_are_sequential,
)
from app.ui import word_doc_model as edits
from app.ui.word_doc_model import MANUAL_ANSWER_NOTE, MANUAL_EDIT_NOTE  # noqa: F401 - re-exported

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Presentation constants
# --------------------------------------------------------------------------- #

#: Brand palette (kept local so this module never imports app.ui.theme, which
#: another module owns -- colour is *signal* here, not decoration).
BRAND_BLUE = "#141E52"
ACCENT = "#1AB7EA"
DANGER = "#E23A3A"
WARNING = "#E89C1F"
OK_GREEN = "#2FBF71"

#: Semi-transparent tints: alpha lets the same colour read correctly on a light
#: or a dark palette, and leaves the theme's own text colour legible.
AMBER_TINT = QColor(0xE8, 0x9C, 0x1F, 62)
RED_TINT = QColor(0xE2, 0x3A, 0x3A, 74)

#: How much of the stem survives in the one-line table cell.
STEM_ELIDE_CHARS = 150
ANSWER_ELIDE_CHARS = 44

#: Longest side of an image thumbnail in the detail pane.
THUMB_PX = 140

FILTER_ALL = "all"
FILTER_ATTENTION = "attention"
FILTER_AI = "ai"
FILTER_MISMATCH = "mismatch"

FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    ("All", FILTER_ALL),
    ("Needs attention", FILTER_ATTENTION),
    ("AI answers", FILTER_AI),
    ("Verbatim mismatch", FILTER_MISMATCH),
)

#: MANUAL_EDIT_NOTE / MANUAL_ANSWER_NOTE live in `app.ui.word_doc_model` with
#: the edit functions themselves, and are re-exported above for older callers.

BANNER_QSS = (
    f"background:{DANGER}; color:#FFFFFF; padding:6px 10px; "
    "border-radius:6px; font-weight:600;"
)

# --------------------------------------------------------------------------- #
# Flag vocabulary -- machine warning -> sentence a teacher can act on
# --------------------------------------------------------------------------- #

FLAG_SENTENCES: dict[Flag, str] = {
    Flag.NO_ANSWER: (
        "No correct answer is marked: the file did not state one and none could "
        "be worked out safely. Pick the right option below."
    ),
    Flag.AI_ANSWER: (
        "The correct option was worked out by the AI, not stated in your file. "
        "Please verify it before exporting."
    ),
    Flag.LOW_CONFIDENCE: (
        "The AI was unsure about this question, or the two parsers disagreed, so "
        "it is worth reading carefully."
    ),
    Flag.FEW_OPTIONS: (
        "Fewer than two options were found, so options were probably missed or "
        "run together."
    ),
    Flag.MANY_OPTIONS: (
        "More than eight options were found, so unrelated lines may have been "
        "pulled in as options."
    ),
    Flag.DUPLICATE_OPTIONS: (
        "Two options have exactly the same text, so one of them is probably "
        "wrong or duplicated."
    ),
    Flag.NON_SEQUENTIAL_LABELS: (
        "The option letters do not run A, B, C ... without gaps, so an option may "
        "be missing or mislabelled."
    ),
    Flag.ANSWER_NOT_IN_OPTIONS: (
        "The answer letter from the document does not match any of the options "
        "listed, so the answer key cannot be trusted. Fix the option or the "
        "answer before exporting."
    ),
    Flag.REFERENCES_MISSING_IMAGE: (
        "The question refers to an image that is not in the file, so the answer "
        "was not guessed."
    ),
    Flag.VERBATIM_MISMATCH: (
        "The text here does not match the source document exactly. Check it "
        "against the original before exporting."
    ),
    Flag.EMPTY_STEM: "The question text is empty.",
    Flag.NUMBERED_SUBITEMS: (
        "This question contains its own numbered list, which was kept inside the "
        "question instead of being split into separate questions."
    ),
    Flag.MERGED_DUPLICATE: (
        "A near-identical question was found in another file; this is the copy "
        "that was kept."
    ),
    Flag.TRUNCATED: (
        "A source line looked cut off, so some words may be missing."
    ),
}

#: Compact chips for the Flags column -- short enough to fit, loud enough to see.
FLAG_CHIPS: dict[Flag, str] = {
    Flag.NO_ANSWER: "NO ANSWER",
    Flag.AI_ANSWER: "AI",
    Flag.LOW_CONFIDENCE: "LOW CONF",
    Flag.FEW_OPTIONS: "FEW OPTS",
    Flag.MANY_OPTIONS: "MANY OPTS",
    Flag.DUPLICATE_OPTIONS: "DUP OPTS",
    Flag.NON_SEQUENTIAL_LABELS: "LABELS",
    Flag.ANSWER_NOT_IN_OPTIONS: "BAD ANSWER",
    Flag.REFERENCES_MISSING_IMAGE: "NO IMAGE",
    Flag.VERBATIM_MISMATCH: "MISMATCH",
    Flag.EMPTY_STEM: "EMPTY STEM",
    Flag.NUMBERED_SUBITEMS: "SUB-LIST",
    Flag.MERGED_DUPLICATE: "DUPLICATE Q",
    Flag.TRUNCATED: "CUT OFF",
}


def _set_primary(button: QPushButton) -> None:
    """Give `button` the shared primary look.

    Imported lazily and defensively on purpose: this module owns *signal* colour
    (amber rows, red mismatches) and must stay importable without the theme, but
    the accent on a call-to-action belongs to the theme -- hard-coding it here is
    what left the export buttons Cerebellum cyan on the Batman dark palette.
    """
    try:
        from app.ui import theme

        theme.set_variant(button, "primary")
    except Exception as exc:  # noqa: BLE001 - a plain button is a fine fallback
        log.debug("primary styling unavailable: %s", exc)


def flag_sentence(f: Flag) -> str:
    """Plain-English meaning of `f`; falls back to a readable form of the enum."""
    fallback = str(getattr(f, "value", f)).replace("_", " ").capitalize() + "."
    return FLAG_SENTENCES.get(f, fallback)


def flag_chip(f: Flag) -> str:
    """Short uppercase chip for `f`, used in the compact Flags column."""
    return FLAG_CHIPS.get(f, str(getattr(f, "value", f)).replace("_", " ").upper())


def flags_plain_english(flags: Sequence[Flag]) -> list[str]:
    """Every flag spelled out, de-duplicated, in the order it was raised."""
    seen: list[str] = []
    for f in flags:
        sentence = flag_sentence(f)
        if sentence not in seen:
            seen.append(sentence)
    return seen


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _elide(text: str, limit: int) -> str:
    """Whitespace-collapsed `text` cut to `limit` chars -- table cells are one line."""
    flat = _collapse(text)
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _basename(path: str) -> str:
    return os.path.basename(path) if path else ""


def _options_summary(q: Question) -> str:
    """e.g. ``4 (A-D)`` for a clean set, ``3 (A, B, D)`` when labels have gaps."""
    labels = q.option_labels
    if not labels:
        return "0"
    if len(labels) >= 2 and labels_are_sequential(labels):
        return f"{len(labels)} ({labels[0]}-{labels[-1]})"
    return f"{len(labels)} ({', '.join(labels)})"


def _answer_origin_text(q: Question) -> str:
    """Source / AI / Manual / ``-`` -- provenance is never blurred."""
    if q.answer.label and MANUAL_ANSWER_NOTE in q.notes:
        return "Manual"
    if q.answer.origin is AnswerOrigin.SOURCE:
        return "Source"
    if q.answer.origin is AnswerOrigin.AI:
        return "AI"
    return "-"


def _effective_confidence(q: Question) -> float:
    """The honest number: the *lower* of parse and answer confidence.

    ``answers.py`` scores the answer while ``reconcile`` scores the structure; a
    95%-parsed question with a 40%-confident AI answer must not read as 95%.
    """
    conf = float(q.confidence)
    if q.answer.is_known:
        conf = min(conf, float(q.answer.confidence))
    return max(0.0, min(1.0, conf))


def _answer_display(q: Question) -> str:
    if not q.answer.label:
        return "-"
    text = q.answer.text
    if not text:
        opt = q.option(q.answer.label)
        text = opt.text if opt is not None else ""
    if not text:
        return q.answer.label
    return f"{q.answer.label} — {_elide(text, ANSWER_ELIDE_CHARS)}"


class ReviewCounts(NamedTuple):
    """Footer / banner arithmetic, computed in one place."""

    total: int
    attention: int
    ai: int
    mismatch: int


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


class _RowTintDelegate(QStyledItemDelegate):
    """Paints the per-row tint itself instead of relying on ``BackgroundRole``.

    Qt drops a model's ``BackgroundRole`` for item painting as soon as the
    application style sheet contains *any* ``QTableView::item`` rule -- the sheet
    takes over the item background. Ours styles ``::item`` for padding, which was
    quietly discarding the amber "needs attention" and red "verbatim mismatch"
    tints: the table's whole point is that a flagged row is impossible to miss,
    and it was rendering identically to a clean one.

    Painting the tint here is independent of the style sheet. Selected rows keep
    the selection colour so the cursor is never ambiguous.
    """

    def paint(self, painter, option, index) -> None:  # noqa: ANN001 - Qt signature
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if not selected:
            tint = index.data(Qt.ItemDataRole.BackgroundRole)
            if isinstance(tint, (QColor, QBrush)):
                painter.save()
                painter.fillRect(option.rect, tint)
                painter.restore()
        super().paint(painter, option, index)


class QuestionTableModel(QAbstractTableModel):
    """Table model over a live ``list[Question]``.

    The model owns the *edit semantics*, not just the display: every mutation
    goes through :meth:`set_stem` / :meth:`set_answer_label` /
    :meth:`set_option_text` so that "stamp Parser.MANUAL and clear the warning
    this fixes" can never be forgotten by a caller.
    """

    COL_NUMBER = 0
    COL_STEM = 1
    COL_OPTIONS = 2
    COL_ANSWER = 3
    COL_ORIGIN = 4
    COL_CONFIDENCE = 5
    COL_FLAGS = 6
    COL_SOURCE = 7
    COLUMN_COUNT = 8

    HEADERS: tuple[str, ...] = (
        "#",
        "Stem",
        "Options",
        "Answer",
        "Origin",
        "Confidence",
        "Flags",
        "Source",
    )

    #: An edit the model refused (e.g. a letter that is not an option label).
    editRejected = Signal(str)
    #: A source row whose Question was mutated by the user.
    questionEdited = Signal(int)

    def __init__(
        self,
        questions: Sequence[Question] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._questions: list[Question] = list(questions or [])

    # -- data plumbing ----------------------------------------------------- #

    @property
    def questions(self) -> list[Question]:
        """The live working set (same objects the caller handed us)."""
        return self._questions

    def set_questions(self, questions: Sequence[Question]) -> None:
        self.beginResetModel()
        self._questions = list(questions)
        self.endResetModel()

    def question_at(self, row: int) -> Question | None:
        if 0 <= row < len(self._questions):
            return self._questions[row]
        return None

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._questions)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return self.COLUMN_COUNT

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
            return None
        return section + 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() in (self.COL_STEM, self.COL_ANSWER):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def counts(self) -> ReviewCounts:
        total = len(self._questions)
        attention = sum(1 for q in self._questions if q.needs_attention)
        ai = sum(
            1
            for q in self._questions
            if q.answer.origin is AnswerOrigin.AI or q.has(Flag.AI_ANSWER)
        )
        mismatch = sum(
            1
            for q in self._questions
            if not q.verbatim_ok or q.has(Flag.VERBATIM_MISMATCH)
        )
        return ReviewCounts(total, attention, ai, mismatch)

    # -- display ----------------------------------------------------------- #

    def data(  # noqa: C901 - one dispatch per role reads better than 6 methods
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        q = self.question_at(index.row())
        if q is None:
            return None
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(q, col)
        if role == Qt.ItemDataRole.EditRole:
            if col == self.COL_STEM:
                return q.stem
            if col == self.COL_ANSWER:
                return q.answer.label or ""
            return self._display(q, col)
        if role == Qt.ItemDataRole.UserRole:
            return self._sort_key(q, col)
        if role == Qt.ItemDataRole.BackgroundRole:
            # Red beats amber: a verbatim drift is the one thing that breaks the
            # user's core promise that wording is preserved.
            if not q.verbatim_ok or q.has(Flag.VERBATIM_MISMATCH):
                return RED_TINT
            if q.needs_attention:
                return AMBER_TINT
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(q, col)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (self.COL_NUMBER, self.COL_OPTIONS, self.COL_CONFIDENCE):
                return int(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _display(self, q: Question, col: int) -> str:
        if col == self.COL_NUMBER:
            return str(q.number)
        if col == self.COL_STEM:
            return _elide(q.stem, STEM_ELIDE_CHARS) or "(empty)"
        if col == self.COL_OPTIONS:
            return _options_summary(q)
        if col == self.COL_ANSWER:
            return _answer_display(q)
        if col == self.COL_ORIGIN:
            return _answer_origin_text(q)
        if col == self.COL_CONFIDENCE:
            return f"{round(_effective_confidence(q) * 100)}%"
        if col == self.COL_FLAGS:
            return " · ".join(flag_chip(f) for f in q.flags)
        if col == self.COL_SOURCE:
            return _basename(q.source_path)
        return ""

    def _sort_key(self, q: Question, col: int) -> object:
        if col == self.COL_NUMBER:
            return q.number
        if col == self.COL_STEM:
            return _collapse(q.stem).casefold()
        if col == self.COL_OPTIONS:
            return len(q.options)
        if col == self.COL_ANSWER:
            return q.answer.label or "￿"  # unanswered sorts last
        if col == self.COL_ORIGIN:
            return {"Source": 0, "Manual": 1, "AI": 2, "-": 3}.get(
                _answer_origin_text(q), 4
            )
        if col == self.COL_CONFIDENCE:
            return _effective_confidence(q)
        if col == self.COL_FLAGS:
            return len(q.flags)
        if col == self.COL_SOURCE:
            return _basename(q.source_path).casefold()
        return ""

    def _tooltip(self, q: Question, col: int) -> str | None:
        if col == self.COL_FLAGS:
            sentences = flags_plain_english(q.flags)
            if not sentences:
                return "No warnings on this question."
            head = _plural(len(sentences), "warning") + " on this question:"
            return head + "\n\n" + "\n\n".join(
                f"• {s}" for s in sentences
            )
        if col == self.COL_STEM:
            return q.stem or "(empty stem)"
        if col == self.COL_ANSWER:
            if not q.answer.label:
                return "No correct answer is marked for this question."
            opt = q.option(q.answer.label)
            body = q.answer.text or (opt.text if opt is not None else "")
            parts = [f"{q.answer.label}) {body}".strip()]
            parts.append(f"Provenance: {_answer_origin_text(q)}")
            if q.answer.rationale.strip():
                parts.append(f"AI reasoning: {q.answer.rationale.strip()}")
            return "\n".join(parts)
        if col == self.COL_ORIGIN:
            return {
                "Source": "The document itself stated this answer.",
                "Manual": "You set this answer by hand in this screen.",
                "AI": (
                    "The AI worked this answer out; the file did not state it. "
                    "Please verify."
                ),
                "-": "No answer is known for this question.",
            }.get(_answer_origin_text(q))
        if col == self.COL_CONFIDENCE:
            return (
                f"Parse confidence {round(float(q.confidence) * 100)}% · "
                f"answer confidence {round(float(q.answer.confidence) * 100)}%"
            )
        if col == self.COL_SOURCE:
            return q.source_path or None
        return None

    # -- editing ----------------------------------------------------------- #

    def setData(
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        text = "" if value is None else str(value)
        if index.column() == self.COL_STEM:
            return self.set_stem(index.row(), text)
        if index.column() == self.COL_ANSWER:
            return self.set_answer_label(index.row(), text)
        return False

    # The edit semantics themselves -- stamp MANUAL, clear the flag the edit
    # fixes, keep the answer body in step with its option -- live in
    # `app.ui.word_doc_model`, shared with the Word preview screen, so the two
    # screens can never treat the same edit differently.

    def set_stem(self, row: int, text: str) -> bool:
        """Replace the stem with the user's own wording."""
        q = self.question_at(row)
        if q is None or not edits.edit_stem(q, text):
            return False
        self._emit_row(row)
        return True

    def set_option_text(self, row: int, opt_index: int, text: str) -> bool:
        """Replace one option's body text (label prefixes are never re-added)."""
        q = self.question_at(row)
        if q is None or not edits.edit_option_text(q, opt_index, text):
            return False
        self._emit_row(row)
        return True

    def set_answer_label(self, row: int, raw: str) -> bool:
        """Mark `raw` as the correct option, or clear the answer when blank.

        Rejects anything that is not one of *this* question's option labels: a
        wrong key silently accepted here would be worse than no key at all.
        """
        q = self.question_at(row)
        if q is None:
            return False
        changed, error = edits.set_answer_label(q, raw)
        if error:
            self.editRejected.emit(error)
            return False
        if not changed:
            return False
        self._emit_row(row)
        return True

    def _emit_row(self, row: int) -> None:
        left = self.index(row, 0)
        right = self.index(row, self.COLUMN_COUNT - 1)
        self.dataChanged.emit(left, right)
        self.questionEdited.emit(row)


# --------------------------------------------------------------------------- #
# Filter proxy
# --------------------------------------------------------------------------- #


class QuestionFilterProxy(QSortFilterProxyModel):
    """Mode + free-text filter over :class:`QuestionTableModel`.

    ``dynamicSortFilter`` is deliberately **off**: when the user fixes a question
    while "Needs attention" is selected, the row losing its amber tint in place
    is far kinder than the row vanishing from under the cursor mid-edit.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = FILTER_ALL
        self._needle = ""
        self.setDynamicSortFilter(False)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        needle = _collapse(text).casefold()
        if needle == self._needle:
            return
        self._needle = needle
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, QuestionTableModel):
            return True
        q = model.question_at(row)
        if q is None:
            return False
        if self._needle and self._needle not in _collapse(q.stem).casefold():
            return False
        if self._mode == FILTER_ATTENTION:
            return q.needs_attention
        if self._mode == FILTER_AI:
            return q.answer.origin is AnswerOrigin.AI or q.has(Flag.AI_ANSWER)
        if self._mode == FILTER_MISMATCH:
            return not q.verbatim_ok or q.has(Flag.VERBATIM_MISMATCH)
        return True


# --------------------------------------------------------------------------- #
# Detail-pane helpers
# --------------------------------------------------------------------------- #


class _CommitPlainTextEdit(QPlainTextEdit):
    """Multi-line box that commits when focus leaves it.

    The stem often contains real line breaks (numbered sub-items), which a
    QLineEdit would destroy, so the detail pane edits it as plain text.
    """

    committed = Signal(str)

    def focusOutEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().focusOutEvent(event)  # type: ignore[arg-type]
        self.committed.emit(self.toPlainText())


def _clear_layout(host: QWidget) -> None:
    """Delete every child widget of `host`'s layout (used before a rebuild)."""
    layout = host.layout()
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _thumbnail(asset: ImageAsset) -> QWidget:
    """A scaled preview of `asset`, or a note when the bytes are unreadable."""
    label = QLabel()
    pixmap = QPixmap()
    if asset.data and pixmap.loadFromData(asset.data):
        label.setPixmap(
            pixmap.scaled(
                THUMB_PX,
                THUMB_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        label.setToolTip(f"{asset.width}×{asset.height} {asset.fmt.upper()}")
    else:
        label.setText("image could not be shown")
        label.setStyleSheet(f"color:{WARNING};")
        log.warning("could not decode a %s image for the review preview", asset.fmt)
    label.setFrameShape(QFrame.Shape.StyledPanel)
    return label


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #


class ReviewTable(QWidget):
    """Table + detail pane + export bar over the parsed question set."""

    #: "word" or "pptx".
    exportRequested = Signal(str)
    backRequested = Signal()
    #: Emitted after any user edit, so main.py knows the working set moved.
    questionsChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QuestionTableModel(parent=self)
        self._proxy = QuestionFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        #: Source row the detail pane is showing. Edits commit against this
        #: rather than the current selection, because a focus-out commit fires
        #: *before* the selection has moved to the row the user just clicked.
        self._detail_row = -1
        self._suspend_detail_edits = False
        self._option_edits: list[QLineEdit] = []
        self._option_radios: list[QRadioButton] = []
        self._option_marks: list[QLabel] = []

        self._build_ui()

        self._model.dataChanged.connect(self._on_data_changed)
        self._model.modelReset.connect(self._on_model_reset)
        self._model.editRejected.connect(self._show_notice)

        self._refresh_summary()
        self._rebuild_detail()

    # -- construction ------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        heading = QLabel("Review & Export")
        heading.setStyleSheet("font-size:20px; font-weight:700;")
        root.addWidget(heading)

        subtitle = QLabel(
            "Amber rows need a human. Red rows do not match the source text. "
            "Double-click a cell, or use the panel below, to fix anything."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size:12px;")
        root.addWidget(subtitle)

        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(BANNER_QSS)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        root.addLayout(self._build_filter_row())

        self._notice = QLabel("")
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet(f"color:{DANGER}; font-weight:600;")
        self._notice.setVisible(False)
        root.addWidget(self._notice)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_table())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # Rows are compact now, so the list needs less height than the editor
        # below it -- the editor is where the actual fixing happens, and it was
        # clipping its own option rows at the old split.
        splitter.setSizes([420, 420])
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        root.addLayout(self._build_footer())

    def _build_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        row.addWidget(QLabel("Show"))
        self._filter_combo = QComboBox()
        for label, key in FILTER_CHOICES:
            self._filter_combo.addItem(label, key)
        self._filter_combo.setToolTip(
            "Narrow the list to the questions that still need your attention."
        )
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self._filter_combo)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search question text…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._proxy.set_search)
        self._search.textChanged.connect(lambda _t: self._after_filter_change())
        row.addWidget(self._search, 1)

        return row

    def _build_table(self) -> QTableView:
        view = QTableView()
        view.setModel(self._proxy)
        # See _RowTintDelegate: without this the amber/red row tints are silently
        # discarded, which is the table's most important signal.
        self._tint_delegate = _RowTintDelegate(view)
        view.setItemDelegate(self._tint_delegate)
        view.setAlternatingRowColors(True)
        view.setSortingEnabled(True)
        view.setWordWrap(False)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        view.verticalHeader().setVisible(False)
        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            QuestionTableModel.COL_STEM, QHeaderView.ResizeMode.Stretch
        )
        header.setHighlightSections(False)
        view.sortByColumn(
            QuestionTableModel.COL_NUMBER, Qt.SortOrder.AscendingOrder
        )
        selection = view.selectionModel()
        if selection is not None:
            selection.selectionChanged.connect(lambda *_: self._rebuild_detail())
        self._view = view
        return view

    def _build_detail(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(2, 6, 2, 6)
        col.setSpacing(6)

        self._d_title = QLabel("No question selected")
        self._d_title.setStyleSheet("font-size:15px; font-weight:700;")
        self._d_title.setWordWrap(True)
        col.addWidget(self._d_title)

        col.addWidget(self._section_label("Question text"))
        self._d_stem = _CommitPlainTextEdit()
        self._d_stem.setMaximumHeight(96)
        self._d_stem.setPlaceholderText("No question selected")
        self._d_stem.committed.connect(self._commit_stem)
        col.addWidget(self._d_stem)

        col.addWidget(
            self._section_label(
                "Options — tick the radio button of the correct answer"
            )
        )
        self._d_options_host = QWidget()
        options_layout = QVBoxLayout(self._d_options_host)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(4)
        col.addWidget(self._d_options_host)

        self._d_answer = QLabel("")
        self._d_answer.setWordWrap(True)
        self._d_answer.setStyleSheet("font-weight:700;")
        col.addWidget(self._d_answer)

        self._d_rationale = QLabel("")
        self._d_rationale.setWordWrap(True)
        self._d_rationale.setStyleSheet("font-style:italic;")
        self._d_rationale.setVisible(False)
        col.addWidget(self._d_rationale)

        self._d_extras = QLabel("")
        self._d_extras.setWordWrap(True)
        self._d_extras.setStyleSheet("font-size:12px;")
        col.addWidget(self._d_extras)

        col.addWidget(self._section_label("What to check"))
        self._d_flags = QLabel("")
        self._d_flags.setWordWrap(True)
        self._d_flags.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        col.addWidget(self._d_flags)

        self._d_images_host = QWidget()
        images_layout = QHBoxLayout(self._d_images_host)
        images_layout.setContentsMargins(0, 0, 0, 0)
        images_layout.setSpacing(8)
        images_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        col.addWidget(self._d_images_host)

        col.addStretch(1)
        scroll.setWidget(body)
        return scroll

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size:11px; font-weight:700; letter-spacing:1px;")
        return label

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._footer = QLabel("")
        self._footer.setStyleSheet("font-size:12px;")
        row.addWidget(self._footer, 1)

        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self.backRequested.emit)
        row.addWidget(self._btn_back)

        self._btn_word = QPushButton("Word preview & export")
        _set_primary(self._btn_word)
        self._btn_word.setToolTip(
            "See the box-format document as it will be written, change it, then export"
        )
        self._btn_word.clicked.connect(lambda: self.exportRequested.emit("word"))
        row.addWidget(self._btn_word)

        self._btn_pptx = QPushButton("Export PowerPoint")
        _set_primary(self._btn_pptx)
        self._btn_pptx.setToolTip("Build the branded slide deck")
        self._btn_pptx.clicked.connect(lambda: self.exportRequested.emit("pptx"))
        row.addWidget(self._btn_pptx)

        return row

    # -- public API -------------------------------------------------------- #

    def set_questions(self, questions: list[Question]) -> None:
        """Push a working set in. The Question objects are used, not copied."""
        self._model.set_questions(questions)

    def questions(self) -> list[Question]:
        """Pull the working set out, including every edit made on this screen."""
        return list(self._model.questions)

    def model(self) -> QuestionTableModel:
        """The underlying model (handy for tests and for main.py's export step)."""
        return self._model

    def selected_question(self) -> Question | None:
        return self._model.question_at(self._detail_row)

    def set_filter(self, mode: str) -> None:
        """Programmatically switch the filter; unknown modes fall back to All."""
        index = self._filter_combo.findData(mode)
        self._filter_combo.setCurrentIndex(index if index >= 0 else 0)

    def visible_count(self) -> int:
        """Rows currently passing the filter -- the number the user can see."""
        return self._proxy.rowCount()

    # -- filter / summary -------------------------------------------------- #

    def _on_filter_changed(self, _index: int) -> None:
        self._proxy.set_mode(self._filter_combo.currentData() or FILTER_ALL)
        self._after_filter_change()

    def _after_filter_change(self) -> None:
        # Edits live on the Question objects, so filtering never loses them; only
        # the selection can go stale when the selected row is filtered out.
        self._clear_notice()
        if self._current_source_row() != self._detail_row:
            self._rebuild_detail()

    def _refresh_summary(self) -> None:
        counts = self._model.counts()
        self._footer.setText(
            f"{_plural(counts.total, 'question')} · "
            f"{counts.attention} need attention · "
            f"{counts.ai} AI-derived answers"
        )
        has_export = counts.total > 0
        self._btn_word.setEnabled(has_export)
        self._btn_pptx.setEnabled(has_export)
        if counts.mismatch:
            self._banner.setText(
                f"{counts.mismatch} question(s) do not match the source text "
                "exactly. Review before exporting."
            )
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)

    def _show_notice(self, message: str) -> None:
        # A rejected edit must be visible without a modal: modals interrupt the
        # review rhythm, and this is information, not a decision.
        self._notice.setText(message)
        self._notice.setVisible(True)
        log.info("review edit rejected: %s", message)

    def _clear_notice(self) -> None:
        if self._notice.isVisible():
            self._notice.clear()
            self._notice.setVisible(False)

    # -- selection / detail pane ------------------------------------------- #

    def _current_source_row(self) -> int:
        selection = self._view.selectionModel()
        if selection is None:
            return -1
        index = selection.currentIndex()
        if not index.isValid() or not selection.isSelected(index):
            rows = selection.selectedRows()
            if not rows:
                return -1
            index = rows[0]
        return self._proxy.mapToSource(index).row()

    def _on_model_reset(self) -> None:
        self._refresh_summary()
        if self._proxy.rowCount() and self._view.selectionModel() is not None:
            self._view.selectRow(0)
        self._rebuild_detail()

    def _on_data_changed(self, *_args: object) -> None:
        self._refresh_summary()
        self._refresh_detail_texts()
        self.questionsChanged.emit()

    def _rebuild_detail(self) -> None:
        """Rebuild the parts of the pane that depend on *which* question is shown."""
        row = self._current_source_row()
        self._detail_row = row
        q = self._model.question_at(row)

        self._suspend_detail_edits = True
        try:
            _clear_layout(self._d_options_host)
            _clear_layout(self._d_images_host)
            self._option_edits.clear()
            self._option_radios.clear()
            self._option_marks.clear()

            self._d_stem.setEnabled(q is not None)
            self._d_stem.setPlainText(q.stem if q is not None else "")

            if q is None:
                empty = QLabel("Select a question above to review it.")
                empty.setStyleSheet("font-size:12px;")
                self._d_options_host.layout().addWidget(empty)
            else:
                self._build_option_rows(q)
                self._build_images(q)
        finally:
            self._suspend_detail_edits = False

        self._refresh_detail_texts()

    def _build_option_rows(self, q: Question) -> None:
        host_layout = self._d_options_host.layout()
        if not q.options:
            empty = QLabel(
                "No options were detected. Fix the source file, or re-run with AI "
                "for a better split."
            )
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color:{WARNING};")
            host_layout.addWidget(empty)
            return

        # One container so the radios are siblings: Qt's auto-exclusivity works
        # per parent widget, and rebuilding drops the whole group cleanly.
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)

        for i, opt in enumerate(q.options):
            radio = QRadioButton(opt.label)
            radio.setToolTip(f"Mark option {opt.label} as the correct answer")
            radio.clicked.connect(
                lambda _checked=False, label=opt.label: self._on_option_marked(label)
            )
            grid.addWidget(radio, i, 0)

            edit = QLineEdit(opt.text)
            edit.setToolTip(
                "Edit this option's wording. Only your own typing changes the "
                "text -- nothing is rewritten automatically."
            )
            edit.editingFinished.connect(
                lambda index=i: self._commit_option(index)
            )
            grid.addWidget(edit, i, 1)

            mark = QLabel("")
            mark.setStyleSheet(f"color:{OK_GREEN}; font-weight:700;")
            grid.addWidget(mark, i, 2)

            self._option_radios.append(radio)
            self._option_edits.append(edit)
            self._option_marks.append(mark)

        host_layout.addWidget(container)

    def _build_images(self, q: Question) -> None:
        host_layout = self._d_images_host.layout()
        assets: list[tuple[str, ImageAsset]] = [("", a) for a in q.images]
        for opt in q.options:
            assets.extend((f"Option {opt.label}", a) for a in opt.images)
        if not assets:
            self._d_images_host.setVisible(False)
            return
        self._d_images_host.setVisible(True)
        for caption, asset in assets:
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(_thumbnail(asset))
            if caption:
                tag = QLabel(caption)
                tag.setStyleSheet("font-size:11px;")
                cell_layout.addWidget(tag)
            cell.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            host_layout.addWidget(cell)

    def _refresh_detail_texts(self) -> None:
        """Update the labels that depend on the question's *current* values.

        Deliberately does not touch the option editors: this runs after every
        edit, and rebuilding the editors would steal focus mid-typing.
        """
        q = self._model.question_at(self._detail_row)
        if q is None:
            self._d_title.setText("No question selected")
            self._d_answer.setText("")
            self._d_rationale.setVisible(False)
            self._d_extras.setText("")
            self._d_flags.setText("")
            return

        bits = [f"Question {q.number}"]
        name = _basename(q.source_path)
        if name:
            bits.append(name)
        lines = q.all_lines
        if lines:
            bits.append(f"source lines {lines[0]}–{lines[-1]}")
        self._d_title.setText("  ·  ".join(bits))

        # The stem box is only refreshed when the user is not typing in it.
        if not self._d_stem.hasFocus() and self._d_stem.toPlainText() != q.stem:
            self._suspend_detail_edits = True
            try:
                self._d_stem.setPlainText(q.stem)
            finally:
                self._suspend_detail_edits = False

        origin = _answer_origin_text(q)
        if q.answer.label:
            body = q.answer.text or ""
            if not body:
                opt = q.option(q.answer.label)
                body = opt.text if opt is not None else ""
            conf = round(_effective_confidence(q) * 100)
            text = f"Answer: {q.answer.label}) {body}    [{origin}, {conf}%]"
            if origin == "AI":
                text += "  — AI-derived, please verify"
                colour = WARNING
            else:
                colour = OK_GREEN
        else:
            text = "Answer: not determined — tick the correct option above"
            colour = DANGER
        self._d_answer.setText(text)
        self._d_answer.setStyleSheet(f"color:{colour}; font-weight:700;")

        rationale = q.answer.rationale.strip()
        self._d_rationale.setVisible(bool(rationale))
        if rationale:
            self._d_rationale.setText(f"AI reasoning: {rationale}")

        extras: list[str] = [f"Parsed by: {q.parser.value}"]
        if q.images:
            extras.append(_plural(len(q.images), "image"))
        if q.tables:
            extras.append(_plural(len(q.tables), "table"))
        if q.explanation.strip():
            extras.append("explanation present")
        if q.notes.strip():
            extras.append(f"notes: {q.notes.strip()}")
        self._d_extras.setText("  ·  ".join(extras))

        sentences = flags_plain_english(q.flags)
        if sentences:
            self._d_flags.setText("\n".join(f"• {s}" for s in sentences))
            self._d_flags.setStyleSheet(f"color:{WARNING};")
        else:
            self._d_flags.setText(
                "Nothing flagged — this question matches the source text and "
                "has a known answer."
            )
            self._d_flags.setStyleSheet("")

        self._sync_option_marks(q)

    def _sync_option_marks(self, q: Question) -> None:
        want = (q.answer.label or "").upper()
        self._suspend_detail_edits = True
        try:
            for i, opt in enumerate(q.options):
                if i >= len(self._option_radios):
                    break
                correct = bool(want) and opt.label.upper() == want
                radio = self._option_radios[i]
                if radio.isChecked() != correct:
                    # setChecked does not emit clicked(), so no edit is triggered.
                    radio.setChecked(correct)
                self._option_marks[i].setText("correct" if correct else "")
        finally:
            self._suspend_detail_edits = False

    # -- detail-pane edits ------------------------------------------------- #

    def _commit_stem(self, text: str) -> None:
        if self._suspend_detail_edits:
            return
        if self._model.set_stem(self._detail_row, text):
            self._clear_notice()

    def _commit_option(self, opt_index: int) -> None:
        if self._suspend_detail_edits:
            return
        if not 0 <= opt_index < len(self._option_edits):
            return
        text = self._option_edits[opt_index].text()
        if self._model.set_option_text(self._detail_row, opt_index, text):
            self._clear_notice()

    def _on_option_marked(self, label: str) -> None:
        if self._suspend_detail_edits:
            return
        if self._model.set_answer_label(self._detail_row, label):
            self._clear_notice()
