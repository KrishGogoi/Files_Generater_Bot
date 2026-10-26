"""The action menu shown once files have been dropped.

Four large cards, one per thing the app can do. The screen is deliberately dumb:
it reports which action was picked via `actionChosen` and lets `main.py` own the
pipeline. `app.parse` / `app.export` are never imported here, so switching
screens stays instant.

Card styling lives in `app.ui.theme` under the `#actionCard` / `#cardGlyph` /
`#cardTitle` / `#cardDesc` / `#cardReason` selectors -- keep the object names in
step with that stylesheet.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

_LOG = logging.getLogger(__name__)

#: Below this width the 2x2 grid reflows to a single column.
_NARROW_PX = 900

GLYPH_EXTRACT = "❖"
GLYPH_ANSWER = "✓"
GLYPH_DECK = "▤"
GLYPH_MERGE = "⇄"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One entry in the menu. `key` is what `actionChosen` emits."""

    key: str
    glyph: str
    title: str
    description: str
    #: Minimum number of queued files this action needs.
    min_files: int = 1
    #: Shown on the card when it is disabled, explaining why.
    disabled_reason: str = ""
    #: Which entry of `theme.TINTS` colours the icon square. Defaults to `key`,
    #: which is how every card in the app is currently matched to its hue.
    tint: str = ""
    #: The call to action along the bottom edge.
    cta: str = "Get started"


ACTIONS: tuple[ActionSpec, ...] = (
    ActionSpec(
        key="extract",
        glyph=GLYPH_EXTRACT,
        # Plain "&": these titles render in a QLabel, which does not consume
        # mnemonics the way a QPushButton does, so "&&" would show literally.
        title="Extract & Format MCQs",
        description=(
            "Detect every question, option and answer, then write a clean, "
            "uniformly formatted Word file."
        ),
        cta="Run this",
    ),
    ActionSpec(
        key="answers",
        glyph=GLYPH_ANSWER,
        title="Detect Correct Answer",
        description=(
            "Work out the correct option where the document has no answer key. "
            "Always marked as AI-derived, never silently mixed in."
        ),
        cta="Run this",
    ),
    ActionSpec(
        key="pptx",
        glyph=GLYPH_DECK,
        title="Build PowerPoint Deck",
        description=(
            "Generate a branded question deck — title slide, one slide per "
            "question, answers on their own slides."
        ),
        cta="Run this",
    ),
    ActionSpec(
        key="merge",
        glyph=GLYPH_MERGE,
        title="Merge Multiple Files",
        description=(
            "Combine several documents into one renumbered question bank, with "
            "duplicate detection."
        ),
        min_files=2,
        disabled_reason="Needs at least 2 files",
        cta="Run this",
    ),
)


def _has_api_key(settings: object | None) -> bool:
    """True when some usable key exists. Never logs or returns the key itself.

    Mirrors the probe in `app.ui.home` so the footer here and the banner there
    can never disagree about whether AI is available.
    """
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
        except Exception:  # noqa: BLE001 - a cosmetic footer must never raise
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


def ai_status_lines(settings: object | None) -> tuple[str, str, bool]:
    """(headline, detail, ai_is_active) describing what will actually run.

    Split in two because the dashboard's status strip sets the headline and the
    detail in different sizes, while `_ai_status` wants them as one sentence.
    """
    use_ai = bool(getattr(settings, "use_ai", True))
    provider = str(getattr(settings, "provider", "gemini") or "").strip().lower()
    model = str(getattr(settings, "model", "") or "").strip()

    if provider in ("", "none"):
        return ("Rule-based only", "Pick a provider in Settings for best accuracy.", False)
    if not use_ai:
        return ("AI is switched off in Settings", "Questions are parsed by rules only.", False)
    if not _has_api_key(settings):
        return ("Rule-based only", "Add a free API key in Settings for best accuracy.", False)
    return (
        f"AI detection active — {model or provider}",
        "Answers are cross-checked against the source.",
        True,
    )


def _ai_status(settings: object | None) -> tuple[str, bool]:
    """(footer text, ai_is_active) -- the one-line form of `ai_status_lines`."""
    headline, detail, active = ai_status_lines(settings)
    return (f"{headline}. {detail}", active)


#: Size of the rounded icon square at the top of a card, and of the circular
#: arrow at its bottom-right. Both are fixed here rather than in the style sheet
#: so the two stay square whatever the font metrics do to the rest of the card.
_TILE_PX = 42
_ARROW_PX = 26

GLYPH_ARROW = "→"


