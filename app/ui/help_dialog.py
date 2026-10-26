"""The rail's Help destination: what each tile does, and the shortcuts.

Deliberately short and deliberately not a manual -- README.md is the manual. The
feature list is read straight out of `app.ui.dashboard.FEATURES`, so a tile and
its explanation here can never drift apart.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, APP_VERSION
from app.ui.dashboard import FEATURES
from app.ui.home import supported_extensions

#: (keys, what it does). Kept in step with the QActions on `MainWindow`.
SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Alt + ←", "Back to the previous screen"),
    ("Ctrl + L", "Lock the app"),
    ("Ctrl + ,", "Open Settings"),
)

WRITES = (
    "A formatted Word file, a PowerPoint deck, or both — you pick where they are "
    "saved in Settings."
)

AI_NOTE = (
    "AI is optional. Without a key the app still parses by rules; with one, "
    "answers it works out for itself are always marked AI-derived so you can "
    "check them before they go out."
)


class HelpDialog(QDialog):
    """A read-only summary of the app, opened from the nav rail."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Help — {APP_NAME}")
        self.setMinimumSize(640, 640)
        self._build()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(14)

        heading = QLabel(f"{APP_NAME} v{APP_VERSION}", self)
        heading.setObjectName("h2")
        outer.addWidget(heading)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)

        inner = QWidget()
        inner.setObjectName("screenInner")
        scroll.setWidget(inner)
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(12)

        column.addWidget(self._section("What the tiles do", self._features(inner)))
        column.addWidget(self._section("Keyboard shortcuts", self._shortcuts(inner)))
        column.addWidget(self._section("Files", self._files(inner)))
        column.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _section(self, title: str, body: QWidget) -> QWidget:
        """A labelled card, so the dialog reads as three blocks not one wall."""
        holder = QWidget(self)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(7)

        caption = QLabel(title.upper(), holder)
        caption.setObjectName("sectionLabel")
        box.addWidget(caption)

        card = QFrame(holder)
        card.setProperty("card", True)
        card.setFrameShape(QFrame.Shape.NoFrame)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(0)
        body.setParent(card)
        inner.addWidget(body)
        box.addWidget(card)
        return holder

    def _features(self, parent: QWidget) -> QWidget:
        host = QWidget(parent)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)
        for row, feature in enumerate(FEATURES):
            name = QLabel(feature.title, host)
            name.setObjectName("cardTitle")
            name.setWordWrap(True)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            what = QLabel(feature.description, host)
            what.setObjectName("cardDesc")
            what.setWordWrap(True)
            grid.addWidget(what, row, 1)
        return host

    def _shortcuts(self, parent: QWidget) -> QWidget:
        host = QWidget(parent)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        for row, (keys, what) in enumerate(SHORTCUTS):
            key_label = QLabel(keys, host)
            key_label.setObjectName("cardTitle")
            grid.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignTop)
            what_label = QLabel(what, host)
            what_label.setObjectName("cardDesc")
            what_label.setWordWrap(True)
            grid.addWidget(what_label, row, 1)
        return host

    def _files(self, parent: QWidget) -> QWidget:
        host = QWidget(parent)
        box = QVBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        # Asked of the ingest layer rather than written down, so this line cannot
        # promise a file type the app cannot actually read.
        reads = " · ".join(sorted(supported_extensions()))
        for text in (f"Reads: {reads}", f"Writes: {WRITES}", AI_NOTE):
            line = QLabel(text, host)
            line.setObjectName("cardDesc")
            line.setWordWrap(True)
            box.addWidget(line)
        return host
