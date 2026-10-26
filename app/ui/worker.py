"""Off-thread job runner for the PySide6 shell.

Reading a 60-question .docx, calling Gemini and writing a deck all take seconds
to minutes. None of it may happen on the GUI thread, so every long operation goes
through `run_job`, which owns four details that are easy to get wrong:

* **The thread is kept alive by a module-level reference.** A `QThread` created in
  a local variable is garbage-collected the moment the caller returns, which in
  PySide6 means a hard crash mid-run rather than an exception.
* **Callbacks are marshalled explicitly.** Connecting a signal straight to a
  plain Python function can run that function in the *worker* thread; touching a
  widget from there is undefined behaviour. So the callbacks are wrapped in a
  `QObject` that stays in the calling (GUI) thread and every connection is an
  explicit `QueuedConnection`.
* **`fn` is inspected, not assumed.** A worker function gets `progress=` and
  `cancel=` injected only if its signature accepts them, so plain one-argument
  functions still work.
* **Cancellation is cooperative.** `CancelToken` is a flag the job polls;
  `progress(...)` raises `Cancelled` as soon as it is set, so a loop that reports
  progress is cancellable for free. A cancelled job reports `failed("cancelled")`.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

_LOG = logging.getLogger(__name__)

#: The exact `failed` payload used when a job was cancelled, so callers can tell
#: "the user stopped it" from "it broke" with a string compare.
CANCELLED = "cancelled"

ProgressFn = Callable[..., None]


class Cancelled(Exception):
    """Raised inside a worker function once its `CancelToken` is set."""


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


class CancelToken:
    """A thread-safe "please stop" flag shared with the worker function.

    Deliberately not a `QObject`: worker code in `app.parse` / `app.export` must
    stay importable without Qt, so the token it receives is plain Python.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled(CANCELLED)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled (or `timeout` elapses). True if cancelled."""
        return self._event.wait(timeout)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CancelToken cancelled={self.cancelled}>"


# --------------------------------------------------------------------------- #
# The job payload
# --------------------------------------------------------------------------- #


def _injectable(fn: Callable[..., Any]) -> frozenset[str]:
    """Which of our optional keywords `fn` will actually accept.

    Builtins and C functions have no introspectable signature; injecting blindly
    would raise `TypeError` before the job even starts, so we inject nothing.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return frozenset()
    names: set[str] = set()
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return frozenset({"progress", "cancel"})
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(name)
    return frozenset(names & {"progress", "cancel"})


