"""The home screen: drag files in, name the deck, continue.

Two rules shaped this module.

* **Nothing here touches a document.** The screen collects paths and branding and
  hands them on; reading, parsing and exporting all happen in `app.ui.worker`
  jobs. That is why `app.ingest` is imported lazily -- the shell must start (and
  the accepted-types line must still be right) even while the sibling packages
  are being written.
* **No modal dialogs for user mistakes.** A folder drop, an unsupported
  extension or a missing API key are reported inline, in place, and the user can
  carry on. The only dialog is the file browser they asked for.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from collections.abc import Iterable

from PySide6.QtCore import (
    QMargins,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import repolish

_LOG = logging.getLogger(__name__)

APP_TITLE = "GOD MODE ON"
APP_SUBTITLE = "Ingest messy MCQ documents. Get clean, uniform output."
API_KEY_URL = "https://aistudio.google.com/apikey"

#: Used until `app.ingest` can be imported (and if it never can). Kept in sync
#: with the contract's SUPPORTED_EXTENSIONS so the UI never promises more than
#: the readers deliver.
_FALLBACK_EXTENSIONS: frozenset[str] = frozenset({".docx", ".pdf", ".txt", ".pptx"})

_EXTENSIONS_CACHE: frozenset[str] | None = None

# Text-presentation glyphs (Segoe UI Symbol). Emoji code points would render in
# colour on Windows and clash with the flat UI.
GLYPH_DROP = "⇩"      # downwards white arrow
GLYPH_GEAR = "⚙"      # gear
GLYPH_CLOSE = "✕"     # multiplication x
GLYPH_WARN = "⚠"      # warning sign


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def supported_extensions() -> frozenset[str]:
    """Extensions the ingest layer can read, lower-cased and dot-prefixed.

    Imported lazily and cached only on success: a half-written `app.ingest`
    should not permanently pin the UI to the fallback list.
    """
    global _EXTENSIONS_CACHE
    if _EXTENSIONS_CACHE is not None:
        return _EXTENSIONS_CACHE
    try:
        from app.ingest import SUPPORTED_EXTENSIONS
    except (ImportError, AttributeError, SyntaxError) as exc:
        _LOG.debug("app.ingest not importable yet (%s); using fallback types", exc)
        return _FALLBACK_EXTENSIONS
    try:
        exts = frozenset(str(e).lower() for e in SUPPORTED_EXTENSIONS)
    except TypeError:
        _LOG.warning("app.ingest.SUPPORTED_EXTENSIONS is not iterable; using fallback")
        return _FALLBACK_EXTENSIONS
    _EXTENSIONS_CACHE = exts or _FALLBACK_EXTENSIONS
    return _EXTENSIONS_CACHE


def extensions_label() -> str:
    """".docx  .pdf  .pptx  .txt" -- the quiet accepted-types line."""
    return "   ".join(sorted(supported_extensions()))


def dialog_filter() -> str:
    """A QFileDialog name filter covering exactly the supported extensions."""
    patterns = " ".join(f"*{e}" for e in sorted(supported_extensions()))
    return f"MCQ documents ({patterns});;All files (*)"


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in supported_extensions()


def human_size(num_bytes: int) -> str:
    """"1.4 MB" -- deliberately decimal units, matching Windows Explorer."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError as exc:
        _LOG.debug("cannot size %s: %s", path, exc)
        return 0


def normalise(path: str) -> str:
    """Canonical form used for de-duplication (case-insensitive on Windows)."""
    try:
        return os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return path


def _elide(text: str, widget: QWidget, max_px: int) -> str:
    metrics = QFontMetrics(widget.font())
    return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, max_px)


def _classify_urls(mime: QMimeData | None) -> tuple[list[str], list[str], list[str]]:
    """Split dropped URLs into (supported files, folders, unsupported files)."""
    files: list[str] = []
    folders: list[str] = []
    rejected: list[str] = []
    if mime is None or not mime.hasUrls():
        return files, folders, rejected
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if os.path.isdir(path):
            folders.append(path)
        elif is_supported(path):
            files.append(path)
        else:
            rejected.append(path)
    return files, folders, rejected