class ActionCard(QFrame):
    """A whole-card click target: icon square, title, description, call to action.

    Focusable and Enter/Space-activatable so the menu is usable without a mouse.
    The arrow and the "Get started" line are plain labels, not buttons: a nested
    click target inside a card that is itself one click target only produces dead
    spots where the user expects a hit.
    """

    activated = Signal(str)

    def __init__(self, spec: ActionSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setObjectName("actionCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName(spec.title.replace("&&", "&"))
        self.setAccessibleDescription(spec.description)

        column = QVBoxLayout(self)
        column.setContentsMargins(17, 15, 17, 14)
        column.setSpacing(8)

        self._glyph = QLabel(spec.glyph, self)
        self._glyph.setObjectName("cardTile")
        # The hue is picked by a generated [tint="..."] rule in the style sheet.
        self._glyph.setProperty("tint", spec.tint or spec.key)
        self._glyph.setFixedSize(_TILE_PX, _TILE_PX)
        self._glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self._glyph)
        column.addSpacing(2)

        self._title = QLabel(spec.title, self)
        self._title.setObjectName("cardTitle")
        self._title.setWordWrap(True)
        column.addWidget(self._title)

        self._desc = QLabel(spec.description, self)
        self._desc.setObjectName("cardDesc")
        self._desc.setWordWrap(True)
        column.addWidget(self._desc, 1)

        # Only ever visible while the card is disabled, so the user is told why
        # rather than left poking at a dead card.
        self._reason = QLabel("", self)
        self._reason.setObjectName("cardReason")
        self._reason.setWordWrap(True)
        self._reason.setVisible(False)
        column.addWidget(self._reason)

        self._cta_row = QWidget(self)
        cta_row = QHBoxLayout(self._cta_row)
        cta_row.setContentsMargins(0, 4, 0, 0)
        cta_row.setSpacing(8)
        self._cta = QLabel(spec.cta, self._cta_row)
        self._cta.setObjectName("cardCta")
        cta_row.addWidget(self._cta, 0, Qt.AlignmentFlag.AlignVCenter)
        cta_row.addStretch(1)
        self._arrow = QLabel(GLYPH_ARROW, self._cta_row)
        self._arrow.setObjectName("cardArrow")
        self._arrow.setFixedSize(_ARROW_PX, _ARROW_PX)
        self._arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cta_row.addWidget(self._arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addWidget(self._cta_row)

    # -- enable/disable ----------------------------------------------------- #

    def set_available(self, available: bool, reason: str = "") -> None:
        """Enable or disable the card, showing `reason` when disabled."""
        self.setEnabled(available)
        text = "" if available else (reason or self.spec.disabled_reason)
        self._reason.setText(text)
        self._reason.setVisible(bool(text))
        # A dead card must not still say "Get started".
        self._cta_row.setVisible(available)
        self.setToolTip("" if available else text)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if available
            else Qt.CursorShape.ForbiddenCursor
        )
        theme.repolish(self)

    # -- activation --------------------------------------------------------- #

    def _fire(self) -> None:
        if self.isEnabled():
            self.activated.emit(self.spec.key)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # Only count a release that landed inside the card, so a drag-off cancels.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self._fire()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self._fire()
            event.accept()
            return
        super().keyPressEvent(event)


class ActionMenu(QWidget):
    """The screen shown between the drop zone and the review table."""

    actionChosen = Signal(str)
    backRequested = Signal()

    def __init__(self, settings: object | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._paths: list[str] = []
        self._cards: dict[str, ActionCard] = {}
        self._columns = 2
        self.setProperty("screen", True)

        self._build()
        self._sync()

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

        self._heading = QLabel("No files yet", inner)
        self._heading.setObjectName("h1")
        column.addWidget(self._heading)

        self._sub = QLabel("Choose what to do with them.", inner)
        self._sub.setObjectName("subtitle")
        self._sub.setWordWrap(True)
        column.addWidget(self._sub)

        self._grid_host = QWidget(inner)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 2, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(16)
        for spec in ACTIONS:
            card = ActionCard(spec, self._grid_host)
            card.activated.connect(self.actionChosen)
            self._cards[spec.key] = card
        self._relayout(self._columns)
        column.addWidget(self._grid_host)

        column.addStretch(1)

        self._footer = QLabel("", inner)
        self._footer.setObjectName("cardDesc")
        self._footer.setWordWrap(True)
        column.addWidget(self._footer)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        back = QPushButton("←  Back", inner)
        back.setProperty("variant", "ghost")
        back.clicked.connect(self.backRequested)
        row.addWidget(back, 0)
        row.addStretch(1)
        column.addLayout(row)

    def _relayout(self, columns: int) -> None:
        """Re-place the cards in `columns` columns, keeping declaration order."""
        for card in self._cards.values():
            self._grid.removeWidget(card)
        for index, spec in enumerate(ACTIONS):
            self._grid.addWidget(
                self._cards[spec.key], index // columns, index % columns
            )
        for col in range(2):
            self._grid.setColumnStretch(col, 1 if col < columns else 0)
        self._columns = columns

    # -- state -------------------------------------------------------------- #

    def set_files(self, paths: list[str] | None) -> None:
        """Called by main.py once the home screen hands over its file list."""
        self._paths = [str(p) for p in (paths or []) if str(p).strip()]
        self._sync()

    #: main.py probes for several spellings; keep the aliases cheap.
    setFiles = set_files
    update_files = set_files
    set_paths = set_files

    def files(self) -> list[str]:
        return list(self._paths)

    def refresh(self) -> None:
        """Re-read the settings object (e.g. after the Settings dialog closed)."""
        self._sync()

    def _sync(self) -> None:
        count = len(self._paths)
        if count == 0:
            self._heading.setText("No files yet")
            self._sub.setText("Go back and drop some question documents first.")
        else:
            names = ", ".join(os.path.basename(p) for p in self._paths[:3])
            if count > 3:
                names += f", +{count - 3} more"
            self._heading.setText(f"{count} file{'s' if count != 1 else ''} ready")
            self._sub.setText(names)

        for spec in ACTIONS:
            self._cards[spec.key].set_available(count >= spec.min_files)

        text, active = _ai_status(self.settings)
        self._footer.setText(text)
        self._footer.setProperty("level", "info" if active else "warn")
        theme.repolish(self._footer)

    # -- responsive --------------------------------------------------------- #

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        wanted = 1 if event.size().width() < _NARROW_PX else 2
        if wanted != self._columns:
            self._relayout(wanted)