def _describe(exc: BaseException) -> str:
    """A short, user-facing one-liner for an exception."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class Job(QObject):
    """Runs one callable and reports back over signals.

    Lives in the worker thread after `run_job` moves it there; `run` is the slot
    the thread's `started` signal fires.
    """

    progress = Signal(int, int, str)   # done, total, message
    finished = Signal(object)          # result payload (may be None)
    failed = Signal(str)               # human-readable reason, or CANCELLED

    def __init__(
        self,
        fn: Callable[..., Any],
        args: Iterable[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        token: CancelToken | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args: tuple[Any, ...] = tuple(args)
        self._kwargs: dict[str, Any] = dict(kwargs or {})
        self.token = token or CancelToken()

    # -- control ----------------------------------------------------------- #

    def cancel(self) -> None:
        """Ask the running function to stop at its next checkpoint."""
        self.token.cancel()

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled

    @property
    def label(self) -> str:
        return getattr(self._fn, "__name__", type(self._fn).__name__)

    # -- the callable handed to the worker function ------------------------- #

    def report(self, done: int = 0, total: int = -1, message: str = "") -> None:
        """`progress` as seen by the worker function.

        Raises `Cancelled` when the token is set: reporting progress is the
        natural place for a long loop to notice it should stop.
        """
        self.token.raise_if_cancelled()
        try:
            self.progress.emit(int(done), int(total), str(message))
        except (TypeError, ValueError):
            # Never let a badly-typed progress call kill an otherwise good run.
            _LOG.debug("ignoring malformed progress report from %s", self.label)

    # -- execution --------------------------------------------------------- #

    @Slot()
    def run(self) -> None:
        """Execute the callable. Emits exactly one of `finished` / `failed`."""
        if self.token.cancelled:
            self.failed.emit(CANCELLED)
            return

        kwargs = dict(self._kwargs)
        accepted = _injectable(self._fn)
        if "progress" in accepted and "progress" not in kwargs:
            kwargs["progress"] = self.report
        if "cancel" in accepted and "cancel" not in kwargs:
            kwargs["cancel"] = self.token

        try:
            result = self._fn(*self._args, **kwargs)
        except Cancelled:
            _LOG.info("job %s cancelled", self.label)
            self.failed.emit(CANCELLED)
            return
        except Exception as exc:  # noqa: BLE001 - a job must never kill the app
            _LOG.exception("job %s failed", self.label)
            self.failed.emit(_describe(exc))
            return

        if self.token.cancelled:
            # Cancelled during the final stretch: the result is not trustworthy.
            self.failed.emit(CANCELLED)
            return
        self.finished.emit(result)


# --------------------------------------------------------------------------- #
# GUI-side callback bridge
# --------------------------------------------------------------------------- #


class _Callbacks(QObject):
    """Holds the caller's callbacks and invokes them in the caller's thread.

    Never moved to the worker thread; combined with `QueuedConnection` that is
    what guarantees the callbacks run where the widgets live.
    """

    def __init__(
        self,
        on_progress: Callable[[int, int, str], None] | None,
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[str], None] | None,
        thread: QThread | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_error = on_error
        self._thread = thread

    @staticmethod
    def _safe(cb: Callable[..., None] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:  # noqa: BLE001 - a broken UI callback must not crash
            _LOG.exception("job callback %r raised", getattr(cb, "__name__", cb))

    @Slot(int, int, str)
    def handle_progress(self, done: int, total: int, message: str) -> None:
        self._safe(self._on_progress, done, total, message)

    @Slot(object)
    def handle_done(self, result: object) -> None:
        self._safe(self._on_done, result)

    @Slot(str)
    def handle_error(self, message: str) -> None:
        self._safe(self._on_error, message)

    @Slot()
    def handle_thread_finished(self) -> None:
        """Drop the module-level reference once the thread has really stopped."""
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        # `finished` fires while the OS thread is still tearing down; destroying
        # the QThread before that completes prints "destroyed while running".
        thread.wait(3000)
        _forget(thread)


# --------------------------------------------------------------------------- #
# Thread + registry
# --------------------------------------------------------------------------- #


class JobThread(QThread):
    """A `QThread` that carries its `Job`, so callers can cancel through it."""

    def __init__(self, job: Job, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.job = job

    def cancel(self) -> None:
        self.job.cancel()

    @property
    def cancelled(self) -> bool:
        return self.job.cancelled


#: Every thread currently running. This is the reference that keeps the QThread
#: and its Job alive; without it PySide6 collects them mid-run.
_ACTIVE: set[JobThread] = set()
_LOCK = threading.Lock()


def _remember(thread: JobThread) -> None:
    with _LOCK:
        _ACTIVE.add(thread)


def _forget(thread: JobThread) -> None:
    with _LOCK:
        _ACTIVE.discard(thread)


def active_jobs() -> int:
    """How many jobs are still running -- used by the window close handler."""
    with _LOCK:
        return len(_ACTIVE)


def cancel_all() -> None:
    """Set the cancel flag on every running job (does not wait)."""
    with _LOCK:
        threads = list(_ACTIVE)
    for thread in threads:
        thread.cancel()


def wait_all(timeout_ms: int = 5000) -> bool:
    """Cancel everything and wait. True when all threads stopped in time."""
    cancel_all()
    with _LOCK:
        threads = list(_ACTIVE)
    ok = True
    for thread in threads:
        thread.quit()
        if not thread.wait(timeout_ms):
            _LOG.warning("job thread did not stop within %d ms", timeout_ms)
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def run_job(
    fn: Callable[..., Any],
    *args: Any,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> QThread:
    """Run `fn(*args, **kwargs)` on a worker thread; report back on the GUI one.

    `fn` may declare a ``progress`` parameter (called as
    ``progress(done, total, message)``) and/or a ``cancel`` parameter (a
    `CancelToken`); both are injected only when the signature accepts them.

    Returns the started thread. Call ``thread.cancel()`` (or
    ``thread.job.cancel()``) to stop it -- the job then reports
    ``on_error(CANCELLED)``. The thread is referenced module-side until it
    finishes, so the caller may discard the return value.
    """
    job = Job(fn, args, kwargs)
    thread = JobThread(job)
    job.moveToThread(thread)

    # The bridge stays in the caller's thread (that is the whole point); the
    # thread object keeps it alive for as long as the job can still signal.
    bridge = _Callbacks(on_progress, on_done, on_error, thread=thread)
    thread.bridge = bridge  # type: ignore[attr-defined]

    queued = Qt.ConnectionType.QueuedConnection
    job.progress.connect(bridge.handle_progress, queued)
    job.finished.connect(bridge.handle_done, queued)
    job.failed.connect(bridge.handle_error, queued)
    # Either terminal signal ends the worker's event loop.
    job.finished.connect(thread.quit, queued)
    job.failed.connect(thread.quit, queued)
    thread.finished.connect(bridge.handle_thread_finished, queued)
    # Auto-connection: `started` is emitted in the worker thread and the job now
    # belongs to it, so `run` executes there.
    thread.started.connect(job.run)

    _remember(thread)
    thread.start()
    _LOG.debug("job %s started (%d active)", job.label, active_jobs())
    return thread