# --------------------------------------------------------------------------- #
# Flow layout (chips wrap like text)
# --------------------------------------------------------------------------- #


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto a new row when it runs out of width.

    Qt ships no wrapping layout, and a grid would leave ragged gaps between file
    chips of very different widths.
    """

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(QMargins(0, 0, 0, 0))
        self.setSpacing(spacing)

    # -- QLayout plumbing --------------------------------------------------- #

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    # -- the actual flow ---------------------------------------------------- #

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        right = rect.right() - margins.right()
        x, y = left, rect.y() + margins.top()
        row_height = 0
        gap = max(0, self.spacing())

        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            if x > left and x + hint.width() > right:
                x = left
                y += row_height + gap
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            row_height = max(row_height, hint.height())

        return y + row_height + margins.bottom() - rect.y()


# --------------------------------------------------------------------------- #
# File chip
# --------------------------------------------------------------------------- #


class FileChip(QFrame):
    """One queued file: name, size, and an x to drop it again."""

    removeRequested = Signal(str)

    def __init__(
        self,
        path: str = "",
        *,
        supported: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.path = path
        self.setObjectName("chip")
        self.setProperty("bad", not supported)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        name = os.path.basename(path) or path
        size = human_size(file_size(path)) if supported else "unsupported"
        self.setToolTip(f"{path}\n{size}")

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 6, 6)
        row.setSpacing(8)

        self._name = QLabel(_elide(name, self, 240), self)
        self._name.setObjectName("chipName")
        self._meta = QLabel(size, self)
        self._meta.setObjectName("chipMeta")

        close = QPushButton(GLYPH_CLOSE, self)
        close.setObjectName("chipClose")
        close.setToolTip(f"Remove {name}")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFlat(True)
        close.clicked.connect(self._emit_remove)

        row.addWidget(self._name)
        row.addWidget(self._meta)
        row.addWidget(close)

    def _emit_remove(self) -> None:
        self.removeRequested.emit(self.path)


# --------------------------------------------------------------------------- #
# Drop zone
# --------------------------------------------------------------------------- #


class DropZone(QFrame):
    """The dashed target: drop files on it, click it, or focus it and press Enter."""

    filesDropped = Signal(list)
    #: Emitted alongside the inline message, so a host screen can log/echo it.
    rejected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # QSS :hover only works once the widget actually tracks hover state.
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setProperty("dragActive", "false")
        self.setAccessibleName("Drop your MCQ documents here")
        self.setToolTip("Drop .docx, .pdf, .pptx or .txt files here, or click to browse")

        column = QVBoxLayout(self)
        column.setContentsMargins(22, 16, 22, 16)
        column.setSpacing(6)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)

        glyph = QLabel(GLYPH_DROP, self)
        glyph.setObjectName("dropGlyph")
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Drop your MCQ documents here", self)
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._hint = QLabel(f"or click to browse    {extensions_label()}", self)
        self._hint.setObjectName("dropHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._notice = QLabel("", self)
        self._notice.setObjectName("notice")
        self._notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notice.setWordWrap(True)
        self._notice.hide()

        column.addStretch(1)
        column.addWidget(glyph)
        column.addWidget(title)
        column.addWidget(self._hint)
        column.addSpacing(4)
        column.addWidget(self._notice)
        column.addStretch(1)

    # -- inline messaging --------------------------------------------------- #

    def set_notice(self, text: str, level: str = "warn") -> None:
        """Show an inline message inside the zone (never a dialog)."""
        if not text:
            self.clear_notice()
            return
        self._notice.setText(text)
        self._notice.setProperty("level", level)
        repolish(self._notice)
        self._notice.show()

    def clear_notice(self) -> None:
        self._notice.clear()
        self._notice.hide()

    def refresh_types(self) -> None:
        """Re-read the accepted extensions (ingest may have arrived since)."""
        self._hint.setText(f"or click to browse    {extensions_label()}")

    # -- browse ------------------------------------------------------------- #

    def browse(self) -> None:
        """Open the file picker; emits `filesDropped` with whatever was chosen."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose MCQ documents", "", dialog_filter()
        )
        if paths:
            self.clear_notice()
            self.filesDropped.emit(list(paths))

    # -- mouse / keyboard --------------------------------------------------- #

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        inside = self.rect().contains(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and inside:
            self.browse()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            event.accept()
            self.browse()
            return
        super().keyPressEvent(event)

    # -- drag and drop ------------------------------------------------------ #

    def _set_drag_state(self, state: str) -> None:
        if self.property("dragActive") != state:
            self.setProperty("dragActive", state)
            repolish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        files, folders, bad = _classify_urls(event.mimeData())
        if not (files or folders or bad):
            event.ignore()
            return
        # Folders and wrong types are still accepted so the drop can explain
        # itself; refusing the drag outright looks like a broken window.
        self._set_drag_state("true" if files else "reject")
        event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if self.property("dragActive") in ("true", "reject"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._set_drag_state("false")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_drag_state("false")
        files, folders, bad = _classify_urls(event.mimeData())
        event.acceptProposedAction()

        messages: list[str] = []
        if folders:
            names = ", ".join(os.path.basename(p.rstrip("\\/")) for p in folders[:3])
            messages.append(
                f"Folders cannot be dropped ({names}) - open it and drop the "
                "files inside."
            )
        if bad:
            names = ", ".join(os.path.basename(p) for p in bad[:3])
            more = f" and {len(bad) - 3} more" if len(bad) > 3 else ""
            messages.append(
                f"Not a supported type: {names}{more}. Accepted: "
                f"{extensions_label().strip()}"
            )

        if messages:
            text = "  ".join(messages)
            self.set_notice(text, "error" if not files else "warn")
            self.rejected.emit(text)
        else:
            self.clear_notice()

        if files:
            self.filesDropped.emit(files)


# --------------------------------------------------------------------------- #
# API-key banner
# --------------------------------------------------------------------------- #


def _has_api_key(settings: object | None) -> bool:
    """True when some usable key exists. Never logs or returns the key itself."""
    if settings is None:
        return False
    if str(getattr(settings, "api_key", "") or "").strip():
        return True
    try:
        from app.config import resolve_api_key
    except (ImportError, AttributeError, SyntaxError) as exc:
        _LOG.debug("app.config not importable (%s); checking env directly", exc)
    else:
        try:
            return bool(str(resolve_api_key(settings) or "").strip())  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a cosmetic banner must never raise
            _LOG.debug("resolve_api_key failed; falling back to env probe")
    return any(
        os.environ.get(var, "").strip()
        for var in (
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GROQ_API_KEY",
        )
    )


class ApiKeyBanner(QFrame):
    """Amber, dismissible-by-fixing, never modal: 'running without AI'."""

    settingsRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(10)

        glyph = QLabel(GLYPH_WARN, self)
        glyph.setObjectName("bannerGlyph")

        text = QLabel(
            "Running without AI. Add a free Gemini key for best accuracy - "
            f'<a href="{API_KEY_URL}" style="color:#1AB7EA;">Get a free key</a>',
            self,
        )
        text.setObjectName("bannerText")
        text.setOpenExternalLinks(True)
        text.setWordWrap(True)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )

        add = QPushButton("Add key", self)
        add.setProperty("variant", "primary")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self.settingsRequested)

        row.addWidget(glyph)
        row.addWidget(text, 1)
        row.addWidget(add)


