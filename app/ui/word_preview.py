"""Word document preview & edit screen -- what "Extract & Format MCQs" lands on.

The PowerPoint action asks what goes on the slides before a deck is written;
this is the same step for the Word file, with the document itself on screen.
Three panes:

* **Questions** -- the list, in the order the file will have. Tick a question
  out of the export, move it up or down.
* **Preview** -- every question drawn as the box `app.export.word_box` will
  write, with the source's bold, pictures and grids, and the house keyword
  rules applied to the explanation. Clicking a box selects it for editing.
* **Edit** -- the selected question: stem, options (add, remove, reword, tick
  the correct one), explanation, and the plain-English warnings for it.

Under them, the layout choices (answer line, explanation, numbering, font)
with the preview following every tick, and the Export Word button.

The screen holds no document logic of its own. Edits go through
`app.ui.word_doc_model` (shared with the review table), the preview is
`app.export.preview_html`, and the file is written by `main.py` from
`export_questions()` / `config()` / `needs_rebuild()`.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.export import preview_html
from app.models import ImageAsset, ParseResult, Question, WordExportConfig
from app.ui import word_doc_model as edits
from app.ui.review import _RowTintDelegate, flags_plain_english
from app.ui.word_doc_model import WordDocModel

log = logging.getLogger(__name__)

#: Semantic colours, as in the review table: amber = needs a human.
AMBER_TINT = QColor(0xE8, 0x9C, 0x1F, 62)
EXCLUDED_FG = QColor(0x9A, 0x9A, 0x9A)
WARNING = "#E89C1F"
DANGER = "#E23A3A"
OK_GREEN = "#2FBF71"

#: How much of the stem the list shows.
LIST_ELIDE_CHARS = 60

#: Fonts the Word file may use; the combo is editable for any other.
FONT_CHOICES: tuple[str, ...] = ("Calibri", "Arial", "Times New Roman", "Cambria", "Verdana")

#: Preview re-render is coalesced: a burst of ticks redraws once.
RENDER_DELAY_MS = 120


def _elide(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)].rstrip() + "…"


def _set_primary(button: QPushButton) -> None:
    try:
        from app.ui import theme

        theme.set_variant(button, "primary")
    except Exception as exc:  # noqa: BLE001 - a plain button is a fine fallback
        log.debug("primary styling unavailable: %s", exc)


class _CommitPlainTextEdit(QPlainTextEdit):
    """Multi-line box that commits when focus leaves it (as the review pane's)."""

    committed = Signal(str)

    def focusOutEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().focusOutEvent(event)  # type: ignore[arg-type]
        self.committed.emit(self.toPlainText())


def _clear_layout(host: QWidget) -> None:
    layout = host.layout()
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #


class WordPreviewScreen(QWidget):
    """Preview, edit and configure the Word export before it is written."""

    #: The user pressed Export Word. main.py reads export_questions() etc.
    exportRequested = Signal()
    backRequested = Signal()
    #: Open the classic review table over the same questions.
    reviewRequested = Signal()
    #: Emitted after any user edit, so main.py knows the working set moved.
    questionsChanged = Signal()

    def __init__(self, settings: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._model = WordDocModel()
        self._selected_uid: int | None = None
        self._suspend = False
        #: id(asset) -> resource URL, so a picture is decoded once per session.
        self._image_urls: dict[int, str] = {}
        self._option_edits: list[QLineEdit] = []
        self._option_radios: list[QRadioButton] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RENDER_DELAY_MS)
        self._render_timer.timeout.connect(self._render_now)
        self._build_ui()

    # -- construction ------------------------------------------------------ #

    def _build_ui(self) -> None:
        # Widgets are wired as they are built, and a combo box announces its
        # first item as a change; nothing may react until the whole screen exists.
        self._suspend = True
        try:
            self._build_widgets()
        finally:
            self._suspend = False

    def _build_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        heading = QLabel("Word document — preview & edit")
        heading.setStyleSheet("font-size:20px; font-weight:700;")
        root.addWidget(heading)

        subtitle = QLabel(
            "This is the document that Export Word will write. Fix wording, options "
            "or answers on the right, untick questions to leave them out, reorder "
            "them on the left, and choose the layout below. Amber boxes need a human."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size:12px;")
        root.addWidget(subtitle)

        self._notice = QLabel("")
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet(f"color:{DANGER}; font-weight:600;")
        self._notice.setVisible(False)
        root.addWidget(self._notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_list_pane())
        splitter.addWidget(self._build_preview_pane())
        splitter.addWidget(self._build_editor_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([260, 640, 420])
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        root.addWidget(self._build_options_card())
        root.addLayout(self._build_footer())

    def _build_list_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(6)
        col.addWidget(self._section_label("Questions — tick to include"))

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setAlternatingRowColors(True)
        # See review._RowTintDelegate: the stylesheet's ::item rule would
        # otherwise discard the amber "needs attention" tint on list rows.
        self._tint_delegate = _RowTintDelegate(self._list)
        self._list.setItemDelegate(self._tint_delegate)
        self._list.setMinimumWidth(200)
        self._list.currentItemChanged.connect(self._on_list_selection)
        self._list.itemChanged.connect(self._on_item_changed)
        col.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._btn_up = QPushButton("▲ Up")
        self._btn_up.setToolTip("Move the selected question up")
        self._btn_up.clicked.connect(lambda: self._move_selected(-1))
        self._btn_down = QPushButton("▼ Down")
        self._btn_down.setToolTip("Move the selected question down")
        self._btn_down.clicked.connect(lambda: self._move_selected(+1))
        row.addWidget(self._btn_up)
        row.addWidget(self._btn_down)
        col.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self._btn_all = QPushButton("Include all")
        self._btn_all.clicked.connect(lambda: self._set_all_included(True))
        self._btn_none = QPushButton("Exclude all")
        self._btn_none.clicked.connect(lambda: self._set_all_included(False))
        row2.addWidget(self._btn_all)
        row2.addWidget(self._btn_none)
        col.addLayout(row2)
        return pane

    def _build_preview_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addWidget(self._section_label("Preview — click a box to edit it"))
        self._browser = QTextBrowser()
        self._browser.setOpenLinks(False)
        self._browser.setOpenExternalLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        # Paper-white regardless of theme: it is a preview of a printed page.
        self._browser.setStyleSheet(
            "QTextBrowser { background: #FFFFFF; color: #000000; padding: 10px; }"
        )
        col.addWidget(self._browser, 1)
        return pane

    def _build_editor_pane(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(300)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(6, 0, 2, 6)
        col.setSpacing(6)

        self._e_title = QLabel("No question selected")
        self._e_title.setStyleSheet("font-size:15px; font-weight:700;")
        self._e_title.setWordWrap(True)
        col.addWidget(self._e_title)

        self._e_include = QCheckBox("Include this question in the Word file")
        self._e_include.toggled.connect(self._on_include_toggled)
        col.addWidget(self._e_include)

        col.addWidget(self._section_label("Question text"))
        self._e_stem = _CommitPlainTextEdit()
        self._e_stem.setMinimumHeight(90)
        self._e_stem.setMaximumHeight(160)
        self._e_stem.setPlaceholderText("No question selected")
        self._e_stem.committed.connect(self._commit_stem)
        col.addWidget(self._e_stem)

        col.addWidget(self._section_label("Options — tick the correct one, ✕ removes"))
        self._e_options_host = QWidget()
        host_layout = QVBoxLayout(self._e_options_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(4)
        col.addWidget(self._e_options_host)

        opt_row = QHBoxLayout()
        self._btn_add_option = QPushButton("+ Add option")
        self._btn_add_option.clicked.connect(self._add_option)
        self._btn_clear_answer = QPushButton("Clear answer")
        self._btn_clear_answer.setToolTip("Forget the correct letter (the box will say 'not determined')")
        self._btn_clear_answer.clicked.connect(self._clear_answer)
        opt_row.addWidget(self._btn_add_option)
        opt_row.addWidget(self._btn_clear_answer)
        opt_row.addStretch(1)
        col.addLayout(opt_row)

        self._e_answer = QLabel("")
        self._e_answer.setWordWrap(True)
        self._e_answer.setStyleSheet("font-weight:700;")
        col.addWidget(self._e_answer)

        col.addWidget(self._section_label("Explanation"))
        self._e_expl = _CommitPlainTextEdit()
        self._e_expl.setMinimumHeight(120)
        self._e_expl.setPlaceholderText("No explanation in the source")
        self._e_expl.committed.connect(self._commit_explanation)
        col.addWidget(self._e_expl, 1)

        col.addWidget(self._section_label("What to check"))
        self._e_flags = QLabel("")
        self._e_flags.setWordWrap(True)
        self._e_flags.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self._e_flags)

        self._e_extras = QLabel("")
        self._e_extras.setWordWrap(True)
        self._e_extras.setStyleSheet("font-size:12px;")
        col.addWidget(self._e_extras)

        col.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_options_card(self) -> QFrame:
        card = QFrame()
        card.setProperty("card", True)
        grid = QGridLayout(card)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)

        title = QLabel("Layout of every box")
        title.setObjectName("cardTitle")
        grid.addWidget(title, 0, 0, 1, 4)

        self.chk_answer = QCheckBox("Answer line (“Ans. B) …”)")
        self.chk_repeat = QCheckBox("Repeat the answer above the explanation")
        self.chk_expl = QCheckBox("Explanation")
        self.chk_mark_ai = QCheckBox("Mark AI-derived answers")
        self.chk_bold = QCheckBox("Keep the source’s bold")
        self.chk_renumber = QCheckBox("Number questions 1, 2, 3 … in this order")
        self.chk_renumber.setToolTip(
            "Untick to print the numbers the source file used, even if they "
            "restart or repeat."
        )
        for chk in (self.chk_answer, self.chk_repeat, self.chk_expl,
                    self.chk_mark_ai, self.chk_bold, self.chk_renumber):
            chk.toggled.connect(self._on_layout_changed)
        grid.addWidget(self.chk_answer, 1, 0)
        grid.addWidget(self.chk_repeat, 1, 1)
        grid.addWidget(self.chk_expl, 1, 2)
        grid.addWidget(self.chk_renumber, 1, 3)
        grid.addWidget(self.chk_bold, 2, 0)
        grid.addWidget(self.chk_mark_ai, 2, 1)

        font_row = QHBoxLayout()
        font_row.setSpacing(6)
        font_row.addWidget(QLabel("Font"))
        self.cmb_font = QComboBox()
        self.cmb_font.setEditable(True)
        for name in FONT_CHOICES:
            self.cmb_font.addItem(name)
        self.cmb_font.currentTextChanged.connect(self._on_layout_changed)
        font_row.addWidget(self.cmb_font)
        font_row.addWidget(QLabel("Size"))
        self.spin_size = QSpinBox()
        self.spin_size.setRange(8, 24)
        self.spin_size.setSuffix(" pt")
        self.spin_size.valueChanged.connect(self._on_layout_changed)
        font_row.addWidget(self.spin_size)
        font_row.addStretch(1)
        grid.addLayout(font_row, 2, 2, 1, 2)
        return card

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._footer = QLabel("")
        self._footer.setStyleSheet("font-size:12px;")
        row.addWidget(self._footer, 1)

        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self.backRequested.emit)
        row.addWidget(self._btn_back)

        self._btn_review = QPushButton("Review table")
        self._btn_review.setToolTip("The table view of the same questions, with confidence and origin")
        self._btn_review.clicked.connect(self.reviewRequested.emit)
        row.addWidget(self._btn_review)

        self._btn_export = QPushButton("Export Word")
        _set_primary(self._btn_export)
        self._btn_export.setToolTip("Write the document exactly as previewed")
        self._btn_export.clicked.connect(self.exportRequested.emit)
        row.addWidget(self._btn_export)
        return row

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size:11px; font-weight:700; letter-spacing:1px;")
        return label

    # -- public API -------------------------------------------------------- #

    def set_result(self, result: ParseResult, cfg: WordExportConfig | None = None) -> None:
        """Show a parse result. The Question objects are used, not copied."""
        self.set_questions(list(result.questions), cfg)

    def set_questions(
        self, questions: Sequence[Question], cfg: WordExportConfig | None = None
    ) -> None:
        if cfg is None:
            cfg = getattr(self._settings, "word", None) or WordExportConfig()
        self._model = WordDocModel.from_questions(questions, cfg)
        self._image_urls.clear()
        self._browser.document().clear()
        self._selected_uid = self._model.entries[0].uid if self._model.entries else None
        self._load_layout(self._model.cfg)
        self._rebuild_list()
        self._refresh_editor()
        self._render_now()

    def model(self) -> WordDocModel:
        return self._model

    def config(self) -> WordExportConfig:
        """The layout as ticked -- read after exportRequested."""
        return self._model.cfg

    def export_questions(self) -> list[Question]:
        """The included questions, in order, numbered as previewed."""
        return self._model.export_questions()

    def questions(self) -> list[Question]:
        """Every question, edits included (for the review table's benefit)."""
        return [e.question for e in self._model.entries]

    def needs_rebuild(self) -> bool:
        """False only when the file may still be restyled in place unchanged."""
        return self._model.needs_rebuild()

    def refresh(self) -> None:
        """Redraw after another screen (the review table) edited the questions."""
        self._rebuild_list()
        self._refresh_editor()
        self._render_now()

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)  # type: ignore[arg-type]
        if self._model.entries:
            self.refresh()

    # -- layout options ---------------------------------------------------- #

    def _load_layout(self, cfg: WordExportConfig) -> None:
        self._suspend = True
        try:
            self.chk_answer.setChecked(bool(cfg.include_answer))
            self.chk_repeat.setChecked(bool(cfg.repeat_answer_in_explanation))
            self.chk_expl.setChecked(bool(cfg.include_explanation))
            self.chk_mark_ai.setChecked(bool(cfg.mark_ai_answers))
            self.chk_bold.setChecked(bool(cfg.preserve_bold))
            self.chk_renumber.setChecked(bool(cfg.renumber))
            self.cmb_font.setCurrentText(cfg.font_name or "Calibri")
            self.spin_size.setValue(int(cfg.font_size_pt or 14))
        finally:
            self._suspend = False
        self._on_layout_changed()

    def _on_layout_changed(self, *_args: object) -> None:
        if self._suspend:
            return
        cfg = self._model.cfg
        cfg.include_answer = self.chk_answer.isChecked()
        cfg.repeat_answer_in_explanation = self.chk_repeat.isChecked()
        cfg.include_explanation = self.chk_expl.isChecked()
        cfg.mark_ai_answers = self.chk_mark_ai.isChecked()
        cfg.preserve_bold = self.chk_bold.isChecked()
        cfg.renumber = self.chk_renumber.isChecked()
        font = self.cmb_font.currentText().strip()
        if font:
            cfg.font_name = font
        cfg.font_size_pt = int(self.spin_size.value())
        self.chk_repeat.setEnabled(cfg.include_answer)
        self.chk_mark_ai.setEnabled(cfg.include_answer)
        self._rebuild_list()          # numbers may have changed
        self._refresh_summary()
        self._schedule_render()

    # -- the list ---------------------------------------------------------- #

    def _rebuild_list(self) -> None:
        self._suspend = True
        try:
            self._list.clear()
            for e in self._model.entries:
                item = QListWidgetItem(self._list_text(e))
                item.setData(Qt.ItemDataRole.UserRole, e.uid)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Checked if e.included else Qt.CheckState.Unchecked
                )
                self._decorate_item(item, e)
                self._list.addItem(item)
                if e.uid == self._selected_uid:
                    self._list.setCurrentItem(item)
        finally:
            self._suspend = False
        self._refresh_summary()

    def _list_text(self, e: edits.Entry) -> str:
        n = self._model.display_number(e.uid)
        head = f"{n}." if n else "—"
        text = f"{head}  {_elide(e.question.stem, LIST_ELIDE_CHARS) or '(empty)'}"
        return text if e.included else f"{text}   (excluded)"

    def _decorate_item(self, item: QListWidgetItem, e: edits.Entry) -> None:
        q = e.question
        sentences = flags_plain_english(q.flags)
        tip = [f"Question {self._model.display_number(e.uid) or '—'}"]
        if q.source_number:
            tip.append(f"Printed as {q.source_number} in the source")
        tip.extend(f"• {s}" for s in sentences)
        item.setToolTip("\n".join(tip))
        if not e.included:
            item.setForeground(EXCLUDED_FG)
        elif q.needs_attention:
            item.setBackground(AMBER_TINT)

    def _on_list_selection(self, current: QListWidgetItem | None, _previous: object) -> None:
        if self._suspend or current is None:
            return
        uid = current.data(Qt.ItemDataRole.UserRole)
        if uid is None or uid == self._selected_uid:
            return
        self._select(int(uid), scroll=True)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._suspend:
            return
        uid = item.data(Qt.ItemDataRole.UserRole)
        if uid is None:
            return
        included = item.checkState() == Qt.CheckState.Checked
        if self._model.set_included(int(uid), included):
            self._after_document_change()

    def _move_selected(self, delta: int) -> None:
        if self._selected_uid is None:
            return
        if self._model.move(self._selected_uid, delta):
            self._after_document_change()

    def _set_all_included(self, included: bool) -> None:
        changed = False
        for e in self._model.entries:
            changed = self._model.set_included(e.uid, included) or changed
        if changed:
            self._after_document_change()

    def _after_document_change(self) -> None:
        self._rebuild_list()
        self._refresh_editor()
        self._schedule_render()
        self.questionsChanged.emit()

    # -- selection --------------------------------------------------------- #

    def _select(self, uid: int, *, scroll: bool) -> None:
        self._selected_uid = uid
        self._suspend = True
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == uid:
                    self._list.setCurrentItem(item)
                    break
        finally:
            self._suspend = False
        self._refresh_editor()
        self._render_now()
        if scroll:
            self._browser.scrollToAnchor(f"q{uid}")

    def _on_anchor_clicked(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("edit:"):
            try:
                self._select(int(text[5:]), scroll=False)
            except ValueError:
                log.debug("ignored preview link %r", text)

    # -- preview ----------------------------------------------------------- #

    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _image_src(self, asset: ImageAsset, key: str) -> str:
        """Register the picture as a document resource once; return its URL."""
        url = self._image_urls.get(id(asset))
        if url:
            return url
        image = QImage()
        if not asset.data or not image.loadFromData(asset.data):
            log.warning("could not decode a %s image for the preview", asset.fmt)
            self._image_urls[id(asset)] = ""
            return ""
        url = f"preview://{key}"
        self._browser.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(url), image
        )
        self._image_urls[id(asset)] = url
        return url

    def _render_now(self) -> None:
        self._render_timer.stop()
        bar = self._browser.verticalScrollBar()
        position = bar.value() if bar is not None else 0
        items = [
            (e.uid, e.question, self._model.display_number(e.uid), e.included)
            for e in self._model.entries
        ]
        try:
            html = preview_html.render_document(
                items, self._model.cfg,
                selected_uid=self._selected_uid, image_src=self._image_src,
            )
        except Exception as exc:  # noqa: BLE001 - a preview bug must not lose the screen
            log.exception("preview render failed")
            html = f"<p style='color:{DANGER}'>The preview could not be drawn: {exc}</p>"
        self._browser.setHtml(html)
        if bar is not None:
            bar.setValue(position)

    # -- editor ------------------------------------------------------------ #

    def _current_entry(self) -> edits.Entry | None:
        if self._selected_uid is None:
            return None
        return self._model.entry(self._selected_uid)

    def _refresh_editor(self) -> None:
        e = self._current_entry()
        self._suspend = True
        try:
            _clear_layout(self._e_options_host)
            self._option_edits.clear()
            self._option_radios.clear()
            enabled = e is not None
            for w in (self._e_stem, self._e_expl, self._e_include, self._btn_add_option,
                      self._btn_clear_answer, self._btn_up, self._btn_down):
                w.setEnabled(enabled)
            if e is None:
                self._e_title.setText("No question selected")
                self._e_stem.setPlainText("")
                self._e_expl.setPlainText("")
                self._e_answer.setText("")
                self._e_flags.setText("")
                self._e_extras.setText("")
                return
            q = e.question
            n = self._model.display_number(e.uid)
            bits = [f"Question {n}" if n else "Excluded question"]
            if q.source_number and q.source_number != n:
                bits.append(f"printed as {q.source_number} in the source")
            self._e_title.setText("  ·  ".join(bits))
            self._e_include.setChecked(e.included)
            if not self._e_stem.hasFocus():
                self._e_stem.setPlainText(q.stem)
            if not self._e_expl.hasFocus():
                self._e_expl.setPlainText(q.explanation)
            self._build_option_rows(q)
            self._refresh_texts(q)
        finally:
            self._suspend = False

    def _build_option_rows(self, q: Question) -> None:
        host_layout = self._e_options_host.layout()
        if not q.options:
            empty = QLabel("No options were detected — add them with “+ Add option”.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color:{WARNING};")
            host_layout.addWidget(empty)
            return
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        want = (q.answer.label or "").upper()
        for i, opt in enumerate(q.options):
            radio = QRadioButton(opt.label)
            radio.setToolTip(f"Mark option {opt.label} as the correct answer")
            radio.setChecked(bool(want) and opt.label.upper() == want)
            radio.clicked.connect(
                lambda _checked=False, label=opt.label: self._mark_answer(label)
            )
            grid.addWidget(radio, i, 0)
            edit = QLineEdit(opt.text)
            edit.setToolTip("Edit this option's wording")
            edit.editingFinished.connect(lambda index=i: self._commit_option(index))
            grid.addWidget(edit, i, 1)
            remove = QPushButton("✕")
            remove.setFixedWidth(28)
            remove.setToolTip(f"Remove option {opt.label}")
            remove.clicked.connect(lambda _c=False, index=i: self._remove_option(index))
            grid.addWidget(remove, i, 2)
            self._option_radios.append(radio)
            self._option_edits.append(edit)
        host_layout.addWidget(container)

    def _refresh_texts(self, q: Question) -> None:
        if q.answer.label:
            body = q.answer.text or ""
            if not body:
                opt = q.option(q.answer.label)
                body = opt.text if opt is not None else ""
            origin = {"source": "from the file", "ai": "AI-derived — verify", "none": ""}.get(
                q.answer.origin.value, ""
            )
            if edits.MANUAL_ANSWER_NOTE in q.notes:
                origin = "set by you"
            text = f"Answer: {q.answer.label}) {_elide(body, 80)}" + (f"    [{origin}]" if origin else "")
            colour = WARNING if q.answer.origin.value == "ai" else OK_GREEN
        else:
            text = "Answer: not determined — tick the correct option above"
            colour = DANGER
        self._e_answer.setText(text)
        self._e_answer.setStyleSheet(f"color:{colour}; font-weight:700;")

        sentences = flags_plain_english(q.flags)
        if sentences:
            self._e_flags.setText("\n".join(f"• {s}" for s in sentences))
            self._e_flags.setStyleSheet(f"color:{WARNING};")
        else:
            self._e_flags.setText("Nothing flagged — this question matches the source and has a known answer.")
            self._e_flags.setStyleSheet("")
        extras = [f"Parsed by: {q.parser.value}"]
        if q.images:
            extras.append(f"{len(q.images)} image(s)")
        if q.tables:
            extras.append(f"{len(q.tables)} grid(s)")
        if q.notes.strip():
            extras.append(f"notes: {q.notes.strip()}")
        self._e_extras.setText("  ·  ".join(extras))

    # -- editor commits ---------------------------------------------------- #

    def _commit_stem(self, text: str) -> None:
        e = self._current_entry()
        if self._suspend or e is None:
            return
        if edits.edit_stem(e.question, text):
            self._after_edit(e)

    def _commit_explanation(self, text: str) -> None:
        e = self._current_entry()
        if self._suspend or e is None:
            return
        if edits.edit_explanation(e.question, text):
            self._after_edit(e)

    def _commit_option(self, index: int) -> None:
        e = self._current_entry()
        if self._suspend or e is None or not 0 <= index < len(self._option_edits):
            return
        if edits.edit_option_text(e.question, index, self._option_edits[index].text()):
            self._after_edit(e, rebuild_editor=False)

    def _mark_answer(self, label: str) -> None:
        e = self._current_entry()
        if self._suspend or e is None:
            return
        changed, error = edits.set_answer_label(e.question, label)
        if error:
            self._show_notice(error)
            return
        if changed:
            self._after_edit(e, rebuild_editor=False)
            self._refresh_texts(e.question)

    def _clear_answer(self) -> None:
        e = self._current_entry()
        if e is None:
            return
        if edits.clear_answer(e.question):
            self._after_edit(e)

    def _add_option(self) -> None:
        e = self._current_entry()
        if e is None:
            return
        if edits.add_option(e.question) is None:
            self._show_notice("A question can have at most eight options (A–H).")
            return
        self._after_edit(e)
        if self._option_edits:
            self._option_edits[-1].setFocus()

    def _remove_option(self, index: int) -> None:
        e = self._current_entry()
        if e is None:
            return
        if edits.remove_option(e.question, index):
            self._after_edit(e)

    def _on_include_toggled(self, checked: bool) -> None:
        e = self._current_entry()
        if self._suspend or e is None:
            return
        if self._model.set_included(e.uid, checked):
            self._after_document_change()

    def _after_edit(self, e: edits.Entry, *, rebuild_editor: bool = True) -> None:
        self._model.note_edit(e.uid)
        self._clear_notice()
        if rebuild_editor:
            self._refresh_editor()
        else:
            self._refresh_texts(e.question)
        self._rebuild_list()
        self._schedule_render()
        self.questionsChanged.emit()

    # -- notices / summary ------------------------------------------------- #

    def _refresh_summary(self) -> None:
        total, excluded, attention = self._model.counts()
        parts = [f"{total} question{'s' if total != 1 else ''}"]
        if excluded:
            parts.append(f"{excluded} excluded")
        parts.append(f"{attention} need attention")
        if self._model.needs_rebuild():
            parts.append("the file will be written from these questions")
        else:
            parts.append("unchanged from the source")
        self._footer.setText(" · ".join(parts))
        self._btn_export.setEnabled(total - excluded > 0)

    def _show_notice(self, message: str) -> None:
        self._notice.setText(message)
        self._notice.setVisible(True)

    def _clear_notice(self) -> None:
        if self._notice.isVisible():
            self._notice.clear()
            self._notice.setVisible(False)
