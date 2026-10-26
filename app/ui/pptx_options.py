"""Pre-flight panel: choose what goes into the deck before it is built.

Opens whenever a PowerPoint is about to be generated -- from the action menu or
from the review table's Export button -- so "what should be on the slides" is a
decision made per deck rather than a preference buried in Settings.

Nothing here changes question wording. The tick boxes only decide which parts of
each question are placed on a slide.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.models import PptxExportConfig
from app.ui import theme

_LOG = logging.getLogger(__name__)

#: Shown in the live preview so the effect of each choice is visible before the
#: deck is built. Deliberately generic -- it stands in for a real question.
_SAMPLE_STEM = "A 40-year-old man reports hearing voices when he turns on the shower."
_SAMPLE_OPTS = ("Functional hallucinations", "Hypnagogic hallucinations")


class PptxOptionsDialog(QDialog):
    """Tick boxes for the contents of each question slide.

    Construct with the current `PptxExportConfig`; read the chosen one back from
    `config()` after `exec()` returns `Accepted`. The config passed in is never
    mutated, so Cancel genuinely cancels.
    """

    def __init__(self, cfg: PptxExportConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = copy.deepcopy(cfg)
        self.setWindowTitle("PowerPoint contents")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._build()
        self._load(self._cfg)
        self._refresh_preview()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(22, 20, 22, 18)
        column.setSpacing(14)

        heading = QLabel("What should each slide show?", self)
        heading.setObjectName("h2")
        column.addWidget(heading)

        sub = QLabel(
            "Tick what you want in the deck. Wording is never changed — these "
            "only decide what gets placed on the slides.",
            self,
        )
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        column.addWidget(sub)

        column.addWidget(self._build_contents_card())
        column.addWidget(self._build_prefix_card())
        column.addWidget(self._build_deck_card())
        column.addWidget(self._build_preview_card())

        self._warning = QLabel("", self)
        self._warning.setObjectName("notice")
        self._warning.setProperty("level", "error")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        column.addWidget(self._warning)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Build deck")
        # set_variant, not a bare setProperty: a dynamic-property selector only
        # takes effect after a style re-polish (see app.ui.theme).
        theme.set_variant(ok, "primary")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        column.addWidget(self._buttons)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(self)
        card.setProperty("card", True)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 13, 16, 14)
        inner.setSpacing(8)
        label = QLabel(title, card)
        label.setObjectName("cardTitle")
        inner.addWidget(label)
        return card, inner

    def _build_contents_card(self) -> QFrame:
        card, inner = self._card("On the question slide")

        self.chk_number = QCheckBox("Question number or “Q” before the question", card)
        self.chk_number.setToolTip(
            "Adds a label in front of the stem. Choose which form below."
        )
        self.chk_text = QCheckBox("Question text", card)
        self.chk_options = QCheckBox("Options (A, B, C, D)", card)
        self.chk_after_images = QCheckBox(
            "Images placed after the options (answer / explanation area)", card
        )
        self.chk_after_images.setToolTip(
            "Images between the question text and the options, and picture "
            "options, are always included. This only decides the images that "
            "come after the last option in the source file."
        )

        for chk in (self.chk_number, self.chk_text, self.chk_options,
                    self.chk_after_images):
            chk.toggled.connect(self._on_change)
            inner.addWidget(chk)
        return card

    def _build_prefix_card(self) -> QFrame:
        card, inner = self._card("Label style")
        self._prefix_group = QButtonGroup(self)

        self.rad_number = QRadioButton("Numbered — “Q1.”, “Q2.”, “Q3.” …", card)
        self.rad_q = QRadioButton("Just “Q.” on every slide", card)
        for i, rad in enumerate((self.rad_number, self.rad_q)):
            self._prefix_group.addButton(rad, i)
            rad.toggled.connect(self._on_change)
            inner.addWidget(rad)

        self._prefix_hint = QLabel(
            "Untick the label above to show no prefix at all.", card
        )
        self._prefix_hint.setObjectName("cardDesc")
        self._prefix_hint.setWordWrap(True)
        inner.addWidget(self._prefix_hint)
        return card

    def _build_deck_card(self) -> QFrame:
        card, inner = self._card("Answers and extra slides")

        self.chk_answer = QCheckBox("Correct answer", card)
        self.chk_answer.toggled.connect(self._on_change)
        inner.addWidget(self.chk_answer)

        row = QHBoxLayout()
        row.setContentsMargins(22, 0, 0, 0)
        row.setSpacing(14)
        self._answer_group = QButtonGroup(self)
        self.rad_next_page = QRadioButton("On the next slide", card)
        self.rad_same_page = QRadioButton("As a banner on the question slide", card)
        for i, rad in enumerate((self.rad_next_page, self.rad_same_page)):
            self._answer_group.addButton(rad, i)
            rad.toggled.connect(self._on_change)
            row.addWidget(rad)
        row.addStretch(1)
        inner.addLayout(row)

        self.chk_title = QCheckBox("Title slide (logo, subject, faculty)", card)
        self.chk_thanks = QCheckBox("“Thank you” slide at the end", card)
        for chk in (self.chk_title, self.chk_thanks):
            chk.toggled.connect(self._on_change)
            inner.addWidget(chk)
        return card

    def _build_preview_card(self) -> QFrame:
        card, inner = self._card("Preview")
        self._preview = QLabel("", card)
        self._preview.setObjectName("cardDesc")
        self._preview.setWordWrap(True)
        self._preview.setTextFormat(Qt.TextFormat.PlainText)
        inner.addWidget(self._preview)
        return card

    # -- state -------------------------------------------------------------- #

    def _load(self, cfg: PptxExportConfig) -> None:
        style = (cfg.question_prefix or "number").strip().lower()
        self.chk_number.setChecked(style != "none")
        self.rad_q.setChecked(style == "q")
        self.rad_number.setChecked(style != "q")

        self.chk_text.setChecked(bool(cfg.include_question_text))
        self.chk_options.setChecked(bool(cfg.include_options))
        self.chk_after_images.setChecked(bool(cfg.include_images_after_options))
        self.chk_answer.setChecked(bool(cfg.include_answers))
        self.rad_next_page.setChecked(bool(cfg.answer_on_separate_slide))
        self.rad_same_page.setChecked(not cfg.answer_on_separate_slide)
        self.chk_title.setChecked(bool(cfg.title_slide))
        self.chk_thanks.setChecked(bool(cfg.thankyou_slide))

    def _on_change(self, _checked: bool = False) -> None:
        labelled = self.chk_number.isChecked()
        self.rad_number.setEnabled(labelled)
        self.rad_q.setEnabled(labelled)
        self._prefix_hint.setVisible(labelled)

        answered = self.chk_answer.isChecked()
        self.rad_next_page.setEnabled(answered)
        self.rad_same_page.setEnabled(answered)

        # An entirely blank question slide is never what someone meant, so say so
        # and block OK rather than silently producing 60 empty slides.
        empty = not (labelled or self.chk_text.isChecked() or self.chk_options.isChecked())
        self._warning.setText(
            "The question slides would be completely blank. Tick at least one of "
            "the three items above."
            if empty
            else ""
        )
        self._warning.setVisible(empty)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not empty)
        theme.repolish(self._warning)

        self._refresh_preview()

    def _refresh_preview(self) -> None:
        lines: list[str] = []
        head = ""
        if self.chk_number.isChecked():
            head = "Q." if self.rad_q.isChecked() else "Q1."
        if self.chk_text.isChecked():
            head = f"{head} {_SAMPLE_STEM}".strip()
        lines.append(f"Slide 1:  {head}" if head else "Slide 1:  (no question heading)")
        if self.chk_options.isChecked():
            for letter, body in zip("AB", _SAMPLE_OPTS):
                lines.append(f"          {letter}. {body}")
            lines.append("          C. …   D. …")
        lines.append(
            "          [ images: question + options + after options ]"
            if self.chk_after_images.isChecked()
            else "          [ images: question + options only ]"
        )
        if self.chk_answer.isChecked():
            if self.rad_same_page.isChecked():
                lines.append("          [ Answer: A. Functional hallucinations ]")
            else:
                lines.append("Slide 2:  Answer: A. Functional hallucinations")
        self._preview.setText("\n".join(lines))

    # -- result ------------------------------------------------------------- #

    def config(self) -> PptxExportConfig:
        """The chosen settings. Only meaningful once the dialog was accepted."""
        if self.chk_number.isChecked():
            style = "q" if self.rad_q.isChecked() else "number"
        else:
            style = "none"
        return replace(
            self._cfg,
            question_prefix=style,
            include_question_text=self.chk_text.isChecked(),
            include_options=self.chk_options.isChecked(),
            include_images_after_options=self.chk_after_images.isChecked(),
            include_answers=self.chk_answer.isChecked(),
            answer_on_separate_slide=self.rad_next_page.isChecked(),
            title_slide=self.chk_title.isChecked(),
            thankyou_slide=self.chk_thanks.isChecked(),
        )
