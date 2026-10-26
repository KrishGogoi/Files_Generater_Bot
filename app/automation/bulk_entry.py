"""Bulk entry into the backend at fixed screen coordinates.

Ported from ``SPLIT_SCREEN_AUTOMATION.py`` with one deliberate change: **the Excel
half is gone.** The original brought Excel to the front, clicked a cell, pressed
Ctrl+C and pasted the result into the backend, walking down the sheet 26 px at a
time. Here the 25 values are supplied by the caller -- typed or pasted into the
app beforehand -- so only the *backend* coordinates are ever touched.

That removes the two most fragile things about the original: it no longer depends
on Excel being open at a particular position, and it can no longer copy the wrong
cell and submit it. What a row does now is exactly:

    click the paste field -> Ctrl+V -> wait for the backend lookup -> click submit

The coordinates below are the ones from the original script, unchanged.

Safety, because this drives the real mouse and submits real entries:

* ``pyautogui.FAILSAFE`` stays on -- slam the pointer into a screen corner to abort.
* ``should_cancel`` is polled between every step, so Stop takes effect within one
  step rather than at the end of the run.
* ``dry_run`` walks the whole sequence and reports each coordinate without
  clicking anything. Use it first whenever the window may have moved.
* Coordinates are validated against the actual screen before the first click.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

_LOG = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]


class BulkEntryCancelled(Exception):
    """Raised inside the run when the caller asked it to stop."""


class BulkEntryError(Exception):
    """Raised when the run cannot safely start."""


# --------------------------------------------------------------------------- #
# Layout -- the original script's coordinates, verbatim
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BulkEntryLayout:
    """Where the boxes are. Defaults are the original script's numbers."""

    #: Pre-click pass: one click per box before the run, to wake the fields up.
    pre_click_x: int = 175
    pre_click_y: tuple[int, ...] = (
        222, 248, 274, 300, 327,
        353, 380, 406, 433, 459,
        486, 512, 539, 565, 593,
        618, 645, 671, 698, 724,
        752, 778, 804, 830, 857,
    )

    #: The paste field of each box. X is shared; Y is per box.
    backend_x: int = 210
    backend_y: tuple[int, ...] = (
        221, 248, 274, 301, 327,
        354, 380, 407, 433, 460,
        486, 513, 539, 564, 592,
        620, 645, 672, 698, 725,
        751, 778, 804, 831, 856,
    )

    #: Submit button offset from the box's Y. The original added these in two
    #: steps (17 then 14); the sum is what matters and is kept identical.
    paste_increment: int = 17
    submit_increment: int = 14

    @property
    def box_count(self) -> int:
        return len(self.backend_y)

    def submit_y(self, index: int) -> int:
        return self.backend_y[index] + self.paste_increment + self.submit_increment


@dataclass(slots=True)
class BulkEntryTiming:
    """Delays in seconds. Only `search` is a hard requirement."""

    countdown: float = 5.0
    pre_click: float = 0.01
    settle: float = 0.3
    #: After the paste, the backend looks the Question ID up. Do not shorten
    #: this without checking that entries still land.
    search: float = 0.5
    after_submit: float = 1.0


@dataclass(slots=True)
class RowOutcome:
    index: int
    value: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class BulkEntryResult:
    submitted: int = 0
    skipped: int = 0
    cancelled: bool = False
    dry_run: bool = False
    elapsed_s: float = 0.0
    rows: list[RowOutcome] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if not r.ok)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def clean_values(raw: Sequence[str]) -> list[str]:
    """Trim, drop blanks, and keep order. What the UI grid hands over."""
    return [str(v).strip() for v in raw if str(v).strip()]


def parse_pasted_block(text: str) -> list[str]:
    """Turn a block pasted out of a spreadsheet into one value per row.

    A spreadsheet paste arrives as newline-separated rows whose cells are
    tab-separated. Only the first cell of each row is taken -- that is the
    Question ID column the original script copied.
    """
    values: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        first = line.split("\t")[0].strip()
        if first:
            values.append(first)
    return values


def _check(should_cancel: CancelFn | None) -> None:
    if should_cancel is not None and should_cancel():
        raise BulkEntryCancelled()


