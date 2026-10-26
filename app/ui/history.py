"""Recent files, read back out of `Settings.recent_files`.

The app has always remembered what it worked on (the drop screen pushes every
accepted batch onto `recent_files`); this screen is just the first place that
shows it. Nothing here re-runs anything on its own: picking a row loads that file
into the drop screen, where the user still chooses and confirms the action.

A row whose file has since been moved or deleted is shown greyed and is not
clickable, rather than quietly disappearing -- "it was here yesterday" deserves
an answer, not a shorter list.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

_LOG = logging.getLogger(__name__)


def _size_text(path: Path) -> str:
    """A short, human size for `path`, or "" when it cannot be measured."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


class HistoryRow(QFrame):
    """One remembered file: name, where it lives, how big it is."""

    activated = Signal(str)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        target = Path(path)
        self.exists = target.is_file()

        self.setObjectName("chip")
        self.setProperty("clickable", "true" if self.exists else "false")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setEnabled(self.exists)
        if self.exists:
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Load {target.name} into the drop screen")
        else:
            self.setToolTip("This file is no longer where it was")

        row = QHBoxLayout(self)
        row.setContentsMargins(13, 9, 13, 9)
        row.setSpacing(12)

        lines = QVBoxLayout()
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(2)
        name = QLabel(target.name, self)
        name.setObjectName("chipName")
        lines.addWidget(name)

        details = [str(target.parent)]
        if self.exists:
            size = _size_text(target)
            if size:
                details.append(size)
        else:
            details.append("missing")
        meta = QLabel(" · ".join(details), self)
        meta.setObjectName("chipMeta")
        meta.setWordWrap(True)
        lines.addWidget(meta)
        row.addLayout(lines, 1)

        if self.exists:
            cta = QLabel("Load →", self)
            cta.setObjectName("cardCta")
            row.addWidget(cta, 0, Qt.AlignmentFlag.AlignVCenter)

    # -- activation --------------------------------------------------------- #

    def _fire(self) -> None:
        if self.isEnabled():
            self.activated.emit(self.path)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
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


class HistoryScreen(QWidget):
    """The rail's History destination."""

    #: A row was picked: load these into the drop screen.
    filesChosen = Signal(list)
    backRequested = Signal()
    #: The list was emptied and `settings` now needs saving.
    cleared = Signal()

    def __init__(self, settings: object | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setProperty("screen", True)
        self._rows: list[HistoryRow] = []
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
        column.setSpacing(14)

        heading = QLabel("History", inner)
        heading.setObjectName("h1")
        column.addWidget(heading)

        subtitle = QLabel(
            "Files you have worked on recently. Pick one to load it back in.",
            inner,
        )
        subtitle.setObjectName("subtitle")
        column.addWidget(subtitle)

        card = QFrame(inner)
        card.setProperty("card", True)
        card.setFrameShape(QFrame.Shape.NoFrame)
        self._list = QVBoxLayout(card)
        self._list.setContentsMargins(14, 14, 14, 14)
        self._list.setSpacing(8)

        self._empty = QLabel(
            "Nothing yet. Files show up here once you have processed them.",
            card,
        )
        self._empty.setObjectName("cardDesc")
        self._empty.setWordWrap(True)
        self._list.addWidget(self._empty)
        column.addWidget(card)

        column.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        back = QPushButton("←  Back", inner)
        theme.set_variant(back, "ghost")
        back.clicked.connect(self.backRequested)
        buttons.addWidget(back, 0)
        buttons.addStretch(1)
        self._clear_button = QPushButton("Clear history", inner)
        theme.set_variant(self._clear_button, "ghost")
        self._clear_button.clicked.connect(self._clear)
        buttons.addWidget(self._clear_button, 0)
        column.addLayout(buttons)

    # -- state -------------------------------------------------------------- #

    def _recent(self) -> list[str]:
        values = getattr(self.settings, "recent_files", None) or []
        return [str(p) for p in values if str(p).strip()]

    def refresh(self) -> None:
        """Rebuild the list from `settings.recent_files`."""
        for row in self._rows:
            self._list.removeWidget(row)
            # Un-parent as well: `deleteLater` only schedules the delete, so a
            # row that is merely out of the layout carries on painting itself at
            # its old geometry until the event loop gets round to it.
            row.setParent(None)
            row.deleteLater()
        self._rows = []

        paths = self._recent()
        for path in paths:
            row = HistoryRow(path, self)
            row.activated.connect(lambda p: self.filesChosen.emit([p]))
            self._list.addWidget(row)
            self._rows.append(row)

        self._empty.setVisible(not paths)
        self._clear_button.setEnabled(bool(paths))

    def set_settings(self, settings: object | None) -> None:
        self.settings = settings
        self.refresh()

    def _clear(self) -> None:
        if self.settings is None:
            return
        try:
            self.settings.recent_files = []  # type: ignore[attr-defined]
        except AttributeError:
            _LOG.debug("settings object has no recent_files to clear")
            return
        self.refresh()
        self.cleared.emit()
