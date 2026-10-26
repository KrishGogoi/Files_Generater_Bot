"""The left-hand nav rail: identity at the top, destinations, quote, version.

The rail replaced the old `QToolBar`, so it carries the same security property
the toolbar did: `MainWindow` hides it while the lock screen is up, and nothing
on it can be reached from behind the password.

It reports a destination key through `navigate` and never decides anything
itself -- including which item looks active. `MainWindow` owns that, because
"Settings" opens a dialog and comes straight back rather than becoming the
current screen, and only the window knows the difference.

Styling lives in `app.ui.theme` under ``#sidebar`` / ``#navItem`` / ``#navGlyph``
/ ``#navText`` / ``#quoteCard``; keep the object names in step with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

_LOG = logging.getLogger(__name__)

#: Wide enough for "Bulk Entry Automation"-length labels at 13px without the
#: rail starting to compete with the content for the eye.
RAIL_PX = 218

#: Shown in the bottom-left corner of the window, under the nav.
QUOTE = "Why do we fall? So that we can learn to pick ourselves back up."

TAGLINE = "Work Smarter. Save Time."

GLYPH_HOME = "⌂"
GLYPH_HISTORY = "↺"
GLYPH_SETTINGS = "⚙"
GLYPH_HELP = "?"


@dataclass(frozen=True, slots=True)
class NavSpec:
    """One rail entry. `key` is what `navigate` emits."""

    key: str
    glyph: str
    label: str
    tip: str


NAV: tuple[NavSpec, ...] = (
    NavSpec("home", GLYPH_HOME, "Home", "Every feature on one screen"),
    NavSpec("history", GLYPH_HISTORY, "History", "Files you worked on recently"),
    NavSpec("settings", GLYPH_SETTINGS, "Settings", "API key, branding, theme, password"),
    NavSpec("help", GLYPH_HELP, "Help", "What each feature does, and the shortcuts"),
)


class NavItem(QFrame):
    """One row of the rail: glyph, label, whole-row click target.

    Focusable and Enter/Space-activatable, so the rail is usable from the
    keyboard like the toolbar it replaced.
    """

    activated = Signal(str)

    def __init__(self, spec: NavSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setObjectName("navItem")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(spec.tip)
        self.setAccessibleName(spec.label)
        self.setAccessibleDescription(spec.tip)

        row = QHBoxLayout(self)
        row.setContentsMargins(11, 8, 11, 8)
        row.setSpacing(11)

        self._glyph = QLabel(spec.glyph, self)
        self._glyph.setObjectName("navGlyph")
        self._glyph.setFixedWidth(15)
        self._glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._glyph)

        self._text = QLabel(spec.label, self)
        self._text.setObjectName("navText")
        row.addWidget(self._text, 1)

        self.set_active(False)

    def set_active(self, active: bool) -> None:
        """Mark this row as the screen currently showing.

        The property is set on the two labels as well as on the row: Qt resolves
        a child's rules against the child's own properties, so a descendant
        selector off the row would leave the glyph and the label unstyled.
        """
        for widget in (self, self._glyph, self._text):
            widget.setProperty("active", "true" if active else "false")
            theme.repolish(widget)

    # -- activation --------------------------------------------------------- #

    def _fire(self) -> None:
        if self.isEnabled():
            self.activated.emit(self.spec.key)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        # Only a release that landed inside the row counts, so a drag-off cancels.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self._fire()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._fire()
            event.accept()
            return
        super().keyPressEvent(event)


class Sidebar(QWidget):
    """The nav rail. Emits `navigate` with a `NavSpec.key`."""

    navigate = Signal(str)

    def __init__(
        self,
        app_name: str = "Krishna's Super Bot",
        version: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        # A plain QWidget subclass ignores a style sheet background unless it is
        # told to draw one; without this the rail would be a hole in the window.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(RAIL_PX)
        self._items: dict[str, NavItem] = {}
        self._build(app_name, version)

    # -- construction ------------------------------------------------------- #

    def _build(self, app_name: str, version: str) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 18, 14, 14)
        column.setSpacing(0)

        column.addLayout(self._build_identity(app_name))
        column.addSpacing(22)

        for spec in NAV:
            item = NavItem(spec, self)
            item.activated.connect(self.navigate)
            self._items[spec.key] = item
            column.addWidget(item)
            column.addSpacing(3)

        column.addStretch(1)

        quote_card = QFrame(self)
        quote_card.setObjectName("quoteCard")
        quote_card.setFrameShape(QFrame.Shape.NoFrame)
        quote_box = QVBoxLayout(quote_card)
        quote_box.setContentsMargins(12, 11, 12, 11)
        quote = QLabel(QUOTE, quote_card)
        quote.setObjectName("quoteText")
        quote.setWordWrap(True)
        quote_box.addWidget(quote)
        column.addWidget(quote_card)

        column.addSpacing(11)
        self._version = QLabel(f"v{version}" if version else "", self)
        self._version.setObjectName("sidebarVersion")
        self._version.setVisible(bool(version))
        column.addWidget(self._version)

    def _build_identity(self, app_name: str) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setContentsMargins(2, 0, 2, 0)
        # Tight: the name and the tagline are one block, separated from the logo
        # by an explicit gap rather than by uniform spacing.
        box.setSpacing(3)

        # The app's own logo, never `BrandConfig.logo_path` -- that is the academy
        # mark and it belongs on the PowerPoint title slide, not in the chrome.
        badge_pixmap = theme.app_logo(40, square=True)
        if not badge_pixmap.isNull():
            badge = QLabel(self)
            badge.setPixmap(badge_pixmap)
            badge.setFixedSize(badge_pixmap.size())
            box.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            box.addSpacing(9)

        title = QLabel(app_name, self)
        title.setObjectName("sidebarTitle")
        title.setWordWrap(True)
        box.addWidget(title)

        tagline = QLabel(TAGLINE, self)
        tagline.setObjectName("sidebarTagline")
        tagline.setWordWrap(True)
        box.addWidget(tagline)
        return box

    # -- state -------------------------------------------------------------- #

    def set_active(self, key: str) -> None:
        """Highlight one destination, or none at all for an unknown key."""
        for item_key, item in self._items.items():
            item.set_active(item_key == key)

    def set_enabled_item(self, key: str, enabled: bool) -> None:
        """Grey out one destination (used while a job is running)."""
        item = self._items.get(key)
        if item is None:
            _LOG.debug("no rail item named %r", key)
            return
        item.setEnabled(enabled)
        item.setCursor(
            Qt.CursorShape.PointingHandCursor
            if enabled
            else Qt.CursorShape.ForbiddenCursor
        )