def _sleep(seconds: float, should_cancel: CancelFn | None) -> None:
    """Sleep in small slices so Stop is responsive during a long delay."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        _check(should_cancel)
        step = min(0.05, remaining)
        time.sleep(step)
        remaining -= step


def validate_layout(layout: BulkEntryLayout, screen: tuple[int, int]) -> list[str]:
    """Coordinates that fall outside `screen`. Empty means everything fits."""
    width, height = screen
    problems: list[str] = []
    for i, y in enumerate(layout.backend_y):
        if not (0 <= layout.backend_x < width):
            problems.append(f"backend X {layout.backend_x} is off-screen (width {width})")
            break
        if not (0 <= y < height):
            problems.append(f"box {i + 1} paste Y {y} is off-screen (height {height})")
        sy = layout.submit_y(i)
        if not (0 <= sy < height):
            problems.append(f"box {i + 1} submit Y {sy} is off-screen (height {height})")
    for i, y in enumerate(layout.pre_click_y):
        if not (0 <= y < height):
            problems.append(f"pre-click {i + 1} Y {y} is off-screen (height {height})")
    return problems


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def run_bulk_entry(
    values: Sequence[str],
    *,
    layout: BulkEntryLayout | None = None,
    timing: BulkEntryTiming | None = None,
    dry_run: bool = False,
    pre_click: bool = True,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> BulkEntryResult:
    """Paste each value into its box and submit it.

    `values` is one entry per box, in order, at most `layout.box_count` of them.
    Runs on a worker thread; `progress(done, total, message)` reports each step.
    """
    layout = layout or BulkEntryLayout()
    timing = timing or BulkEntryTiming()
    cleaned = clean_values(values)
    result = BulkEntryResult(dry_run=dry_run)

    if not cleaned:
        raise BulkEntryError("there are no values to enter")
    if len(cleaned) > layout.box_count:
        raise BulkEntryError(
            f"{len(cleaned)} values given but the screen only has "
            f"{layout.box_count} boxes"
        )

    try:
        import pyautogui
        import pyperclip
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise BulkEntryError(
            "pyautogui and pyperclip are needed for bulk entry: "
            "pip install -r requirements.txt"
        ) from exc

    pyautogui.FAILSAFE = True

    screen = tuple(pyautogui.size())
    problems = validate_layout(layout, screen)
    if problems:
        raise BulkEntryError(
            "these coordinates fall outside the "
            f"{screen[0]}x{screen[1]} screen:\n  " + "\n  ".join(problems[:6])
        )

    total = len(cleaned)
    started = time.perf_counter()

    def say(done: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)
        _LOG.info("bulk entry %d/%d: %s", done, total, message)

    try:
        # Countdown: the user has to bring the backend window to the front, and
        # this is their window to change their mind.
        whole = int(timing.countdown)
        for remaining in range(whole, 0, -1):
            say(0, f"Starting in {remaining}s — click the backend window now")
            _sleep(1.0, should_cancel)

        if pre_click and not dry_run:
            saved_pause = pyautogui.PAUSE
            pyautogui.PAUSE = 0
            try:
                for j, y in enumerate(layout.pre_click_y, start=1):
                    _check(should_cancel)
                    say(0, f"Pre-click {j}/{len(layout.pre_click_y)}")
                    pyautogui.click(layout.pre_click_x, y)
                    time.sleep(timing.pre_click)
            finally:
                pyautogui.PAUSE = saved_pause
        elif pre_click:
            say(0, f"[dry run] would pre-click {len(layout.pre_click_y)} boxes")

        for i, value in enumerate(cleaned):
            _check(should_cancel)
            paste_y = layout.backend_y[i]
            submit_y = layout.submit_y(i)
            shown = value if len(value) <= 28 else value[:25] + "..."

            if dry_run:
                say(
                    i + 1,
                    f"[dry run] box {i + 1}: paste {shown!r} at "
                    f"({layout.backend_x}, {paste_y}), submit at "
                    f"({layout.backend_x}, {submit_y})",
                )
                result.rows.append(RowOutcome(i, value, True, "dry run"))
                result.skipped += 1
                continue

            try:
                # Clipboard rather than typing: it is what the original did, and
                # a field that reacts to a paste event behaves the same way.
                pyperclip.copy(value)
                _sleep(timing.settle, should_cancel)

                say(i + 1, f"Box {i + 1}: pasting {shown}")
                pyautogui.click(layout.backend_x, paste_y)
                _sleep(timing.settle, should_cancel)
                pyautogui.hotkey("ctrl", "v")

                # The backend looks the Question ID up from the pasted value.
                _sleep(timing.search, should_cancel)

                say(i + 1, f"Box {i + 1}: submitting")
                pyautogui.moveTo(layout.backend_x, submit_y)
                _sleep(timing.settle, should_cancel)
                pyautogui.click()
                _sleep(timing.after_submit, should_cancel)

                result.rows.append(RowOutcome(i, value, True))
                result.submitted += 1
            except BulkEntryCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the run
                _LOG.warning("box %d failed: %s", i + 1, exc)
                result.rows.append(
                    RowOutcome(i, value, False, f"{type(exc).__name__}: {exc}")
                )

    except BulkEntryCancelled:
        result.cancelled = True
        say(result.submitted, "Stopped")
    finally:
        result.elapsed_s = time.perf_counter() - started

    return result