# --------------------------------------------------------------------------- #
# Home screen
# --------------------------------------------------------------------------- #


class HomeScreen(QWidget):
    """Title, drop zone, queued-file chips, branding, Continue.

    `settings` is an `app.config.Settings` (or anything with the same attribute
    names); the branding fields write straight back into `settings.brand` so the
    export jobs pick them up without any extra wiring.
    """

    filesReady = Signal(list)
    settingsRequested = Signal()

    def __init__(
        self,
        settings: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        # Tolerate HomeScreen(parent) as well as HomeScreen(settings, parent):
        # main.py should not have to remember which comes first.
        if isinstance(settings, QWidget) and parent is None:
            settings, parent = None, settings
        super().__init__(parent)
        self._settings = settings
        self._paths: list[str] = []
        self._action = ""

        self.setObjectName("homeScreen")
        self.setProperty("screen", True)
        self.setAcceptDrops(True)

        self._build()
        self._bind_brand()
        self.refresh_api_banner()
        self._sync_files_ui()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        inner = QWidget()
        inner.setObjectName("screenInner")
        scroll.setWidget(inner)

        column = QVBoxLayout(inner)
        column.setContentsMargins(28, 20, 28, 20)
        column.setSpacing(12)

        column.addLayout(self._build_header())
        self._banner = ApiKeyBanner(inner)
        self._banner.settingsRequested.connect(self.settingsRequested)
        column.addWidget(self._banner)

        self.drop_zone = DropZone(inner)
        self.drop_zone.filesDropped.connect(self.add_files)
        column.addWidget(self.drop_zone)

        self._files_card = self._build_files_card(inner)
        column.addWidget(self._files_card)

        self._brand_card = self._build_brand_card(inner)
        column.addWidget(self._brand_card)
        column.addStretch(1)
        column.addLayout(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(4)
        title = QLabel(APP_TITLE, self)
        title.setObjectName("h1")
        subtitle = QLabel(APP_SUBTITLE, self)
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)

        self.gear_button = QPushButton(GLYPH_GEAR, self)
        self.gear_button.setObjectName("iconButton")
        self.gear_button.setToolTip("Settings - API key, branding, export options")
        self.gear_button.setAccessibleName("Settings")
        self.gear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gear_button.clicked.connect(self.settingsRequested)

        row.addLayout(titles, 1)
        row.addWidget(self.gear_button, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _build_files_card(self, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setObjectName("filesCard")
        card.setProperty("card", True)

        column = QVBoxLayout(card)
        column.setContentsMargins(14, 11, 14, 11)
        column.setSpacing(10)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self._files_label = QLabel("QUEUED FILES", card)
        self._files_label.setObjectName("sectionLabel")
        self.clear_button = QPushButton("Clear all", card)
        self.clear_button.setProperty("variant", "link")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("Remove every queued file")
        self.clear_button.clicked.connect(self.clear_files)
        head.addWidget(self._files_label)
        head.addStretch(1)
        head.addWidget(self.clear_button)
        column.addLayout(head)

        chips = QWidget(card)
        chips.setObjectName("chipHost")
        self._chip_layout = FlowLayout(chips, spacing=8)
        column.addWidget(chips)
        return card

    def _build_brand_card(self, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setObjectName("brandCard")
        card.setProperty("card", True)

        column = QVBoxLayout(card)
        column.setContentsMargins(14, 11, 14, 12)
        column.setSpacing(10)

        heading = QLabel("DECK DETAILS", card)
        heading.setObjectName("sectionLabel")
        column.addWidget(heading)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(10)

        self.subject_edit = self._field(card, fields, "Subject", "e.g. ENT Recall")
        self.faculty_edit = self._field(card, fields, "Faculty", "e.g. Dr Bhanu")
        self.date_edit = self._field(card, fields, "Date", "e.g. 18 July 2025")

        column.addLayout(fields)
        hint = QLabel(
            "Used on the PowerPoint title slide. The Word export is questions "
            "only — nothing is added above the first box.", card
        )
        hint.setObjectName("hint")
        column.addWidget(hint)
        return card

    @staticmethod
    def _field(
        card: QWidget, row: QHBoxLayout, label: str, placeholder: str
    ) -> QLineEdit:
        cell = QVBoxLayout()
        cell.setContentsMargins(0, 0, 0, 0)
        cell.setSpacing(5)
        caption = QLabel(label, card)
        caption.setObjectName("fieldLabel")
        edit = QLineEdit(card)
        edit.setPlaceholderText(placeholder)
        edit.setClearButtonEnabled(True)
        edit.setAccessibleName(label)
        caption.setBuddy(edit)
        cell.addWidget(caption)
        cell.addWidget(edit)
        row.addLayout(cell, 1)
        return edit

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self._status = QLabel("", self)
        self._status.setObjectName("footer")

        self.continue_button = QPushButton("Continue  →", self)
        self.continue_button.setProperty("variant", "primary")
        self.continue_button.setDefault(True)
        self.continue_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_button.setToolTip("Choose what to do with these files")
        self.continue_button.clicked.connect(self._emit_ready)

        row.addWidget(self._status, 1)
        row.addWidget(self.continue_button)
        return row

    # -- action ------------------------------------------------------------- #

    #: Actions whose export never reads `settings.brand`. For these the DECK
    #: DETAILS card is three fields that change nothing about the output, so it
    #: is hidden rather than left on screen inviting input that goes nowhere.
    _BRANDLESS_ACTIONS: frozenset[str] = frozenset({"extract"})

    def set_action(self, action: object) -> None:
        """Tell the screen which action the dashboard picked.

        Only the deck puts subject / faculty / date on a slide; the box-format
        Word export of "Extract & Format MCQs" starts at the first question and
        writes nothing above it. `main.py` reaches this through `_try_call`, so
        an unknown or missing action simply shows everything.
        """
        self._action = str(action or "").strip().lower()
        self._brand_card.setVisible(self._action not in self._BRANDLESS_ACTIONS)

    #: main.py probes for both spellings.
    setAction = set_action

    # -- settings ----------------------------------------------------------- #

    def set_settings(self, settings: object | None) -> None:
        """Point the screen at a (new) Settings object and re-sync the fields."""
        self._settings = settings
        self._bind_brand()
        self.refresh_api_banner()

    @property
    def settings(self) -> object | None:
        return self._settings

    def _brand(self) -> object | None:
        return getattr(self._settings, "brand", None)

    def _bind_brand(self) -> None:
        """Two-way bind the three brand fields to `settings.brand`."""
        brand = self._brand()
        pairs = (
            (self.subject_edit, "subject"),
            (self.faculty_edit, "faculty"),
            (self.date_edit, "date_str"),
        )
        for edit, attr in pairs:
            try:
                edit.textChanged.disconnect()
            except RuntimeError:
                pass  # nothing connected yet: first bind
            edit.setText(str(getattr(brand, attr, "") or "") if brand else "")

        if brand is None:
            return

        # A dated title slide is the norm, so seed today rather than leaving the
        # export to guess -- the user can still overwrite it.
        if not str(getattr(brand, "date_str", "") or "").strip():
            today = _dt.date.today().strftime("%d %B %Y")
            self.date_edit.setText(today)
            self._write_brand("date_str", today)

        for edit, attr in pairs:
            edit.textChanged.connect(
                lambda text, name=attr: self._write_brand(name, text)
            )

    def _write_brand(self, attr: str, value: str) -> None:
        brand = self._brand()
        if brand is None:
            return
        try:
            setattr(brand, attr, value)
        except (AttributeError, TypeError) as exc:
            _LOG.debug("cannot store brand.%s: %s", attr, exc)

    def refresh_api_banner(self) -> None:
        """Show or hide the amber banner. Call this after the settings dialog."""
        self._banner.setVisible(not _has_api_key(self._settings))

    # -- file queue --------------------------------------------------------- #

    def files(self) -> list[str]:
        """The queued paths, in the order they were added."""
        return list(self._paths)

    def add_files(self, paths: Iterable[str]) -> None:
        """Queue `paths`, skipping duplicates, folders, missing and wrong types."""
        seen = {normalise(p) for p in self._paths}
        added: list[str] = []
        duplicates: list[str] = []
        unsupported: list[str] = []
        missing: list[str] = []

        for raw in paths or ():
            path = str(raw).strip()
            if not path:
                continue
            if os.path.isdir(path):
                unsupported.append(path)
                continue
            if not os.path.isfile(path):
                missing.append(path)
                continue
            if not is_supported(path):
                unsupported.append(path)
                continue
            key = normalise(path)
            if key in seen:
                duplicates.append(path)
                continue
            seen.add(key)
            self._paths.append(path)
            added.append(path)

        self._report_add(added, duplicates, unsupported, missing)
        self._sync_files_ui()

    def _report_add(
        self,
        added: list[str],
        duplicates: list[str],
        unsupported: list[str],
        missing: list[str],
    ) -> None:
        parts: list[str] = []
        if unsupported:
            names = ", ".join(os.path.basename(p.rstrip("\\/")) for p in unsupported[:3])
            more = f" +{len(unsupported) - 3}" if len(unsupported) > 3 else ""
            parts.append(
                f"Skipped {len(unsupported)} unsupported "
                f"item{'s' if len(unsupported) > 1 else ''} ({names}{more}). "
                f"Accepted: {extensions_label().strip()}"
            )
        if missing:
            names = ", ".join(os.path.basename(p) for p in missing[:3])
            parts.append(f"Could not find {names}")
        if duplicates:
            parts.append(
                f"{len(duplicates)} file{'s' if len(duplicates) > 1 else ''} "
                "already queued"
            )
        if parts:
            level = "warn" if added else "error"
            self.drop_zone.set_notice("  ".join(parts), level)
        elif added:
            self.drop_zone.clear_notice()

    def remove_file(self, path: str) -> None:
        key = normalise(path)
        self._paths = [p for p in self._paths if normalise(p) != key]
        self._sync_files_ui()

    def clear_files(self) -> None:
        self._paths.clear()
        self.drop_zone.clear_notice()
        self._sync_files_ui()

    def _sync_files_ui(self) -> None:
        """Rebuild the chip row and the enabled state of Continue."""
        while self._chip_layout.count():
            item = self._chip_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for path in self._paths:
            chip = FileChip(path, parent=self._files_card)
            chip.removeRequested.connect(self.remove_file)
            self._chip_layout.addWidget(chip)

        count = len(self._paths)
        total = sum(file_size(p) for p in self._paths)
        self._files_card.setVisible(count > 0)
        self._files_label.setText(f"QUEUED FILES  ({count})")
        self.continue_button.setEnabled(count > 0)

        if count == 0:
            self._status.setText("No files queued yet.")
            self.continue_button.setToolTip("Add at least one document first")
        else:
            noun = "file" if count == 1 else "files"
            self._status.setText(f"{count} {noun} ready  |  {human_size(total)}")
            self.continue_button.setToolTip("Choose what to do with these files")
        self._files_card.updateGeometry()

    def _emit_ready(self) -> None:
        if not self._paths:
            return
        self._remember_recent()
        self.filesReady.emit(list(self._paths))

    def _remember_recent(self) -> None:
        remember = getattr(self._settings, "remember_files", None)
        if remember is None:
            return
        try:
            remember(list(self._paths))
        except Exception:  # noqa: BLE001 - MRU bookkeeping is never worth a crash
            _LOG.debug("could not update recent files list", exc_info=True)

    # -- window-wide drop --------------------------------------------------- #

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        files, folders, bad = _classify_urls(event.mimeData())
        if files or folders or bad:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        files, folders, bad = _classify_urls(event.mimeData())
        event.acceptProposedAction()
        # Route everything through add_files so one code path owns the messaging.
        self.add_files(files + folders + bad)
