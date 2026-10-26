"""The home page: every feature of the app on one screen.

Shown straight after unlocking. Picking a document feature carries its choice
through to the drop zone, so the action is already decided by the time files are
in — the old flow (drop first, choose second) is still reachable via
"Process documents" and via the Change link on the drop screen.

The app's identity lives in the nav rail (`app.ui.sidebar`), so this screen opens
with a greeting rather than repeating the logo and the name: the tiles are what
the user came for and they start as close to the top as the greeting allows.

Reuses `ActionCard` from `app.ui.actions` so a card looks and behaves the same
here as it does there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME
from app.ui import theme
from app.ui.actions import ActionCard, ActionSpec, ai_status_lines

_LOG = logging.getLogger(__name__)

#: Below these widths the card grid drops from three columns to two, then one.
#: Measured on the screen, not the window: the nav rail has already taken its
#: 218px by the time this widget is laid out.
_WIDE_PX = 950
_MEDIUM_PX = 640

GLYPH_EXTRACT = "❖"
GLYPH_ANSWER = "✓"
GLYPH_DECK = "▤"
GLYPH_MERGE = "⇄"
GLYPH_BULK = "⌨"
GLYPH_SETTINGS = "⚙"
GLYPH_DOT = "●"


def owner_name(app_name: str = APP_NAME) -> str:
    """``"Krishna's Super Bot"`` -> ``"Krishna"``, for the greeting.

    Derived rather than hard-coded so renaming the app in one place renames the
    person it greets. Falls back to the whole name when there is no possessive
    to cut at.
    """
    head = app_name.replace("’", "'").split("'")[0].strip()
    return head or app_name.strip()


@dataclass(frozen=True, slots=True)
class Feature:
    """One dashboard tile. `key` is what `featureChosen` emits."""

    key: str
    glyph: str
    title: str
    description: str
    #: True when choosing it leads to the drop zone rather than its own screen.
    needs_files: bool = True


FEATURES: tuple[Feature, ...] = (
    Feature(
        key="extract",
        glyph=GLYPH_EXTRACT,
        title="Extract & Format MCQs",
        description=(
            "Read a messy document and write a clean, uniformly formatted Word "
            "file — one bordered box per question."
        ),
    ),
    Feature(
        key="answers",
        glyph=GLYPH_ANSWER,
        title="Detect Correct Answer",
        description=(
            "Work out the correct option where the document has no answer key. "
            "Always flagged as AI-derived, never mixed in silently."
        ),
    ),
    Feature(
        key="pptx",
        glyph=GLYPH_DECK,
        title="Build PowerPoint Deck",
        description=(
            "Branded question deck. You choose what goes on each slide before "
            "it is built."
        ),
    ),
    Feature(
        key="merge",
        glyph=GLYPH_MERGE,
        title="Merge Multiple Files",
        description=(
            "Combine several documents into one renumbered question bank, with "
            "duplicate detection."
        ),
    ),
    Feature(
        key="bulk",
        glyph=GLYPH_BULK,
        title="Bulk Entry Automation",
        description=(
            "Paste up to 25 values and let the app enter them into the backend "
            "for you. No spreadsheet needed."
        ),
        needs_files=False,
    ),
    Feature(
        key="settings",
        glyph=GLYPH_SETTINGS,
        title="Settings",
        description=(
            "API key and model, branding, Word and PowerPoint defaults, theme "
            "and the app password."
        ),
        needs_files=False,
    ),
)


class DashboardScreen(QWidget):
    """The landing page. Emits `featureChosen` with a `Feature.key`."""

    featureChosen = Signal(str)

    def __init__(self, settings: object | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setProperty("screen", True)
        self._cards: dict[str, ActionCard] = {}
        self._columns = 3
        self._build()
        self.refresh()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        inner = QWidget()
        inner.setObjectName("screenInner")
        scroll.setWidget(inner)

        column = QVBoxLayout(inner)
        column.setContentsMargins(30, 24, 30, 20)
        column.setSpacing(18)

        column.addLayout(self._build_header(inner))

        self._grid_host = QWidget(inner)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(16)
        for feature in FEATURES:
            card = ActionCard(
                ActionSpec(
                    key=feature.key,
                    glyph=feature.glyph,
                    title=feature.title,
                    description=feature.description,
                ),
                self._grid_host,
            )
            card.activated.connect(self.featureChosen)
            self._cards[feature.key] = card
        self._relayout(self._columns)
        column.addWidget(self._grid_host)

        column.addStretch(1)
        column.addWidget(self._build_status_strip(inner))

    def _build_header(self, parent: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(4)

        eyebrow = QLabel("WELCOME BACK", parent)
        eyebrow.setObjectName("eyebrow")
        # Qt's style sheets have no letter-spacing, and an all-caps line this
        # small needs it to stop reading as one word.
        spaced = QFont(eyebrow.font())
        spaced.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        eyebrow.setFont(spaced)
        titles.addWidget(eyebrow)

        greeting = QLabel(owner_name(), parent)
        greeting.setObjectName("greeting")
        titles.addWidget(greeting)

        subtitle = QLabel("What would you like to do today?", parent)
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        row.addLayout(titles, 1)

        self.lock_button = QPushButton("Lock", parent)
        self.lock_button.setObjectName("pill")
        self.lock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lock_button.setToolTip("Lock the app and return to the password screen")
        self.lock_button.clicked.connect(lambda: self.featureChosen.emit("lock"))
        row.addWidget(self.lock_button, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _build_status_strip(self, parent: QWidget) -> QFrame:
        """What will actually run, on the bottom edge where it stays visible."""
        strip = QFrame(parent)
        strip.setObjectName("statusStrip")
        strip.setFrameShape(QFrame.Shape.NoFrame)

        row = QHBoxLayout(strip)
        row.setContentsMargins(15, 11, 13, 11)
        row.setSpacing(11)

        self._dot = QLabel(GLYPH_DOT, strip)
        self._dot.setObjectName("statusDot")
        row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignTop)

        lines = QVBoxLayout()
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(2)
        self._status_text = QLabel("", strip)
        self._status_text.setObjectName("statusText")
        self._status_text.setWordWrap(True)
        lines.addWidget(self._status_text)
        self._status_hint = QLabel("", strip)
        self._status_hint.setObjectName("statusHint")
        self._status_hint.setWordWrap(True)
        lines.addWidget(self._status_hint)
        row.addLayout(lines, 1)

        # Only one of these is ever visible: an actionable button exactly when
        # there is something to fix, and a quiet word when there is not.
        self._ready = QLabel("Ready", strip)
        self._ready.setObjectName("statusHint")
        row.addWidget(self._ready, 0, Qt.AlignmentFlag.AlignVCenter)

        self._fix_button = QPushButton("Open Settings", strip)
        self._fix_button.setObjectName("pill")
        self._fix_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fix_button.clicked.connect(lambda: self.featureChosen.emit("settings"))
        row.addWidget(self._fix_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return strip

    def _relayout(self, columns: int) -> None:
        for card in self._cards.values():
            self._grid.removeWidget(card)
        for index, feature in enumerate(FEATURES):
            self._grid.addWidget(
                self._cards[feature.key], index // columns, index % columns
            )
        for col in range(3):
            self._grid.setColumnStretch(col, 1 if col < columns else 0)
        self._columns = columns

    # -- state -------------------------------------------------------------- #

    def refresh(self) -> None:
        """Re-read the settings object, e.g. after the Settings dialog closed."""
        headline, detail, active = ai_status_lines(self.settings)
        self._status_text.setText(headline)
        self._status_hint.setText(detail)
        self._dot.setProperty("state", "on" if active else "off")
        theme.repolish(self._dot)
        self._ready.setVisible(active)
        self._fix_button.setVisible(not active)

        # Bulk entry cannot work without pyautogui, so say so on the card rather
        # than failing when it is clicked.
        card = self._cards.get("bulk")
        if card is not None:
            import importlib.util

            available = importlib.util.find_spec("pyautogui") is not None
            card.set_available(
                available,
                "pyautogui is not installed — run: pip install -r requirements.txt",
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        width = event.size().width()
        wanted = 3 if width >= _WIDE_PX else (2 if width >= _MEDIUM_PX else 1)
        if wanted != self._columns:
            self._relayout(wanted)
