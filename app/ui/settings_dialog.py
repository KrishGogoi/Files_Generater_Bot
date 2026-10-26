"""Settings dialog: API key, provider, export house style and branding.

Three rules shape this file:

1. **The key is a secret.** It is shown masked, never logged, never put in a
   message box, and scrubbed out of any provider error text before display.
2. **Nothing blocks the GUI.** "Test connection" makes a real API call, so it
   runs through ``app.ui.worker.run_job`` on a worker thread.
3. **Cancel really cancels.** Widget state is only written back into the
   ``Settings`` object by :meth:`apply_to_settings`, which only ``accept()``
   calls.

``app.config`` / ``app.parse`` are imported lazily inside methods so that this
dialog stays importable while its sibling modules are still being written, and
so a GUI launch never pays for the provider SDKs.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import os
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QInputDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

if TYPE_CHECKING:  # pragma: no cover - import kept out of the runtime path
    from app.config import Settings

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #

ACCENT = "#1AB7EA"
DANGER = "#E23A3A"
OK_GREEN = "#2FBF71"
MUTED = "#8892A6"

GEMINI_KEY_URL = "https://aistudio.google.com/apikey"

#: (label shown, value stored in Settings.provider)
PROVIDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("Google Gemini (free)", "gemini"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Groq", "groq"),
    ("None — rule-based only", "none"),
)

#: Suggestions only: the combo stays editable so a new model name can be typed
#: the day it ships, without waiting for an app update.
MODEL_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    # Verified reachable on a fresh free-tier key, fastest-first. The whole
    # gemini-2.5-* family is deliberately absent: it 404s for new keys. Pro
    # models are omitted too -- they return 429 RESOURCE_EXHAUSTED on free tier.
    "gemini": (
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-3.6-flash",
        "gemini-3-flash-preview",
    ),
    "openai": ("gpt-4o-mini",),
    "anthropic": ("claude-sonnet-4-5",),
    "groq": ("llama-3.3-70b-versatile",),
    "none": (),
}

#: Environment variable consulted per provider when no key is typed in.
KEY_ENV_VARS: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "none": (),
}

COMMON_FONTS: tuple[str, ...] = (
    "Calibri",
    "Arial",
    "Arial Rounded MT Bold",
    "Segoe UI",
    "Times New Roman",
    "Georgia",
    "Verdana",
    "Tahoma",
)

LOGO_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif);;All files (*.*)"
LOGO_PREVIEW_PX = 96


@dataclasses.dataclass(slots=True)
class _ProbeSettings:
    """Minimal stand-in handed to ``make_provider`` for a connection test.

    Used only when ``Settings`` is not a dataclass (so ``dataclasses.replace``
    cannot copy it); it carries exactly the fields a provider factory needs, and
    it means the real ``Settings`` object is never mutated by a test click.
    """

    api_key: str = ""
    provider: str = "gemini"
    model: str = "gemini-3.1-flash-lite"
    use_ai: bool = True
    max_concurrency: int = 4


def _probe_provider(probe: Any) -> str:
    """Build the provider and make one tiny call. Runs on a worker thread."""
    from app.parse import providers  # lazy: never imported just to open settings

    provider = providers.make_provider(probe)
    if provider is None:
        raise RuntimeError(
            "No provider could be built — check the provider choice and that an "
            "API key is present."
        )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    provider.complete_json(
        "You are a connection test. Answer with JSON only.",
        'Reply with exactly {"ok": true}.',
        schema,
        timeout=30.0,
    )
    return provider.name


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


class SettingsDialog(QDialog):
    """Edit a :class:`~app.config.Settings` in place (on OK only)."""

    def __init__(self, settings: "Settings", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._brand = settings.brand
        self._word = settings.word
        self._pptx = settings.pptx
        #: Kept alive for the duration of a connection test; a QThread that goes
        #: out of scope while running takes the process down with it.
        self._test_thread: QThread | None = None

        self.setWindowTitle("Settings — Krishna's Super Bot")
        self.setMinimumWidth(700)
        self.setMinimumHeight(560)

        self._build_ui()
        self._load_from_settings()

    # -- construction ------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_ai_tab(), "AI")
        self._tabs.addTab(self._build_word_tab(), "Word output")
        self._tabs.addTab(self._build_pptx_tab(), "PowerPoint")
        # "&&" because QTabWidget treats "&" as a mnemonic marker and would
        # render "Branding _output" with the O underlined.
        self._tabs.addTab(self._build_brand_tab(), "Branding && output")
        root.addWidget(self._tabs, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Save")
            ok.setDefault(True)
            # set_variant re-polishes: a QDialogButtonBox's standard buttons are
            # already styled by the time we get them, so a bare setProperty
            # would have no visible effect.
            theme.set_variant(ok, "primary")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    # -- AI tab ------------------------------------------------------------ #

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)

        hint = QLabel(
            "Get a free Gemini API key at "
            f'<a href="{GEMINI_KEY_URL}">{GEMINI_KEY_URL}</a> — no card needed.'
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setOpenExternalLinks(True)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"border:1px solid {ACCENT}; border-radius:6px; padding:8px 10px; "
            "font-weight:600;"
        )
        col.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self._use_ai = QCheckBox("Use AI to read the questions (recommended)")
        self._use_ai.setToolTip(
            "With AI off the app still works: it falls back to the rule-based "
            "parser."
        )
        self._use_ai.toggled.connect(lambda _c: self._sync_ai_state())
        form.addRow("", self._use_ai)

        self._provider = QComboBox()
        for label, value in PROVIDER_CHOICES:
            self._provider.addItem(label, value)
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("Provider", self._provider)

        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("Paste your API key here")
        self._api_key.textChanged.connect(lambda _t: self._sync_key_note())
        key_row.addWidget(self._api_key, 1)
        self._key_toggle = QToolButton()
        self._key_toggle.setText("Show")
        self._key_toggle.setCheckable(True)
        self._key_toggle.setToolTip("Show or hide the key")
        self._key_toggle.toggled.connect(self._on_key_visibility)
        key_row.addWidget(self._key_toggle)
        form.addRow("API key", key_row)

        self._key_note = QLabel("")
        self._key_note.setWordWrap(True)
        self._key_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        form.addRow("", self._key_note)

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._model.setToolTip("Pick a suggestion or type any model name")
        form.addRow("Model", self._model)

        test_row = QHBoxLayout()
        test_row.setSpacing(8)
        self._btn_test = QPushButton("Test connection")
        self._btn_test.clicked.connect(self._on_test_connection)
        test_row.addWidget(self._btn_test)
        self._test_status = QLabel("")
        self._test_status.setWordWrap(True)
        test_row.addWidget(self._test_status, 1)
        form.addRow("", test_row)

        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 8)
        self._concurrency.setToolTip(
            "How many chunks of the document are sent at once. Lower this if the "
            "free tier rate-limits you."
        )
        form.addRow("Parallel AI calls", self._concurrency)

        col.addLayout(form)
        col.addStretch(1)
        return page

    # -- Word tab ---------------------------------------------------------- #

    def _build_word_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)

        style_box = QGroupBox("House style")
        form = QFormLayout(style_box)
        form.setSpacing(8)

        self._word_font = self._font_combo()
        form.addRow("Font", self._word_font)

        self._word_size = QSpinBox()
        self._word_size.setRange(8, 72)
        self._word_size.setSuffix(" pt")
        form.addRow("Font size", self._word_size)

        col.addWidget(style_box)

        content_box = QGroupBox("What goes in the document")
        content = QVBoxLayout(content_box)
        content.setSpacing(6)

        self._word_answer = QCheckBox("Include the answer row")
        self._word_explanation = QCheckBox("Include the explanation block")
        self._word_mark_ai = QCheckBox(
            "Mark AI-derived answers so they can be checked"
        )
        self._word_mark_ai.setToolTip(
            "Writes a small marker next to any answer the AI worked out rather "
            "than read from the file."
        )
        self._word_color_tables = QCheckBox("Colour nested tables (blue banded)")
        for widget in (
            self._word_answer,
            self._word_explanation,
            self._word_mark_ai,
            self._word_color_tables,
        ):
            content.addWidget(widget)

        col.addWidget(content_box)
        col.addStretch(1)
        return page

    # -- PowerPoint tab ---------------------------------------------------- #

    def _build_pptx_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)

        type_box = QGroupBox("Typography")
        form = QFormLayout(type_box)
        form.setSpacing(8)

        self._pptx_q_font = self._font_combo()
        form.addRow("Question font", self._pptx_q_font)

        self._pptx_q_size = QSpinBox()
        self._pptx_q_size.setRange(10, 96)
        self._pptx_q_size.setSuffix(" pt")
        form.addRow("Question size", self._pptx_q_size)

        self._pptx_o_font = self._font_combo()
        form.addRow("Options font", self._pptx_o_font)

        self._pptx_o_size = QSpinBox()
        self._pptx_o_size.setRange(10, 96)
        self._pptx_o_size.setSuffix(" pt")
        form.addRow("Options size", self._pptx_o_size)

        self._pptx_min_size = QSpinBox()
        self._pptx_min_size.setRange(8, 40)
        self._pptx_min_size.setSuffix(" pt")
        self._pptx_min_size.setToolTip(
            "Long questions are shrunk to fit the slide, but never below this."
        )
        form.addRow("Smallest allowed size", self._pptx_min_size)

        col.addWidget(type_box)

        answer_box = QGroupBox("Answers")
        answers = QVBoxLayout(answer_box)
        answers.setSpacing(6)

        self._pptx_include_answers = QCheckBox("Include answers in the deck")
        self._pptx_include_answers.toggled.connect(
            lambda _c: self._sync_pptx_state()
        )
        answers.addWidget(self._pptx_include_answers)

        placement = QWidget()
        placement_col = QVBoxLayout(placement)
        placement_col.setContentsMargins(18, 0, 0, 0)
        placement_col.setSpacing(4)
        self._pptx_answer_slide = QRadioButton("Answer on its own slide")
        self._pptx_answer_banner = QRadioButton(
            "Answer as a banner on the question slide"
        )
        placement_col.addWidget(self._pptx_answer_slide)
        placement_col.addWidget(self._pptx_answer_banner)
        answers.addWidget(placement)
        self._pptx_placement = placement

        col.addWidget(answer_box)

        extra_box = QGroupBox("Extra slides")
        extras = QVBoxLayout(extra_box)
        extras.setSpacing(6)
        self._pptx_title_slide = QCheckBox("Title slide (logo, subject, faculty)")
        self._pptx_thankyou = QCheckBox("Thank-you slide at the end")
        extras.addWidget(self._pptx_title_slide)
        extras.addWidget(self._pptx_thankyou)
        col.addWidget(extra_box)

        col.addStretch(1)
        return page

    # -- Branding tab ------------------------------------------------------ #

    def _build_brand_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(10)

        brand_box = QGroupBox("Branding (PowerPoint title slide)")
        form = QFormLayout(brand_box)
        form.setSpacing(8)

        self._brand_subject = QLineEdit()
        self._brand_subject.setPlaceholderText("e.g. Anatomy — Upper Limb")
        form.addRow("Subject", self._brand_subject)

        self._brand_faculty = QLineEdit()
        self._brand_faculty.setPlaceholderText("e.g. Dr Praveen")
        form.addRow("Faculty", self._brand_faculty)

        date_row = QHBoxLayout()
        date_row.setSpacing(6)
        self._brand_date = QLineEdit()
        self._brand_date.setPlaceholderText("e.g. 25 July 2026")
        date_row.addWidget(self._brand_date, 1)
        btn_today = QPushButton("Today")
        btn_today.clicked.connect(self._on_today)
        date_row.addWidget(btn_today)
        form.addRow("Date", date_row)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(6)
        self._brand_logo = QLineEdit()
        self._brand_logo.setPlaceholderText("No logo chosen")
        self._brand_logo.textChanged.connect(lambda _t: self._sync_logo_preview())
        logo_row.addWidget(self._brand_logo, 1)
        btn_logo = QPushButton("Browse…")
        btn_logo.clicked.connect(self._on_pick_logo)
        logo_row.addWidget(btn_logo)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._brand_logo.clear)
        logo_row.addWidget(btn_clear)
        form.addRow("Logo", logo_row)

        self._logo_preview = QLabel("No logo chosen")
        self._logo_preview.setFrameShape(QFrame.Shape.StyledPanel)
        self._logo_preview.setMinimumHeight(LOGO_PREVIEW_PX // 2)
        self._logo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._logo_preview.setStyleSheet(f"color:{MUTED}; padding:6px;")
        form.addRow("", self._logo_preview)

        # Said plainly, because there are two logos and mixing them up is easy:
        # this one is the academy mark for the deck, the app's own logo is fixed.
        logo_note = QLabel(
            "This logo goes on the PowerPoint title slide only. The app's own "
            "logo — on the lock screen and the home page — is built in and is "
            "not affected by this setting. The Word export carries no logo."
        )
        logo_note.setWordWrap(True)
        logo_note.setStyleSheet(f"color:{MUTED};")
        form.addRow("", logo_note)

        col.addWidget(brand_box)

        out_box = QGroupBox("Output folder")
        out = QHBoxLayout(out_box)
        out.setSpacing(6)
        self._output_dir = QLineEdit()
        self._output_dir.setPlaceholderText(
            "Leave empty to save next to the source file"
        )
        out.addWidget(self._output_dir, 1)
        btn_out = QPushButton("Browse…")
        btn_out.clicked.connect(self._on_pick_output)
        out.addWidget(btn_out)
        col.addWidget(out_box)

        lock_box = QGroupBox("App password")
        lock = QVBoxLayout(lock_box)
        lock.setSpacing(6)
        self._auth_state = QLabel("")
        self._auth_state.setStyleSheet(f"color:{MUTED};")
        self._auth_state.setWordWrap(True)
        lock.addWidget(self._auth_state)
        lock_row = QHBoxLayout()
        lock_row.setSpacing(6)
        self._btn_password = QPushButton("Change password…")
        self._btn_password.clicked.connect(self._on_change_password)
        lock_row.addWidget(self._btn_password)
        self._btn_remove_password = QPushButton("Remove password")
        self._btn_remove_password.clicked.connect(self._on_remove_password)
        lock_row.addWidget(self._btn_remove_password)
        lock_row.addStretch(1)
        lock.addLayout(lock_row)
        col.addWidget(lock_box)

        look_box = QGroupBox("Appearance")
        look = QVBoxLayout(look_box)
        look.setSpacing(6)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        theme_row.addWidget(QLabel("Theme"))
        self._theme = QComboBox()
        self._theme.addItem("Light", "light")
        self._theme.addItem("Dark", "dark")
        theme_row.addWidget(self._theme)
        hint = QLabel("Applies when the app is restarted.")
        hint.setStyleSheet(f"color:{MUTED};")
        theme_row.addWidget(hint, 1)
        look.addLayout(theme_row)
        wall = QLabel(
            "Dark uses the Batman wallpaper behind the app, with the palette "
            "taken from the picture. Light stays plain — a photograph behind a "
            "white working surface fights every panel on screen."
        )
        wall.setWordWrap(True)
        wall.setStyleSheet(f"color:{MUTED};")
        look.addWidget(wall)
        col.addWidget(look_box)

        col.addStretch(1)
        return page

    @staticmethod
    def _font_combo() -> QComboBox:
        """Editable font picker.

        Not a QFontComboBox on purpose: the house style names fonts (e.g.
        "Arial Rounded MT Bold") that may not be installed on this machine but
        exist on the one the deck is presented from, and a QFontComboBox would
        silently substitute them.
        """
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItems(COMMON_FONTS)
        return combo

    # -- load / save ------------------------------------------------------- #

    def _load_from_settings(self) -> None:
        s = self._settings

        index = self._provider.findData(s.provider)
        self._provider.setCurrentIndex(index if index >= 0 else 0)
        self._populate_models(s.provider, keep=s.model)
        self._api_key.setText(s.api_key)
        self._use_ai.setChecked(bool(s.use_ai))
        self._concurrency.setValue(max(1, min(8, int(s.max_concurrency))))

        self._word_font.setCurrentText(self._word.font_name)
        self._word_size.setValue(int(self._word.font_size_pt))
        self._word_answer.setChecked(bool(self._word.include_answer))
        self._word_explanation.setChecked(bool(self._word.include_explanation))
        self._word_mark_ai.setChecked(bool(self._word.mark_ai_answers))
        self._word_color_tables.setChecked(bool(self._word.color_nested_tables))

        self._pptx_q_font.setCurrentText(self._pptx.question_font)
        self._pptx_q_size.setValue(int(self._pptx.question_size_pt))
        self._pptx_o_font.setCurrentText(self._pptx.options_font)
        self._pptx_o_size.setValue(int(self._pptx.options_size_pt))
        self._pptx_min_size.setValue(int(self._pptx.min_font_pt))
        self._pptx_include_answers.setChecked(bool(self._pptx.include_answers))
        if self._pptx.answer_on_separate_slide:
            self._pptx_answer_slide.setChecked(True)
        else:
            self._pptx_answer_banner.setChecked(True)
        self._pptx_title_slide.setChecked(bool(self._pptx.title_slide))
        self._pptx_thankyou.setChecked(bool(self._pptx.thankyou_slide))

        self._brand_subject.setText(self._brand.subject)
        self._brand_faculty.setText(self._brand.faculty)
        self._brand_date.setText(self._brand.date_str)
        self._brand_logo.setText(self._brand.logo_path)
        self._output_dir.setText(s.output_dir)
        theme_index = self._theme.findData(
            str(getattr(s, "theme", "light") or "light").lower()
        )
        self._theme.setCurrentIndex(max(0, theme_index))

        self._sync_ai_state()
        self._sync_pptx_state()
        self._sync_logo_preview()
        self._sync_auth_state()

    # -- app password ------------------------------------------------------- #

    def _sync_auth_state(self) -> None:
        try:
            from app import auth
        except ImportError:
            self._auth_state.setText("The password module is unavailable.")
            self._btn_password.setEnabled(False)
            self._btn_remove_password.setEnabled(False)
            return
        self._auth_state.setText(
            auth.describe()
            + "\nThis locks the app window. It does not encrypt your documents — "
            "anyone with access to this PC can still open the files or use the CLI."
        )
        self._btn_password.setText(
            "Change password…" if auth.is_password_set() else "Set a password…"
        )
        self._btn_remove_password.setEnabled(auth.is_password_set())

    def _on_change_password(self) -> None:
        from app import auth

        existing = auth.is_password_set()
        if existing:
            current, ok = QInputDialog.getText(
                self, "Current password", "Enter your current password:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return
            if not auth.verify_password(current):
                QMessageBox.warning(
                    self, "Password", "That is not the current password."
                )
                return

        new, ok = QInputDialog.getText(
            self, "New password",
            f"New password (at least {auth.MIN_PASSWORD_LENGTH} characters):",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        again, ok = QInputDialog.getText(
            self, "New password", "Type it again to confirm:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        if new != again:
            QMessageBox.warning(self, "Password", "The two passwords do not match.")
            return
        try:
            auth.set_password(new)
        except auth.AuthError as exc:
            QMessageBox.warning(self, "Password", str(exc))
            return
        self._sync_auth_state()
        QMessageBox.information(
            self, "Password",
            "Password saved. It applies the next time the app is opened or locked.",
        )

    def _on_remove_password(self) -> None:
        from app import auth

        if not auth.is_password_set():
            return
        current, ok = QInputDialog.getText(
            self, "Remove password", "Enter your current password to remove it:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        if not auth.verify_password(current):
            QMessageBox.warning(self, "Password", "That is not the current password.")
            return
        if QMessageBox.question(
            self, "Remove password",
            "Remove the password? The app will open without asking for one.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            auth.clear_password()
        except auth.AuthError as exc:
            QMessageBox.warning(self, "Password", str(exc))
            return
        self._sync_auth_state()

    def apply_to_settings(self) -> "Settings":
        """Write the widget state into the Settings object and return it.

        Separate from :meth:`accept` so the write-back can be exercised (and
        re-used) without touching the settings file on disk.
        """
        s = self._settings
        provider = self._provider.currentData() or "none"

        s.provider = provider
        s.api_key = self._api_key.text().strip()
        s.model = self._model.currentText().strip()
        # "none" means rule-based only, so it must not leave use_ai claiming AI.
        s.use_ai = bool(self._use_ai.isChecked()) and provider != "none"
        s.max_concurrency = int(self._concurrency.value())
        s.output_dir = self._output_dir.text().strip()
        s.theme = self._theme.currentData() or "light"

        self._word.font_name = self._word_font.currentText().strip() or "Calibri"
        self._word.font_size_pt = int(self._word_size.value())
        self._word.include_answer = self._word_answer.isChecked()
        self._word.include_explanation = self._word_explanation.isChecked()
        self._word.mark_ai_answers = self._word_mark_ai.isChecked()
        self._word.color_nested_tables = self._word_color_tables.isChecked()

        self._pptx.question_font = self._pptx_q_font.currentText().strip() or "Arial"
        self._pptx.question_size_pt = int(self._pptx_q_size.value())
        self._pptx.options_font = self._pptx_o_font.currentText().strip() or "Arial"
        self._pptx.options_size_pt = int(self._pptx_o_size.value())
        self._pptx.min_font_pt = int(self._pptx_min_size.value())
        self._pptx.include_answers = self._pptx_include_answers.isChecked()
        self._pptx.answer_on_separate_slide = self._pptx_answer_slide.isChecked()
        self._pptx.title_slide = self._pptx_title_slide.isChecked()
        self._pptx.thankyou_slide = self._pptx_thankyou.isChecked()

        self._brand.subject = self._brand_subject.text().strip()
        self._brand.faculty = self._brand_faculty.text().strip()
        self._brand.date_str = self._brand_date.text().strip()
        self._brand.logo_path = self._brand_logo.text().strip()

        s.brand = self._brand
        s.word = self._word
        s.pptx = self._pptx
        return s

    def settings(self) -> "Settings":
        """The Settings object this dialog edits (updated once OK was pressed)."""
        return self._settings

    def accept(self) -> None:
        self.apply_to_settings()
        try:
            from app.config import save_settings  # lazy: only needed on save

            save_settings(self._settings)
        except ImportError:  # pragma: no cover - only while app.config is absent
            log.warning("app.config unavailable; settings kept in memory only")
        except OSError as exc:
            # Worth interrupting for: the user thinks their key is stored.
            log.error("could not save settings: %s", exc)
            QMessageBox.warning(
                self,
                "Settings not saved",
                "Your choices are active for this session but could not be "
                f"written to disk:\n\n{exc}",
            )
        super().accept()

    # -- AI tab behaviour -------------------------------------------------- #

    def _on_provider_changed(self, _index: int) -> None:
        provider = self._provider.currentData() or "none"
        self._populate_models(provider)
        self._test_status.clear()
        self._sync_ai_state()

    def _populate_models(self, provider: str, *, keep: str | None = None) -> None:
        """Refill the model suggestions, keeping a hand-typed model name.

        A name the user typed themselves is theirs to keep; one of *our*
        suggestions is replaced by the new provider's default, because a Gemini
        model id is useless once the provider is OpenAI.
        """
        current = keep if keep is not None else self._model.currentText().strip()
        known = {m for models in MODEL_SUGGESTIONS.values() for m in models}
        suggestions = MODEL_SUGGESTIONS.get(provider, ())

        self._model.blockSignals(True)
        try:
            self._model.clear()
            self._model.addItems(suggestions)
            if current and current not in known:
                self._model.insertItem(0, current)
                self._model.setCurrentIndex(0)
            elif current in suggestions:
                self._model.setCurrentText(current)
            elif suggestions:
                self._model.setCurrentText(suggestions[0])
            else:
                self._model.setCurrentText("")
        finally:
            self._model.blockSignals(False)

    def _on_key_visibility(self, shown: bool) -> None:
        self._api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self._key_toggle.setText("Hide" if shown else "Show")

    def _sync_ai_state(self) -> None:
        provider = self._provider.currentData() or "none"
        rule_based = provider == "none"
        for widget in (self._api_key, self._key_toggle, self._model, self._btn_test):
            widget.setEnabled(not rule_based)
        self._concurrency.setEnabled(not rule_based and self._use_ai.isChecked())
        self._sync_key_note()

    def _sync_key_note(self) -> None:
        provider = self._provider.currentData() or "none"
        if provider == "none":
            self._key_note.setText(
                "Rule-based only: no key is used and nothing leaves this computer."
            )
            return
        if self._api_key.text().strip():
            self._key_note.setText(
                "Stored on this computer only. The key is never shown in logs or "
                "written into your documents."
            )
            return
        env = [name for name in KEY_ENV_VARS.get(provider, ()) if os.environ.get(name)]
        if env:
            self._key_note.setText(
                f"No key typed here — {env[0]} from your environment will be used."
            )
        else:
            self._key_note.setText(
                "No key yet. The app still works: it falls back to the "
                "rule-based parser."
            )

    def _on_test_connection(self) -> None:
        provider = self._provider.currentData() or "none"
        if provider == "none":
            self._set_test_status("Nothing to test — AI is switched off.", MUTED)
            return

        self._btn_test.setEnabled(False)
        self._set_test_status("Testing…", MUTED)
        probe = self._probe_settings()

        try:
            from app.ui.worker import run_job  # lazy: keeps this module light
        except ImportError as exc:  # pragma: no cover - only during development
            self._btn_test.setEnabled(True)
            self._set_test_status(f"Cannot run the test: {exc}", DANGER)
            return

        # Off the GUI thread: a provider call can take seconds, and a frozen
        # dialog is indistinguishable from a crashed app.
        self._test_thread = run_job(
            _probe_provider,
            probe,
            on_done=self._on_test_done,
            on_error=self._on_test_failed,
        )

    def _probe_settings(self) -> Any:
        """A throwaway copy of Settings carrying the *typed* values."""
        key = self._api_key.text().strip()
        provider = self._provider.currentData() or "none"
        model = self._model.currentText().strip()
        try:
            return dataclasses.replace(
                self._settings, api_key=key, provider=provider, model=model
            )
        except TypeError:
            # Settings is not a dataclass (or rejects replace): fall back to the
            # minimal shape a provider factory actually reads.
            return _ProbeSettings(api_key=key, provider=provider, model=model)

    def _on_test_done(self, result: object) -> None:
        name = str(result) if result else "provider"
        self._btn_test.setEnabled(True)
        self._set_test_status(f"✓ Connected — {name} answered.", OK_GREEN)
        log.info("connection test succeeded for %s", name)

    def _on_test_failed(self, message: object) -> None:
        detail = self._scrub(str(message))
        self._btn_test.setEnabled(True)
        self._set_test_status(f"✗ {detail}", DANGER)
        log.warning("connection test failed: %s", detail)

    def _set_test_status(self, text: str, colour: str) -> None:
        self._test_status.setText(text)
        self._test_status.setStyleSheet(f"color:{colour}; font-weight:600;")

    def _scrub(self, text: str) -> str:
        """Remove the API key from provider error text before it is displayed."""
        key = self._api_key.text().strip()
        if key and key in text:
            text = text.replace(key, "***")
        return text

    # -- other tabs' behaviour --------------------------------------------- #

    def _sync_pptx_state(self) -> None:
        self._pptx_placement.setEnabled(self._pptx_include_answers.isChecked())

    def _on_today(self) -> None:
        self._brand_date.setText(_dt.date.today().strftime("%d %B %Y"))

    def _on_pick_logo(self) -> None:
        start = os.path.dirname(self._brand_logo.text().strip()) or ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a logo image", start, LOGO_FILTER
        )
        if path:
            self._brand_logo.setText(path)

    def _on_pick_output(self) -> None:
        start = self._output_dir.text().strip() or ""
        path = QFileDialog.getExistingDirectory(self, "Choose an output folder", start)
        if path:
            self._output_dir.setText(path)

    def _sync_logo_preview(self) -> None:
        path = self._brand_logo.text().strip()
        if not path:
            self._logo_preview.setPixmap(QPixmap())
            self._logo_preview.setText("No logo chosen")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._logo_preview.setPixmap(QPixmap())
            self._logo_preview.setText("That file could not be read as an image")
            log.info("logo preview failed for %s", path)
            return
        self._logo_preview.setText("")
        self._logo_preview.setPixmap(
            pixmap.scaled(
                LOGO_PREVIEW_PX * 3,
                LOGO_PREVIEW_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
