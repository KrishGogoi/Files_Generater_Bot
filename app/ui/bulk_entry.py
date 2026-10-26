"""Bulk entry screen: paste the 25 values here, then drive the backend.

This replaces the spreadsheet half of the original automation. Instead of the
script clicking into Excel and copying a cell per row, the values live in the
table below -- typed, or pasted straight out of a spreadsheet in one go -- and the
run only ever touches the backend coordinates.

Everything long-running goes through `app.ui.worker`, so the window stays alive
and **Stop** actually stops: the engine polls for cancellation between steps.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

_LOG = logging.getLogger(__name__)

#: Matches the backend layout. Kept as a plain number here so the table can be
#: built before the automation package (and pyautogui) is ever imported.
BOX_COUNT = 25


class ValueTable(QTableWidget):
    """A 25-row, one-column grid with spreadsheet-style paste.

    Ctrl+V fills downward from the selected row, so a block copied out of Excel
    lands in one action instead of 25. Ctrl+X / Delete clear the selection.
    """

    pasted = Signal(int)

    def __init__(self, rows: int = BOX_COUNT, parent: QWidget | None = None) -> None:
        super().__init__(rows, 1, parent)
        self.setHorizontalHeaderLabels(["Value to paste into the backend"])
        self.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setVerticalHeaderLabels([str(i + 1) for i in range(rows)])
        self.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.setAlternatingRowColors(True)
        for r in range(rows):
            self.setItem(r, 0, QTableWidgetItem(""))

    # -- clipboard ---------------------------------------------------------- #

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for index in self.selectedIndexes():
                self.item(index.row(), 0).setText("")
            event.accept()
            return
        super().keyPressEvent(event)

    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            return
        values = _first_column(text)
        start = self.currentRow() if self.currentRow() >= 0 else 0
        written = 0
        for offset, value in enumerate(values):
            row = start + offset
            if row >= self.rowCount():
                break
            self.item(row, 0).setText(value)
            written += 1
        self.pasted.emit(written)

    # -- values ------------------------------------------------------------- #

    def values(self) -> list[str]:
        """Every non-empty cell, top to bottom."""
        out: list[str] = []
        for r in range(self.rowCount()):
            item = self.item(r, 0)
            text = (item.text() if item else "").strip()
            if text:
                out.append(text)
        return out

    def set_values(self, values: list[str]) -> None:
        for r in range(self.rowCount()):
            self.item(r, 0).setText(values[r] if r < len(values) else "")

    def clear_values(self) -> None:
        self.set_values([])


def _first_column(text: str) -> list[str]:
    """Rows of a spreadsheet paste, first cell only.

    Delegates to the automation module when it is importable so the UI and the
    engine can never disagree about what a pasted block means; the inline
    fallback keeps this screen usable if pyautogui is missing.
    """
    try:
        from app.automation.bulk_entry import parse_pasted_block
    except ImportError:
        rows = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.strip():
                first = line.split("\t")[0].strip()
                if first:
                    rows.append(first)
        return rows
    return parse_pasted_block(text)


class BulkEntryScreen(QWidget):
    """Paste values, dry-run to check the coordinates, then run for real."""

    backRequested = Signal()

    def __init__(self, settings: object | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setProperty("screen", True)
        #: `_running` is the state the buttons key off; `_thread` only keeps the
        #: QThread alive. They are separate because a job that fails immediately
        #: calls back *before* run_job returns -- assigning the thread afterwards
        #: would then overwrite the handler's reset and leave the screen stuck.
        self._running = False
        self._thread = None
        self._cancelled = False
        self._build()
        self._sync()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(26, 18, 26, 16)
        column.setSpacing(10)

        head = QLabel("Bulk Entry Automation", self)
        head.setObjectName("h1")
        column.addWidget(head)

        sub = QLabel(
            f"Paste up to {BOX_COUNT} values below — one per box, in order. When you "
            "run it, the app clicks each box on the backend, pastes the value, waits "
            "for the lookup and clicks submit. No spreadsheet is involved.",
            self,
        )
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        column.addWidget(sub)

        warn = QLabel(
            "⚠  This moves your mouse and submits real entries. Put the backend "
            "window where it normally sits, run a dry run first, and remember you "
            "can slam the pointer into a screen corner to abort.",
            self,
        )
        warn.setObjectName("notice")
        warn.setProperty("level", "warn")
        warn.setWordWrap(True)
        column.addWidget(warn)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.table = ValueTable(BOX_COUNT, self)
        self.table.pasted.connect(self._on_pasted)
        self.table.itemChanged.connect(lambda *_: self._sync())
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self.table, 3)

        body.addWidget(self._build_side_panel(), 2)
        column.addLayout(body, 1)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        column.addWidget(self.progress)

        self.status = QLabel("", self)
        self.status.setObjectName("cardDesc")
        self.status.setWordWrap(True)
        column.addWidget(self.status)

        column.addLayout(self._build_buttons())

    def _build_side_panel(self) -> QFrame:
        card = QFrame(self)
        card.setProperty("card", True)
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 11, 14, 11)
        col.setSpacing(8)

        title = QLabel("How to fill this in", card)
        title.setObjectName("cardTitle")
        col.addWidget(title)

        steps = QLabel(
            "1.  In your spreadsheet, select the column of values.\n"
            "2.  Copy them (Ctrl+C).\n"
            "3.  Click the first row here and press Ctrl+V — the whole block "
            "lands at once.\n"
            "4.  Check the rows, then Dry run.\n"
            "5.  If the coordinates look right, Run.\n\n"
            "Only the first column of a paste is used; extra columns are ignored. "
            "Blank rows are skipped, so 25 values are not required.",
            card,
        )
        steps.setObjectName("cardDesc")
        steps.setWordWrap(True)
        col.addWidget(steps)

        self.coords = QLabel("", card)
        self.coords.setObjectName("cardDesc")
        self.coords.setWordWrap(True)
        col.addWidget(self.coords)
        col.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(6)
        paste_btn = QPushButton("Paste from clipboard", card)
        paste_btn.clicked.connect(self._paste_from_clipboard)
        row.addWidget(paste_btn)
        clear_btn = QPushButton("Clear", card)
        theme.set_variant(clear_btn, "ghost")
        clear_btn.clicked.connect(self._clear)
        row.addWidget(clear_btn)
        col.addLayout(row)
        return card

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        back = QPushButton("←  Back", self)
        theme.set_variant(back, "ghost")
        back.clicked.connect(self.backRequested)
        row.addWidget(back)
        row.addStretch(1)

        self.count_label = QLabel("", self)
        self.count_label.setObjectName("cardDesc")
        row.addWidget(self.count_label)

        self.stop_button = QPushButton("Stop", self)
        theme.set_variant(self.stop_button, "danger")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)

        self.dry_button = QPushButton("Dry run", self)
        self.dry_button.clicked.connect(lambda: self._start(dry_run=True))
        row.addWidget(self.dry_button)

        self.run_button = QPushButton("Run", self)
        theme.set_variant(self.run_button, "primary")
        self.run_button.clicked.connect(lambda: self._start(dry_run=False))
        row.addWidget(self.run_button)
        return row

    # -- state -------------------------------------------------------------- #

    def _sync(self) -> None:
        n = len(self.table.values())
        self.count_label.setText(f"{n} value{'s' if n != 1 else ''} ready")
        running = self._running
        self.run_button.setEnabled(n > 0 and not running)
        self.dry_button.setEnabled(n > 0 and not running)
        self.table.setEnabled(not running)
        self.stop_button.setVisible(running)
        if not self.coords.text():
            self._show_coords()

    def _show_coords(self) -> None:
        try:
            from app.automation.bulk_entry import BulkEntryLayout
        except ImportError:
            self.coords.setText(
                "pyautogui is not installed, so this feature cannot run.\n"
                "Install it with: pip install -r requirements.txt"
            )
            self.run_button.setEnabled(False)
            self.dry_button.setEnabled(False)
            return
        layout = BulkEntryLayout()
        self.coords.setText(
            f"Backend coordinates in use: X {layout.backend_x}, "
            f"Y {layout.backend_y[0]}–{layout.backend_y[-1]} for "
            f"{layout.box_count} boxes; submit sits "
            f"{layout.paste_increment + layout.submit_increment} px below each box."
        )

    def _on_pasted(self, written: int) -> None:
        self.status.setText(f"Pasted {written} value(s) from the clipboard.")
        self._sync()

    def _paste_from_clipboard(self) -> None:
        self.table.setCurrentCell(0, 0)
        self.table._paste()

    def _clear(self) -> None:
        self.table.clear_values()
        self.status.setText("")
        self._sync()

    # -- running ------------------------------------------------------------ #

    def _start(self, *, dry_run: bool) -> None:
        values = self.table.values()
        if not values:
            return
        if not dry_run:
            confirm = QMessageBox(self)
            confirm.setWindowTitle("Run bulk entry?")
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setText(
                f"This will submit {len(values)} entries to the backend."
            )
            confirm.setInformativeText(
                "Bring the backend window to the front during the 5-second "
                "countdown. Move the mouse to a screen corner to abort."
            )
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
            )
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return

        try:
            from app.automation.bulk_entry import BulkEntryError, run_bulk_entry
            from app.ui.worker import run_job
        except ImportError as exc:
            self.status.setText(f"Cannot start: {exc}")
            return

        self._cancelled = False
        self._running = True
        self.progress.setVisible(True)
        self.progress.setRange(0, len(values))
        self.progress.setValue(0)
        self.status.setText("Starting…")
        self._sync()

        def job(progress=None):
            try:
                return run_bulk_entry(
                    values,
                    dry_run=dry_run,
                    progress=progress,
                    should_cancel=lambda: self._cancelled,
                )
            except BulkEntryError as exc:
                return exc

        thread = run_job(
            job,
            on_progress=self._on_progress,
            on_done=self._on_done,
            on_error=self._on_error,
        )
        # Only adopt the thread if the job has not already finished -- see the
        # note on _running in __init__.
        if self._running:
            self._thread = thread

    def _stop(self) -> None:
        self._cancelled = True
        self.status.setText("Stopping…")
        self.stop_button.setEnabled(False)

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.status.setText(message)

    def _on_done(self, result: object) -> None:
        self._running = False
        self._thread = None
        self.stop_button.setEnabled(True)
        self.progress.setVisible(False)
        if isinstance(result, Exception):
            self._on_error(str(result))
            return
        submitted = getattr(result, "submitted", 0)
        skipped = getattr(result, "skipped", 0)
        failed = getattr(result, "failed", 0)
        cancelled = getattr(result, "cancelled", False)
        elapsed = getattr(result, "elapsed_s", 0.0)
        dry = getattr(result, "dry_run", False)

        if dry:
            text = f"Dry run finished: {skipped} box(es) checked, nothing clicked."
        elif cancelled:
            text = f"Stopped after {submitted} entr{'y' if submitted == 1 else 'ies'}."
        else:
            text = f"Done: {submitted} submitted in {elapsed:.1f}s."
        if failed:
            text += f" {failed} row(s) failed — see the log."
        self.status.setText(text)
        self._sync()

    def _on_error(self, message: str) -> None:
        self._running = False
        self._thread = None
        self.stop_button.setEnabled(True)
        self.progress.setVisible(False)
        self.status.setText(f"Failed: {message}")
        self._sync()
