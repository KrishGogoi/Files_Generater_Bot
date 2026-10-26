#!/usr/bin/env python
"""Krishna's Super Bot -- entry point.

Two ways in, one pipeline:

* ``python main.py``            the PySide6 desktop app (Home -> Actions -> Review)
* ``python main.py --cli ...``  the same pipeline headless, so the whole thing is
                                testable without a display

`run_pipeline` is the single place that knows the order of the stages, and both
entry paths call it. Every stage module is imported *lazily*, for two reasons:
a missing or broken module must produce a sentence the user can act on instead of
an ImportError traceback at startup, and the CLI must not drag in Qt (nor the GUI
an AI SDK it will never use).

The other rule this file enforces: a dead, key-less or rate-limited AI provider
degrades to the rule-based parser and adds a warning. It never fails a run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

# Running "python main.py" already puts this folder on sys.path; doing it
# explicitly also covers being launched by an absolute path from elsewhere.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import (  # noqa: E402  (must follow the sys.path fix-up)
    APP_NAME,
    APP_VERSION,
    Settings,
    load_settings,
    resolve_api_key,
    save_settings,
)
from app.models import BrandConfig, ParseResult, Parser, Question  # noqa: E402

LOG = logging.getLogger("krishnas_super_bot")

# --------------------------------------------------------------------------- #
# Qt is optional: --cli has to work on a box where PySide6 is missing or broken.
# --------------------------------------------------------------------------- #

_QT_ERROR = ""
try:
    from PySide6.QtCore import QSettings, Qt, QUrl
    from PySide6.QtGui import QAction, QDesktopServices, QPainter
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as _qt_exc:  # pragma: no cover - depends on the install
    _QT_ERROR = str(_qt_exc)
    QMainWindow = object  # type: ignore[assignment,misc]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ACTIONS: tuple[str, ...] = ("extract", "answers", "pptx", "merge")

ACTION_LABELS: dict[str, str] = {
    "extract": "Extract & format MCQs",
    "answers": "Detect correct answers",
    "pptx": "Build PowerPoint deck",
    "merge": "Merge multiple files",
}

#: Which exporter each action ends in.
ACTION_EXPORT: dict[str, str] = {
    "extract": "word",
    "answers": "word",
    "pptx": "pptx",
    "merge": "word",
}

EXPORT_EXT: dict[str, str] = {"word": ".docx", "pptx": ".pptx"}
EXPORT_TAG: dict[str, str] = {"word": "_formatted", "pptx": "_deck"}

NO_KEY_BANNER = (
    "Running without AI. Add a free Gemini key in Settings for best accuracy."
)

#: Used only when app.ingest cannot be imported, so --help/globbing still work.
_FALLBACK_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".pptx"})

ProgressFn = Callable[..., None]


class PipelineError(RuntimeError):
    """A module the requested action needs is missing or unusable.

    Raised with a message meant for the user, not a traceback.
    """


@dataclass(slots=True)
class _JobFailure:
    """Carried back from a worker thread when the job raised.

    Belt-and-braces: `worker.run_job` is expected to emit `failed`, but a
    background exception must never be able to take the window down with it.
    """

    message: str


# --------------------------------------------------------------------------- #
# Progress plumbing
# --------------------------------------------------------------------------- #


def _emit(progress: ProgressFn | None, done: int, total: int, message: str = "") -> None:
    """Report progress, tolerating a 2-arg or 3-arg callback.

    The GUI hands in `Job.progress.emit` (done, total, message) while the parse
    stages use (done, total); accepting both keeps the callers simple.
    """
    if progress is None:
        return
    try:
        progress(done, total, message)
        return
    except TypeError:
        pass  # fall through to the two-argument form
    except Exception as exc:  # noqa: BLE001 - a bad UI callback must not kill a run
        LOG.debug("progress callback failed: %s", exc)
        return
    try:
        progress(done, total)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("progress callback failed: %s", exc)


def _stage_progress(
    progress: ProgressFn | None, done: int, total: int, label: str
) -> Callable[[int, int], None]:
    """Adapt a stage's (done, total) reporting onto the file-level counters.

    The bar stays monotonic per file; the stage's own numbers go in the message.
    """

    def cb(sub_done: int, sub_total: int) -> None:
        detail = f"{label} {sub_done}/{sub_total}" if sub_total else label
        _emit(progress, done, total, detail)

    return cb


# --------------------------------------------------------------------------- #
# Lazy module loading
# --------------------------------------------------------------------------- #


def _import(name: str, *, purpose: str, required: bool = True) -> ModuleType | None:
    """Import `name`, turning a missing module into a readable message."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        msg = f"{purpose} is unavailable - could not import {name} ({exc})"
        if required:
            raise PipelineError(msg) from exc
        LOG.warning("%s", msg)
        return None
    except Exception as exc:  # noqa: BLE001 - a module that explodes on import
        msg = f"{purpose} failed to load from {name} ({type(exc).__name__}: {exc})"
        if required:
            raise PipelineError(msg) from exc
        LOG.warning("%s", msg)
        return None


def supported_extensions() -> frozenset[str]:
    """Extensions the ingest layer can read (fallback list if it is missing)."""
    ingest = _import("app.ingest", purpose="document readers", required=False)
    exts = getattr(ingest, "SUPPORTED_EXTENSIONS", None) if ingest else None
    if isinstance(exts, (set, frozenset, tuple, list)) and exts:
        return frozenset(str(e).lower() for e in exts)
    return _FALLBACK_EXTENSIONS


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FileOutcome:
    """What one input file produced -- kept per file so the CLI can report it."""

    path: str
    result: ParseResult | None = None
    elapsed_s: float = 0.0
    error: str = ""
    exports: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.result is not None

    @property
    def questions(self) -> list[Question]:
        return list(self.result.questions) if self.result is not None else []

    @property
    def n_flagged(self) -> int:
        return self.result.n_flagged if self.result is not None else 0


def make_provider(settings: Settings) -> tuple[Any | None, list[str]]:
    """Build the AI provider, or explain in plain words why there is none."""
    if not settings.use_ai:
        return None, ["AI is switched off in Settings - using the rule-based parser."]
    if settings.provider.strip().lower() == "none":
        return None, ["Provider is set to 'none' - using the rule-based parser."]
    if not resolve_api_key(settings):
        return None, [NO_KEY_BANNER]

    providers = _import("app.parse.providers", purpose="AI providers", required=False)
    if providers is None or not hasattr(providers, "make_provider"):
        return None, ["AI provider module missing - using the rule-based parser."]
    try:
        provider = providers.make_provider(settings)
    except Exception as exc:  # noqa: BLE001 - SDK/HTTP layers raise anything
        LOG.warning("provider init failed: %s: %s", type(exc).__name__, exc)
        return None, [f"AI provider could not start ({type(exc).__name__}) - rules only."]
    if provider is None:
        return None, ["No usable AI provider configured - using the rule-based parser."]
    LOG.info("AI provider ready: %s / %s", settings.provider, settings.model)
    return provider, []


def parse_file(
    path: str | Path,
    settings: Settings,
    provider: Any | None,
    action: str,
    *,
    progress: ProgressFn | None = None,
    done: int = 0,
    total: int = 1,
) -> ParseResult:
    """Read one file and parse it: rules -> (AI -> reconcile) -> (answers).

    Raises `PipelineError` only when a core module is missing; anything the AI
    layer throws is absorbed into a warning on the returned `ParseResult`.
    """
    ingest = _import("app.ingest", purpose="document readers")
    rules = _import("app.parse.rules", purpose="the rule-based parser")
    assert ingest is not None and rules is not None  # required=True guarantees it

    name = Path(path).name
    started = time.perf_counter()
    _emit(progress, done, total, f"Reading {name}")
    doc = ingest.read_any(path)

    _emit(progress, done, total, f"Parsing {name}")
    base = rules.parse(doc)
    result = base

    if provider is not None:
        ai = _import("app.parse.ai", purpose="the AI parser", required=False)
        reconcile = _import("app.parse.reconcile", purpose="the reconciler",
                            required=False)
        if ai is None or reconcile is None:
            result.warnings.append(
                "AI modules unavailable - kept the rule-based parse."
            )
        else:
            try:
                ai_result = ai.parse(
                    doc,
                    provider,
                    settings=settings,
                    progress=_stage_progress(progress, done, total, f"{name}: AI pass"),
                )
                result = reconcile.reconcile(ai_result, base, doc)
                result.warnings.extend(ai_result.warnings)
                result.ai_calls = max(result.ai_calls, ai_result.ai_calls)
            except Exception as exc:  # noqa: BLE001 - never fail because AI failed
                LOG.warning("AI pass failed for %s: %s: %s", name,
                            type(exc).__name__, exc)
                result = base
                result.warnings.append(
                    f"AI pass failed ({type(exc).__name__}: {exc}) - "
                    "used the rule-based parse instead."
                )

    if action == "answers":
        result = _derive_answers(
            result, doc, settings, provider,
            progress=progress, done=done, total=total, name=name,
        )

    # Carry the reader's own notes (missing text layer, unreadable image, ...).
    doc_warnings = list(getattr(doc, "warnings", []) or [])
    result.warnings = _dedupe(list(result.warnings) + doc_warnings)
    if result.source is None:
        result.source = doc
    if not result.elapsed_s:
        result.elapsed_s = time.perf_counter() - started

    # Some parsers leave source_path blank; the review table and the merge step
    # both need it, so fill it in from the file we actually read.
    src = str(path)
    for i, q in enumerate(result.questions):
        if not q.source_path:
            q.source_path = src
        if not q.source_index:
            q.source_index = i

    # Count the questions 1..N in document order, whatever the file printed
    # (a restart at 1, a "2." after a "5.", no number at all). The printed
    # number is kept on each question, and the preview screen can switch the
    # export back to it. Done here, once, so the AI and rules paths agree.
    if hasattr(rules, "assign_numbers"):
        try:
            rules.assign_numbers(result.questions, result.warnings)
        except Exception as exc:  # noqa: BLE001 - numbering must never lose a parse
            LOG.warning("could not renumber questions for %s: %s", name, exc)

    # Bold runs are carried over here, once, rather than by each parser: this is
    # the only point where the finished questions and their document are both in
    # hand. It is also after source_path is set, which the annotator checks.
    styles = _import("app.parse.styles", purpose="bold preservation", required=False)
    if styles is not None and hasattr(styles, "annotate_bold"):
        try:
            styles.annotate_bold(result.questions, doc)
        except Exception as exc:  # noqa: BLE001 - never lose a parse over styling
            LOG.warning("could not carry bold formatting for %s: %s", name, exc)
    return result


def _derive_answers(
    result: ParseResult,
    doc: Any,
    settings: Settings,
    provider: Any | None,
    *,
    progress: ProgressFn | None,
    done: int,
    total: int,
    name: str,
) -> ParseResult:
    """Run `answers.detect_answers` for the "answers" action, tolerantly."""
    if provider is None:
        result.warnings.append(
            "Answer detection needs an AI key - kept only the answers the "
            "document stated itself."
        )
        return result
    answers = _import("app.parse.answers", purpose="answer detection", required=False)
    if answers is None or not hasattr(answers, "detect_answers"):
        result.warnings.append("Answer detection module missing - skipped.")
        return result
    try:
        result.questions = answers.detect_answers(
            result.questions,
            doc,
            provider,
            settings=settings,
            progress=_stage_progress(progress, done, total, f"{name}: answers"),
        )
    except Exception as exc:  # noqa: BLE001 - a blank key beats a failed run
        LOG.warning("answer detection failed for %s: %s: %s", name,
                    type(exc).__name__, exc)
        result.warnings.append(
            f"Answer detection failed ({type(exc).__name__}: {exc}) - "
            "answers left as found in the source."
        )
    return result


def run_files(
    paths: Sequence[str | Path],
    action: str,
    settings: Settings,
    *,
    progress: ProgressFn | None = None,
) -> list[FileOutcome]:
    """Parse every file, one `FileOutcome` each. One bad file never stops the rest."""
    provider, provider_warnings = make_provider(settings)
    total = max(1, len(paths))
    outcomes: list[FileOutcome] = []

    for i, path in enumerate(paths):
        started = time.perf_counter()
        try:
            result = parse_file(
                path, settings, provider, action,
                progress=progress, done=i, total=total,
            )
        except PipelineError:
            raise  # a core module is missing: that is not a per-file problem
        except Exception as exc:  # noqa: BLE001 - unreadable file, corrupt docx, ...
            LOG.warning("failed to process %s: %s: %s", path, type(exc).__name__, exc)
            outcomes.append(
                FileOutcome(
                    path=str(path),
                    elapsed_s=time.perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            _emit(progress, i + 1, total, f"{Path(path).name}: failed")
            continue

        result.warnings = _dedupe(provider_warnings + list(result.warnings))
        outcomes.append(
            FileOutcome(
                path=str(path),
                result=result,
                elapsed_s=result.elapsed_s or (time.perf_counter() - started),
            )
        )
        _emit(
            progress, i + 1, total,
            f"{Path(path).name}: {len(result.questions)} questions",
        )
    return outcomes


def combine(
    outcomes: Sequence[FileOutcome], action: str, settings: Settings
) -> ParseResult:
    """Fold the per-file results into the single `ParseResult` the UI shows.

    For the "merge" action this goes through `merge.merge` (dedupe + renumber);
    otherwise files are simply concatenated in input order.
    """
    good = [o.result for o in outcomes if o.ok and o.result is not None]
    warnings: list[str] = []
    for o in outcomes:
        if o.error:
            warnings.append(f"{Path(o.path).name}: {o.error}")
    for r in good:
        warnings.extend(r.warnings)

    if action == "merge" and good:
        merge = _import("app.parse.merge", purpose="the file merger", required=False)
        if merge is not None and hasattr(merge, "merge"):
            try:
                merged = merge.merge(good, renumber=True)
                merged.warnings = _dedupe(warnings + list(merged.warnings))
                if merged.source is None:
                    merged.source = good[0].source
                if not merged.elapsed_s:
                    merged.elapsed_s = sum(r.elapsed_s for r in good)
                merged.ai_calls = merged.ai_calls or sum(r.ai_calls for r in good)
                return merged
            except Exception as exc:  # noqa: BLE001 - fall back to concatenation
                LOG.warning("merge failed: %s: %s", type(exc).__name__, exc)
                warnings.append(
                    f"Merge/dedupe failed ({type(exc).__name__}: {exc}) - "
                    "files were concatenated without de-duplication."
                )
        else:
            warnings.append(
                "Merge module unavailable - files were concatenated without "
                "de-duplication."
            )

    questions: list[Question] = []
    for r in good:
        questions.extend(r.questions)

    parsers = {r.parser for r in good}
    combined = ParseResult(
        questions=questions,
        source=good[0].source if good else None,
        parser=parsers.pop() if len(parsers) == 1 else Parser.AI_RULES,
        warnings=_dedupe(warnings),
        elapsed_s=sum(r.elapsed_s for r in good),
        ai_calls=sum(r.ai_calls for r in good),
    )
    if len(good) > 1:
        combined.warnings.append(
            f"{len(questions)} questions from {len(good)} files shown together; "
            "use the Merge action to de-duplicate and renumber them."
        )
    return combined


def run_pipeline(
    paths: Sequence[str | Path],
    action: str,
    settings: Settings,
    progress: ProgressFn | None = None,
) -> ParseResult:
    """Read -> rules -> AI -> reconcile (-> answers / merge). The shared path.

    `action` is one of `ACTIONS`. Never raises for AI trouble; raises
    `PipelineError` only when a core module is genuinely absent.
    """
    if action not in ACTIONS:
        raise PipelineError(
            f"unknown action {action!r} (expected one of {', '.join(ACTIONS)})"
        )
    outcomes = run_files(paths, action, settings, progress=progress)
    result = combine(outcomes, action, settings)
    _emit(
        progress, len(paths), max(1, len(paths)),
        f"{len(result.questions)} questions, {result.n_flagged} need attention",
    )
    return result


def _dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of warning strings."""
    return list(dict.fromkeys(w for w in items if w))


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def _passthrough_source(action: str, questions: Sequence[Question], word_box: Any) -> str | None:
    """The single .docx to restyle in place rather than rebuild, or None.

    "Extract & format" on a document that is ALREADY laid out as question boxes
    is a formatting job, not a writing job: the boxes exist, and the user asked
    for their file formatted. Restyling in place is `format_docs.py`'s own mode
    of operation and keeps every row the source had -- including the ones the box
    model has no slot for, such as the trailing "Tags: #..." row.

    Only for `extract`. "Detect correct answer" has to write a key the source
    does not contain, and "merge" has many sources and one output, so both of
    those genuinely need the box writer.
    """
    if action != "extract":
        return None
    paths = {q.source_path for q in questions if q.source_path}
    if len(paths) != 1:
        return None                       # merged, or nothing to go on
    src = paths.pop()
    if not src.lower().endswith(".docx") or not os.path.isfile(src):
        return None
    return src if word_box.has_question_boxes(src) else None


def run_export(
    kind: str,
    questions: Sequence[Question],
    out_path: str | Path,
    settings: Settings,
    *,
    action: str = "",
    rebuild: bool = False,
) -> str:
    """Write `questions` as Word ("word") or PowerPoint ("pptx"). Returns the path.

    `rebuild=True` forces the box writer even for a source that already has
    question boxes: the preview screen sets it whenever the user changed
    anything (wording, an answer, the order, a layout tick), because restyling
    the source in place would silently discard every one of those changes.
    """
    key = "pptx" if kind.lower() in {"pptx", "ppt", "deck", "powerpoint"} else "word"
    target = Path(out_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(f"cannot create output folder {target.parent}: {exc}") from exc

    if not questions:
        raise PipelineError("nothing to export - no questions were found")

    if key == "word":
        word_box = _import("app.export.word_box", purpose="the Word exporter")
        assert word_box is not None
        src = None if rebuild else _passthrough_source(action, questions, word_box)
        if src is not None:
            written, ok, detail = word_box.reformat_existing(
                src, str(target), settings.word
            )
            LOG.info("restyled %s in place: verbatim %s (%s)",
                     Path(src).name, "OK" if ok else "FAILED", detail)
            return str(written or target)
        written = word_box.write_box_docx(
            list(questions), str(target), settings.word, settings.brand
        )
    else:
        deck = _import("app.export.pptx_deck", purpose="the PowerPoint exporter")
        assert deck is not None
        written = deck.build_deck(
            list(questions), str(target), settings.pptx, settings.brand
        )
    return str(written or target)


def resolve_out_dir(
    requested: str, settings: Settings, paths: Sequence[str | Path]
) -> Path:
    """--out, else settings.output_dir, else next to the first input file."""
    if requested.strip():
        return Path(requested).expanduser()
    if settings.output_dir.strip():
        return Path(settings.output_dir).expanduser()
    if paths:
        return Path(paths[0]).expanduser().resolve().parent
    return Path.cwd()


def out_path_for(out_dir: Path, stem: str, kind: str, *, tag: str | None = None) -> Path:
    """A free filename in `out_dir` -- never clobber an earlier export.

    `tag` overrides the usual "_formatted" / "_deck" suffix ("" for none).
    """
    ext = EXPORT_EXT.get(kind, ".docx")
    if tag is None:
        tag = EXPORT_TAG.get(kind, "")
    candidate = out_dir / f"{stem}{tag}{ext}"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{stem}{tag}_{n}{ext}"
        n += 1
    return candidate


# --------------------------------------------------------------------------- #
# JSON report (consumed by the verification phase)
# --------------------------------------------------------------------------- #


def _enum_value(value: Any) -> Any:
    """`Flag.NO_ANSWER` -> "no_answer"; passes plain values straight through."""
    return getattr(value, "value", value)


def _question_report(q: Question, doc: Any, verifier: Any | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": q.number,
        "stem": q.stem,
        "stem_lines": list(q.stem_lines),
        "n_options": len(q.options),
        "options": [
            {"label": o.label, "text": o.text, "lines": list(o.source_lines)}
            for o in q.options
        ],
        "answer": {
            "label": q.answer.label,
            "text": q.answer.text,
            "origin": _enum_value(q.answer.origin),
            "confidence": round(float(q.answer.confidence), 3),
            "rationale": q.answer.rationale,
            "source_lines": list(q.answer.source_lines),
        },
        "explanation_lines": list(q.explanation_lines),
        "parser": _enum_value(q.parser),
        "confidence": round(float(q.confidence), 3),
        "flags": [_enum_value(f) for f in q.flags],
        "verbatim_ok": bool(q.verbatim_ok),
        "needs_attention": bool(q.needs_attention),
        "n_images": len(q.images),
        "n_tables": len(q.tables),
        "source_path": q.source_path,
        "notes": q.notes,
    }
    # An independent re-derivation from the line indices: the report should not
    # simply believe the parser's own verbatim_ok flag.
    if verifier is not None and doc is not None:
        try:
            ok, detail = verifier(q, doc)
            data["verbatim_proof"] = {"ok": bool(ok), "detail": detail}
        except Exception as exc:  # noqa: BLE001 - reporting must not fail the run
            data["verbatim_proof"] = {"ok": None, "detail": f"check failed: {exc}"}
    return data


def build_report(
    outcomes: Sequence[FileOutcome],
    combined: ParseResult,
    action: str,
    settings: Settings,
    exports: Sequence[str],
) -> dict[str, Any]:
    """A machine-readable dump of the run. Never contains the API key."""
    reconcile = _import("app.parse.reconcile", purpose="the reconciler", required=False)
    verifier = getattr(reconcile, "verify_question", None) if reconcile else None

    files: list[dict[str, Any]] = []
    for o in outcomes:
        doc = o.result.source if o.result is not None else None
        files.append(
            {
                "path": o.path,
                "name": Path(o.path).name,
                "ok": o.ok,
                "error": o.error,
                "elapsed_s": round(o.elapsed_s, 3),
                "n_questions": len(o.questions),
                "n_flagged": o.n_flagged,
                "n_lines": len(doc.lines) if doc is not None else 0,
                "n_images": len(doc.images) if doc is not None else 0,
                "n_tables": len(doc.tables) if doc is not None else 0,
                "parser": _enum_value(o.result.parser) if o.result else "",
                "ai_calls": o.result.ai_calls if o.result else 0,
                "warnings": list(o.result.warnings) if o.result else [],
                "exports": list(o.exports),
                "questions": [
                    _question_report(q, doc, verifier) for q in o.questions
                ],
            }
        )

    verbatim_bad = sum(1 for q in combined.questions if not q.verbatim_ok)
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "action": action,
        "ai": {
            "enabled": settings.use_ai,
            "provider": settings.provider,
            "model": settings.model,
            "key_present": bool(resolve_api_key(settings)),  # never the key itself
        },
        "totals": {
            "files": len(outcomes),
            "files_failed": sum(1 for o in outcomes if not o.ok),
            "questions": len(combined.questions),
            "needs_attention": combined.n_flagged,
            "verbatim_mismatch": verbatim_bad,
            "elapsed_s": round(sum(o.elapsed_s for o in outcomes), 3),
            "ai_calls": combined.ai_calls,
        },
        "warnings": list(combined.warnings),
        "exports": list(exports),
        "files": files,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _expand_inputs(raw_paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Turn CLI arguments into real files. Returns (files, problems).

    Globs are expanded here on purpose: PowerShell does not do it for us, and
    a folder argument is treated as "every supported file inside it".
    """
    exts = supported_extensions()
    files: list[str] = []
    problems: list[str] = []
    for raw in raw_paths:
        text = str(raw).strip().strip('"')
        if not text:
            continue
        p = Path(text).expanduser()
        if any(ch in text for ch in "*?["):
            parent = p.parent if str(p.parent) else Path(".")
            matches = sorted(m for m in parent.glob(p.name) if m.is_file())
            if not matches:
                problems.append(f"no files match {text}")
            files.extend(str(m) for m in matches)
            continue
        try:
            if p.is_file():
                files.append(str(p))
                continue
            if p.is_dir():
                found = sorted(
                    q for q in p.iterdir()
                    if q.is_file() and q.suffix.lower() in exts
                )
                if not found:
                    problems.append(f"no supported files in {text}")
                files.extend(str(q) for q in found)
                continue
        except OSError as exc:
            problems.append(f"cannot access {text} ({exc})")
            continue
        problems.append(f"not found: {text}")

    unique = list(dict.fromkeys(files))
    for f in unique:
        if Path(f).suffix.lower() not in exts:
            problems.append(f"unsupported file type: {f}")
    return [f for f in unique if Path(f).suffix.lower() in exts], problems


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="main.py",
        description=f"{APP_NAME} {APP_VERSION} - MCQ extraction and formatting.",
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --cli bank.docx --action extract --out out\n"
            "  python main.py --cli a.docx b.docx --action merge --json report.json\n"
            "  python main.py --cli notes.docx --action pptx --no-ai\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "files", nargs="*",
        help="input .docx/.pdf/.txt/.pptx files (or folders / globs)",
    )
    ap.add_argument(
        "--cli", action="store_true",
        help="run headless: no window, print a summary, write the exports",
    )
    ap.add_argument(
        "--action", choices=ACTIONS, default="extract",
        help="what to do with the files (default: extract)",
    )
    ap.add_argument(
        "--out", default="", metavar="DIR",
        help="output folder (default: the settings folder, else beside the input)",
    )
    ap.add_argument(
        "--no-ai", action="store_true",
        help="rule-based parsing only, even when a key is configured",
    )
    ap.add_argument(
        "--json", dest="json_report", default="", metavar="REPORT.json",
        help="write a machine-readable report of the run",
    )
    ap.add_argument(
        "--no-export", action="store_true",
        help="parse and report only; write no .docx/.pptx",
    )
    # Deck contents. The GUI asks for these in a pre-flight panel; these flags
    # are the same choices for headless runs. Every one is a --x / --no-x pair
    # defaulting to None, so an unspecified flag keeps the saved preference AND
    # anything switched off in the panel can be switched back on from here.
    deck = ap.add_argument_group("PowerPoint contents")
    deck.add_argument(
        "--q-prefix", choices=("number", "q", "none"), default=None,
        help='label before the stem: "Q1." (number), "Q." (q), or nothing (none)',
    )
    deck.add_argument(
        "--question-text", action=argparse.BooleanOptionalAction, default=None,
        help="include the question text (--no-question-text to leave it off)",
    )
    deck.add_argument(
        "--options", action=argparse.BooleanOptionalAction, default=None,
        help="include the A/B/C/D list (--no-options to leave it off)",
    )
    deck.add_argument(
        "--after-option-images", action=argparse.BooleanOptionalAction, default=None,
        help="include images placed after the options (--no-after-option-images "
             "to leave them off; question and option images are always kept)",
    )
    deck.add_argument(
        "--answers", action=argparse.BooleanOptionalAction, default=None,
        help="include the correct answer (--no-answers to omit it)",
    )
    deck.add_argument(
        "--answer-next-slide", action=argparse.BooleanOptionalAction, default=None,
        help="answer on its own slide (--no-answer-next-slide for an in-slide banner)",
    )
    deck.add_argument(
        "--title-slide", action=argparse.BooleanOptionalAction, default=None,
        help="include the title slide (--no-title-slide to skip it)",
    )
    deck.add_argument(
        "--thankyou-slide", action=argparse.BooleanOptionalAction, default=None,
        help="include the closing slide (--no-thankyou-slide to skip it)",
    )
    ap.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v for progress and info logs, -vv for debug",
    )
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return ap


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # The AI SDKs are chatty at DEBUG and can echo request bodies; keep them quiet.
    for noisy in ("httpx", "httpcore", "urllib3", "google", "google_genai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def _cli_exports(
    outcomes: Sequence[FileOutcome],
    combined: ParseResult,
    action: str,
    settings: Settings,
    out_dir: Path,
) -> list[str]:
    """Write the exports for a CLI run. A failed export never aborts the batch."""
    kind = ACTION_EXPORT.get(action, "word")
    written: list[str] = []

    if action == "merge":
        if not combined.questions:
            print("  (nothing to export: no questions found)")
            return written
        stem = Path(outcomes[0].path).stem if outcomes else "questions"
        target = out_path_for(out_dir, f"{stem}_merged", kind)
        try:
            written.append(run_export(kind, combined.questions, target, settings))
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            LOG.warning("export failed: %s: %s", type(exc).__name__, exc)
            print(f"  export failed: {exc}", file=sys.stderr)
        return written

    for o in outcomes:
        if not o.ok or not o.questions:
            continue
        target = out_path_for(out_dir, Path(o.path).stem, kind)
        # A box document is restyled in place only when nothing changes --
        # and renumbering a file whose own numbering restarted is a change.
        rebuild = bool(settings.word.renumber) and any(
            q.source_number != q.number for q in o.questions
        )
        try:
            path = run_export(kind, o.questions, target, settings, action=action,
                              rebuild=rebuild)
        except Exception as exc:  # noqa: BLE001 - one bad file, not the whole run
            LOG.warning("export failed for %s: %s: %s", o.path,
                        type(exc).__name__, exc)
            print(f"  export failed for {Path(o.path).name}: {exc}", file=sys.stderr)
            continue
        o.exports.append(path)
        written.append(path)
    return written


def _print_summary(outcomes: Sequence[FileOutcome], combined: ParseResult) -> None:
    width = max((len(Path(o.path).name) for o in outcomes), default=10)
    width = min(max(width, 12), 48)
    print("")
    for o in outcomes:
        name = Path(o.path).name
        if not o.ok:
            print(f"{name:<{width}}  FAILED  {o.error}")
            continue
        print(
            f"{name:<{width}}  {len(o.questions):>4} questions  "
            f"{o.n_flagged:>4} need attention  {o.elapsed_s:6.2f}s"
        )
        for w in (o.result.warnings if o.result else [])[:6]:
            print(f"{'':<{width}}    ! {w}")
        extra = max(0, len(o.result.warnings if o.result else []) - 6)
        if extra:
            print(f"{'':<{width}}    ! (+{extra} more warnings)")
        for path in o.exports:
            print(f"{'':<{width}}    -> {path}")

    total_q = len(combined.questions)
    total_s = sum(o.elapsed_s for o in outcomes)
    print("")
    print(
        f"Total: {total_q} questions, {combined.n_flagged} need attention, "
        f"{sum(1 for o in outcomes if not o.ok)} file(s) failed, {total_s:.2f}s"
    )


#: CLI flag -> PptxExportConfig field. Each flag is a --x / --no-x pair whose
#: default is None, so an unspecified flag leaves the saved preference alone and
#: an explicit --x can re-enable something the GUI panel had switched off.
_DECK_FLAGS: tuple[tuple[str, str], ...] = (
    ("question_text", "include_question_text"),
    ("options", "include_options"),
    ("after_option_images", "include_images_after_options"),
    ("answers", "include_answers"),
    ("answer_next_slide", "answer_on_separate_slide"),
    ("title_slide", "title_slide"),
    ("thankyou_slide", "thankyou_slide"),
)


def _apply_deck_flags(settings: Settings, args: argparse.Namespace) -> None:
    """Fold the --q-prefix and --x/--no-x deck flags into `settings.pptx`."""
    cfg = settings.pptx
    if getattr(args, "q_prefix", None):
        cfg.question_prefix = args.q_prefix
    for arg_name, field_name in _DECK_FLAGS:
        value = getattr(args, arg_name, None)
        if value is not None:
            setattr(cfg, field_name, bool(value))


def run_cli(args: argparse.Namespace) -> int:
    """The headless path. Returns a process exit code."""
    settings = load_settings()
    if args.no_ai:
        settings.use_ai = False
    _apply_deck_flags(settings, args)

    paths, problems = _expand_inputs(args.files)
    for p in problems:
        print(f"warning: {p}", file=sys.stderr)
    if not paths:
        exts = ", ".join(sorted(supported_extensions()))
        print(
            f"error: no readable input files. Pass at least one file ({exts}).",
            file=sys.stderr,
        )
        return 2

    out_dir = resolve_out_dir(args.out, settings, paths)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot use output folder {out_dir}: {exc}", file=sys.stderr)
        return 2

    ai_state = "off"
    if settings.ai_enabled:
        ai_state = (
            f"{settings.provider}/{settings.model}"
            if resolve_api_key(settings) else "no key -> rules only"
        )
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"action : {args.action} - {ACTION_LABELS.get(args.action, args.action)}")
    print(f"files  : {len(paths)}")
    print(f"ai     : {ai_state}")
    print(f"output : {out_dir}")

    progress: ProgressFn | None = None
    if args.verbose:
        def progress(done: int, total: int, message: str = "") -> None:  # noqa: E306
            print(f"[{done}/{total}] {message}", file=sys.stderr)

    try:
        outcomes = run_files(paths, args.action, settings, progress=progress)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    combined = combine(outcomes, args.action, settings)

    exports: list[str] = []
    if not args.no_export:
        exports = _cli_exports(outcomes, combined, args.action, settings, out_dir)

    _print_summary(outcomes, combined)
    if args.action == "merge" and exports:
        for path in exports:
            print(f"  -> {path}")
    for w in combined.warnings[:10]:
        print(f"note: {w}")

    if args.json_report:
        report = build_report(outcomes, combined, args.action, settings, exports)
        target = Path(args.json_report).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"report: {target}")
        except (OSError, TypeError, ValueError) as exc:
            print(f"warning: could not write {target}: {exc}", file=sys.stderr)

    return 1 if any(not o.ok for o in outcomes) else 0


# --------------------------------------------------------------------------- #
# GUI helpers (tolerant glue -- the screens are owned by other modules)
# --------------------------------------------------------------------------- #


def _make_screen(module_name: str, class_name: str, settings: Settings) -> Any:
    """Instantiate a screen, accepting either `Cls(settings)` or `Cls()`.

    The contract fixes the class names and their signals but not their
    constructor arity, so try the richer form first and fall back.
    """
    module = _import(module_name, purpose=f"the {class_name} screen")
    assert module is not None
    cls = getattr(module, class_name, None)
    if cls is None:
        raise PipelineError(f"{module_name} does not define {class_name}")
    first_error: Exception | None = None
    for call_args in ((settings,), ()):
        try:
            return cls(*call_args)
        except TypeError as exc:
            first_error = first_error or exc
            LOG.debug("%s(%d args) rejected: %s", class_name, len(call_args), exc)
    raise PipelineError(f"could not create {class_name}: {first_error}")


def _connect(obj: Any, names: Sequence[str], slot: Callable[..., Any]) -> bool:
    """Connect the first signal in `names` that exists on `obj`."""
    for name in names:
        signal = getattr(obj, name, None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(slot)
            return True
    return False


def _try_call(obj: Any, names: Sequence[str], *call_args: Any) -> bool:
    """Call the first method in `names` that exists and accepts `call_args`."""
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                fn(*call_args)
                return True
            except TypeError as exc:
                LOG.debug("%s.%s rejected the arguments: %s",
                          type(obj).__name__, name, exc)
    return False


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #


class MainWindow(QMainWindow):
    """Login -> Dashboard -> (Drop -> Actions -> Review) or Bulk entry.

    This window owns no document logic: every job goes through
    `app.ui.worker.run_job` so the GUI thread stays free.

    The chrome is a left-hand nav rail (`app.ui.sidebar`) rather than a toolbar.
    The lock screen is index 0 and the rail is hidden while it is showing, so
    Settings and Back cannot be used to step around the password -- the same
    property the hidden toolbar used to give, and the reason the rail is hidden
    rather than merely disabled.
    """

    LOGIN, DASH, HOME, MENU, REVIEW, BULK, WORDPREV, HISTORY = 0, 1, 2, 3, 4, 5, 6, 7

    #: Which rail entry is lit on each screen. Everything reached from the home
    #: page keeps "Home" lit, because that is where Back and the rail return to;
    #: the lock screen lights nothing, since the rail is not on screen.
    _RAIL_FOR_SCREEN: dict[int, str] = {
        LOGIN: "",
        DASH: "home",
        HOME: "home",
        MENU: "home",
        REVIEW: "home",
        BULK: "home",
        WORDPREV: "home",
        HISTORY: "history",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.paths: list[str] = []
        self.result: ParseResult | None = None
        self.action = "extract"
        self._busy_flag = False
        self._thread: Any = None  # keep a reference or the QThread is collected
        self._history: list[int] = []

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1280, 860)

        self._qsettings = QSettings("Cerebellum Academy", APP_NAME)
        self._restore_geometry()

        central = QWidget(self)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.sidebar = self._build_sidebar(central)
        shell.addWidget(self.sidebar)

        content = QWidget(central)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.banner = self._build_banner()
        layout.addWidget(self.banner)
        self.stack = QStackedWidget(content)
        layout.addWidget(self.stack, 1)
        shell.addWidget(content, 1)
        self.setCentralWidget(central)

        self._build_actions()
        self._build_statusbar()

        # Order must match the LOGIN/DASH/HOME/MENU/REVIEW/BULK indices.
        self.login = _make_screen("app.ui.login", "LoginScreen", settings)
        self.dash = _make_screen("app.ui.dashboard", "DashboardScreen", settings)
        self.home = _make_screen("app.ui.home", "HomeScreen", settings)
        self.menu = _make_screen("app.ui.actions", "ActionMenu", settings)
        self.review = _make_screen("app.ui.review", "ReviewTable", settings)
        self.bulk = _make_screen("app.ui.bulk_entry", "BulkEntryScreen", settings)
        self.wordprev = _make_screen("app.ui.word_preview", "WordPreviewScreen", settings)
        self.history = _make_screen("app.ui.history", "HistoryScreen", settings)
        for widget in (self.login, self.dash, self.home, self.menu,
                       self.review, self.bulk, self.wordprev, self.history):
            self.stack.addWidget(widget)

        self._wire_signals()
        self.refresh_banner()
        self.lock()

    # -- construction ------------------------------------------------------- #

    def _build_banner(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("aiBanner")
        # Colours come from the theme (`#aiBanner` in theme.py) so the strip is
        # the same amber as every other warning in the app. A plain QWidget does
        # not paint a style sheet background unless it is told to.
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        self.banner_label = QLabel(NO_KEY_BANNER, bar)
        self.banner_label.setWordWrap(True)
        row.addWidget(self.banner_label, 1)
        open_settings = QPushButton("Open Settings", bar)
        open_settings.clicked.connect(self.open_settings)
        row.addWidget(open_settings, 0)
        bar.setVisible(False)
        return bar

    def _build_sidebar(self, parent: QWidget) -> QWidget:
        """The nav rail down the left edge: Home, History, Settings, Help."""
        module = _import("app.ui.sidebar", purpose="the navigation rail")
        assert module is not None
        rail = module.Sidebar(APP_NAME, APP_VERSION, parent)
        rail.navigate.connect(self.on_navigate)
        return rail

    def _build_actions(self) -> None:
        """Keyboard shortcuts. The rail carries the visible versions of these.

        They are added to the window rather than to a toolbar, so Alt+Left,
        Ctrl+L and Ctrl+, keep working now that there is no toolbar to own them.
        `act_back` has no rail entry: every screen you can go back from has its
        own Back button, and the rail's Home is the other way out.
        """
        self.act_back = QAction("Back", self)
        self.act_back.setShortcut("Alt+Left")
        self.act_back.setEnabled(False)
        self.act_back.triggered.connect(self.go_back)
        self.act_home = QAction("Home", self)
        self.act_home.triggered.connect(self.go_home)
        self.act_lock = QAction("Lock", self)
        self.act_lock.setShortcut("Ctrl+L")
        self.act_lock.triggered.connect(self.lock)
        self.act_settings = QAction("Settings", self)
        self.act_settings.setShortcut("Ctrl+,")
        self.act_settings.triggered.connect(self.open_settings)
        for action in (self.act_back, self.act_home, self.act_lock,
                       self.act_settings):
            self.addAction(action)

    def _build_statusbar(self) -> None:
        self.progress = QProgressBar(self)
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Drop your question files to start.")

    def _wire_signals(self) -> None:
        if not _connect(self.login, ("unlocked",), self.on_unlocked):
            LOG.warning("LoginScreen has no unlocked signal - cannot leave the lock screen")
        if not _connect(self.dash, ("featureChosen",), self.on_feature_chosen):
            LOG.warning("DashboardScreen has no featureChosen signal")
        _connect(self.bulk, ("backRequested", "back"), self.go_dashboard)
        if not _connect(self.home, ("filesReady",), self.on_files_ready):
            LOG.warning("HomeScreen has no filesReady signal - cannot start a job")
        if not _connect(self.menu, ("actionChosen",), self.on_action_chosen):
            LOG.warning("ActionMenu has no actionChosen signal")
        if not _connect(self.review, ("exportRequested",), self.on_export_requested):
            LOG.warning("ReviewTable has no exportRequested signal")
        # Optional conveniences some screens offer.
        _connect(self.home, ("settingsRequested", "settingsClicked", "openSettings"),
                 self.open_settings)
        _connect(self.menu, ("backRequested", "back"), self.go_back)
        _connect(self.review, ("backRequested", "back"), self.go_back)
        if not _connect(self.wordprev, ("exportRequested",), self.on_word_preview_export):
            LOG.warning("WordPreviewScreen has no exportRequested signal")
        _connect(self.wordprev, ("backRequested", "back"), self.go_back)
        _connect(self.wordprev, ("reviewRequested",), self.show_review_table)
        _connect(self.history, ("filesChosen",), self.on_history_files)
        _connect(self.history, ("backRequested", "back"), self.go_back)
        _connect(self.history, ("cleared",), self.on_history_cleared)

    def _restore_geometry(self) -> None:
        try:
            geometry = self._qsettings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
            state = self._qsettings.value("windowState")
            if state:
                self.restoreState(state)
        except (TypeError, ValueError) as exc:  # settings written by another version
            LOG.debug("could not restore geometry: %s", exc)

    # -- navigation --------------------------------------------------------- #

    def show_screen(self, index: int) -> None:
        current = self.stack.currentIndex()
        if current != index:
            self._history.append(current)
        self.stack.setCurrentIndex(index)
        self.act_back.setEnabled(bool(self._history))
        self._sync_rail(index)

    def _sync_rail(self, index: int) -> None:
        """Light the rail entry that matches the screen now showing."""
        _try_call(self.sidebar, ("set_active",), self._RAIL_FOR_SCREEN.get(index, ""))

    def on_navigate(self, key: Any) -> None:
        """A nav rail entry was clicked."""
        which = str(key or "")
        if which == "home":
            self.go_home()
            return
        if which == "history":
            _try_call(self.history, ("refresh",))
            self.show_screen(self.HISTORY)
            self.statusBar().showMessage("Files you have worked on recently.")
            return
        if which == "settings":
            self.open_settings()
            return
        if which == "help":
            self.open_help()
            return
        LOG.warning("unknown rail destination %r", which)

    def show_action_menu(self) -> None:
        """Open the full action menu, keeping whatever files are queued.

        Reached from the drop screen (files queued, no action chosen yet), not
        from the toolbar.
        """
        if self.paths:
            _try_call(self.menu, ("set_files", "setFiles", "update_files", "set_paths"),
                      list(self.paths))
        self.show_screen(self.MENU)

    def go_back(self) -> None:
        if not self._history:
            return
        self.stack.setCurrentIndex(self._history.pop())
        self.act_back.setEnabled(bool(self._history))
        self._sync_rail(self.stack.currentIndex())

    def go_home(self) -> None:
        self._history.clear()
        self.stack.setCurrentIndex(self.DASH)
        self.act_back.setEnabled(False)
        _try_call(self.dash, ("refresh",))
        self._sync_rail(self.DASH)

    #: "Home" and "Dashboard" are the same destination; both names are used by
    #: the toolbar and by the screens' Back buttons.
    go_dashboard = go_home

    # -- lock --------------------------------------------------------------- #

    def lock(self) -> None:
        """Show the lock screen and hide everything that could step around it.

        Called on start-up and by the dashboard's Lock button. The work in
        progress is dropped deliberately: leaving a parsed question bank on
        screen behind a lock screen would defeat the point.
        """
        self.paths = []
        self.result = None
        self.action = "extract"
        self._history.clear()
        _try_call(self.login, ("refresh",))
        self.stack.setCurrentIndex(self.LOGIN)
        self._set_chrome_visible(False)

    def on_unlocked(self) -> None:
        """Password accepted: reveal the app and land on the dashboard."""
        self._set_chrome_visible(True)
        self.refresh_banner()
        self.go_dashboard()
        self.statusBar().showMessage("Unlocked. Choose what you would like to do.")

    def _set_chrome_visible(self, visible: bool) -> None:
        """Nav rail, status bar and AI banner, hidden while locked."""
        self.sidebar.setVisible(visible)
        self.statusBar().setVisible(visible)
        if not visible:
            self.banner.setVisible(False)
        # The shortcuts used to be switched off for free by hiding the toolbar
        # that owned them. They hang off the window now, so the lock screen has
        # to disable them itself -- otherwise Ctrl+, opens Settings from behind
        # the password.
        for action in (self.act_home, self.act_lock, self.act_settings):
            action.setEnabled(visible)
        self.act_back.setEnabled(False)

    # -- dashboard ---------------------------------------------------------- #

    def on_feature_chosen(self, key: Any) -> None:
        """A dashboard tile was clicked."""
        which = str(key or "")
        if which == "lock":
            self.lock()
            return
        if which == "settings":
            self.open_settings()
            return
        if which == "bulk":
            self.show_screen(self.BULK)
            self.statusBar().showMessage(
                "Paste your values, then run a dry run before the real one."
            )
            return
        if which in ACTIONS:
            # The action is already decided, so the drop screen leads straight
            # into it rather than asking again on the action menu.
            self.action = which
            _try_call(self.home, ("set_action", "setAction"), which)
            self.show_screen(self.HOME)
            self.statusBar().showMessage(
                f"{ACTION_LABELS.get(which, which)} — drop your files."
            )
            return
        LOG.warning("unknown dashboard feature %r", which)

    # -- history ------------------------------------------------------------ #

    def on_history_files(self, paths: Any) -> None:
        """A History row was picked.

        It lands in the drop screen rather than starting a job: the file is only
        half the decision, and re-running something from a list of names is the
        kind of convenience that quietly overwrites yesterday's export.
        """
        wanted = [str(p) for p in (paths or []) if str(p).strip()]
        if not wanted:
            return
        if not _try_call(self.home, ("add_files", "set_files", "setFiles"), wanted):
            LOG.warning("HomeScreen would not take the files from History")
            return
        self.show_screen(self.HOME)
        self.statusBar().showMessage(
            f"Loaded {Path(wanted[0]).name} — choose what to do with it."
        )

    def on_history_cleared(self) -> None:
        save_settings(self.settings)
        self.statusBar().showMessage("History cleared.", 4000)

    # -- banner / settings -------------------------------------------------- #

    def refresh_banner(self) -> None:
        """Amber, never modal: the app is fully usable without a key."""
        if not self.settings.ai_enabled:
            self.banner_label.setText(
                "AI is switched off. Questions are parsed by rules only."
            )
            self.banner.setVisible(True)
            return
        if not resolve_api_key(self.settings):
            self.banner_label.setText(NO_KEY_BANNER)
            self.banner.setVisible(True)
            return
        self.banner.setVisible(False)

    def open_settings(self) -> None:
        module = _import("app.ui.settings_dialog", purpose="the settings dialog",
                         required=False)
        cls = getattr(module, "SettingsDialog", None) if module else None
        if cls is None:
            QMessageBox.information(
                self, "Settings",
                "The settings dialog is not available in this build.\n"
                "Set GEMINI_API_KEY in your environment to enable AI.",
            )
            return
        dialog = None
        for call_args in ((self.settings, self), (self.settings,), ()):
            try:
                dialog = cls(*call_args)
                break
            except TypeError as exc:
                LOG.debug("SettingsDialog(%d args) rejected: %s", len(call_args), exc)
        if dialog is None:
            QMessageBox.warning(self, "Settings", "Could not open the settings dialog.")
            return
        if dialog.exec():
            updated = getattr(dialog, "settings", None)
            if isinstance(updated, Settings):
                self.settings = updated
            save_settings(self.settings)
            self.refresh_banner()
            self.statusBar().showMessage("Settings saved.", 4000)

    def open_help(self) -> None:
        """The rail's Help entry. Never fatal: a missing dialog is a message."""
        module = _import("app.ui.help_dialog", purpose="the help dialog",
                         required=False)
        cls = getattr(module, "HelpDialog", None) if module else None
        if cls is None:
            QMessageBox.information(
                self, "Help",
                "The help screen is not available in this build.\n"
                "See README.md beside the app for the full guide.",
            )
            return
        try:
            dialog = cls(self)
        except TypeError:
            dialog = cls()
        dialog.exec()

    def _current_brand(self) -> BrandConfig:
        """Subject / faculty / date typed on the home screen win over the file."""
        for name in ("brand", "brand_config", "brandConfig", "get_brand"):
            value = getattr(self.home, name, None)
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if isinstance(value, BrandConfig):
                return value
        return self.settings.brand

    # -- jobs --------------------------------------------------------------- #

    def _start_job(
        self,
        fn: Callable[..., Any],
        message: str,
        on_done: Callable[[Any], None],
    ) -> None:
        if self._busy_flag:
            self.statusBar().showMessage("Still working - please wait.", 3000)
            return
        worker = _import("app.ui.worker", purpose="the background job runner",
                         required=False)
        run_job = getattr(worker, "run_job", None) if worker else None
        self._set_busy(True, message)

        def done(payload: Any) -> None:
            self._set_busy(False, "")
            if isinstance(payload, _JobFailure):
                self._job_error(payload.message)
                return
            on_done(payload)

        if run_job is None:
            # Last resort: without the worker the window would be useless, so run
            # inline. It blocks, but it still produces the user's output.
            LOG.warning("worker.run_job missing - running the job on the GUI thread")
            done(fn())
            return
        self._thread = run_job(
            fn,
            on_progress=self.on_progress,
            on_done=done,
            on_error=lambda msg: (self._set_busy(False, ""), self._job_error(str(msg))),
        )

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy_flag = busy
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)  # indeterminate until the first update
            self.statusBar().showMessage(message)
        self.act_settings.setEnabled(not busy)

    def on_progress(self, done: int = 0, total: int = 0, message: str = "") -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if message:
            self.statusBar().showMessage(message)

    def _job_error(self, message: str) -> None:
        LOG.warning("job failed: %s", message)
        self.statusBar().showMessage("The job did not finish.", 8000)
        QMessageBox.warning(self, APP_NAME, message or "The job did not finish.")

    # -- signal handlers ---------------------------------------------------- #

    def on_files_ready(self, paths: Any) -> None:
        self.paths = [str(p) for p in (paths or []) if str(p).strip()]
        if not self.paths:
            self.statusBar().showMessage("No readable files were dropped.", 5000)
            return
        self.settings.remember_files(self.paths)
        _try_call(self.menu, ("set_files", "setFiles", "update_files", "set_paths"),
                  list(self.paths))
        # The dashboard already chose the action, so run it rather than asking a
        # second time. Back (or Home) returns to the tiles to pick another one.
        if self.action in ACTIONS:
            self.on_action_chosen(self.action)
            return
        self.show_screen(self.MENU)
        self.statusBar().showMessage(
            f"{len(self.paths)} file(s) ready - choose what to do."
        )

    def on_action_chosen(self, action: Any) -> None:
        self.action = str(action or "extract")
        if not self.paths:
            self.statusBar().showMessage("Drop some files first.", 4000)
            self.go_home()
            return
        # Ask what goes in the deck before any work starts, so a cancel here
        # costs nothing and the choices are already in place when it is written.
        if self.action == "pptx" and not self.ask_pptx_contents():
            return
        paths = list(self.paths)
        act = self.action
        settings = self.settings

        def job(progress: ProgressFn | None = None) -> Any:
            try:
                return run_pipeline(paths, act, settings, progress)
            except Exception as exc:  # noqa: BLE001 - hand it back, never crash
                LOG.exception("pipeline failed")
                return _JobFailure(f"{type(exc).__name__}: {exc}")

        self._start_job(job, f"{ACTION_LABELS.get(act, act)}...", self.on_pipeline_done)

    def on_pipeline_done(self, result: Any) -> None:
        if not isinstance(result, ParseResult):
            self._job_error(f"Unexpected result: {type(result).__name__}")
            return
        self.result = result
        if not _try_call(
            self.review,
            ("set_result", "setResult", "load_result", "load", "show_result"),
            result,
        ):
            _try_call(
                self.review,
                ("set_questions", "setQuestions", "set_data", "populate"),
                result.questions,
            )
            _try_call(self.review, ("set_source", "setSource"), result.source)
        # The preview holds the same Question objects as the table, so an edit
        # on either screen is visible on both.
        _try_call(self.wordprev, ("set_result",), result, self.settings.word)
        # Word-bound actions land on the document preview -- the place to fix
        # wording, drop a question or pick the layout before the file is
        # written. The deck action keeps the table (its own pre-flight panel
        # already ran) and the table stays one click away from the preview.
        if ACTION_EXPORT.get(self.action, "word") == "word" and result.questions:
            self.show_screen(self.WORDPREV)
        else:
            self.show_screen(self.REVIEW)
        self.statusBar().showMessage(
            f"{len(result.questions)} questions - {result.n_flagged} need attention "
            f"({result.elapsed_s:.1f}s)"
        )
        if not result.questions:
            # `combine` records each file that could not be opened as
            # "<name>: <error>". That is a read failure, not an empty question
            # bank, and the Gemini advice below would not help with it.
            names = tuple(f"{Path(p).name}: " for p in self.paths)
            failed = [w for w in result.warnings if w.startswith(names)]
            if failed:
                QMessageBox.warning(
                    self, APP_NAME,
                    "These files could not be opened, so no questions were read:"
                    "\n\n" + "\n\n".join(failed[:5]),
                )
                return
            QMessageBox.information(
                self, APP_NAME,
                "No multiple-choice questions were found in these files.\n\n"
                "If this file really is a question bank, add a free Gemini key in "
                "Settings - the AI pass handles layouts the rules miss.",
            )

    def ask_pptx_contents(self) -> bool:
        """Show the pre-flight contents panel. False when the user cancelled.

        Deck contents are a per-deck decision, so this opens every time a
        PowerPoint is about to be written rather than living only in Settings.
        The chosen values are saved, so they come back as the defaults next time.
        """
        try:
            from app.ui.pptx_options import PptxOptionsDialog
        except ImportError:
            LOG.warning("pptx options panel unavailable; using saved settings")
            return True
        dialog = PptxOptionsDialog(self.settings.pptx, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("PowerPoint cancelled.", 4000)
            return False
        self.settings.pptx = dialog.config()
        try:
            save_settings(self.settings)
        except OSError as exc:  # a failed save must not block the export
            LOG.warning("could not save deck settings: %s", exc)
        return True

    def show_review_table(self) -> None:
        """The classic table over the same questions (from the preview)."""
        self.show_screen(self.REVIEW)

    def show_word_preview(self) -> None:
        """The document preview over the current questions (from the table)."""
        if self.result is None:
            self.statusBar().showMessage("Nothing to preview yet.", 4000)
            return
        # Rebuilt from the table's working set, which may have been edited.
        _try_call(self.wordprev, ("refresh",))
        self.show_screen(self.WORDPREV)

    def on_export_requested(self, kind: Any) -> None:
        which = "pptx" if str(kind).lower().startswith("p") else "word"
        if which == "word":
            # Word goes through the preview first, exactly as PowerPoint goes
            # through its contents panel: the user sees the document and can
            # change it before a file is written.
            self.show_word_preview()
            return
        questions = self._questions_for_export()
        if not questions:
            self.statusBar().showMessage("Nothing to export yet.", 4000)
            return
        if not self.ask_pptx_contents():
            return
        self._write_export(which, questions, rebuild=False)

    def on_word_preview_export(self) -> None:
        """Export Word pressed on the preview screen."""
        questions: list[Question] = []
        for name in ("export_questions",):
            fn = getattr(self.wordprev, name, None)
            if callable(fn):
                try:
                    questions = list(fn())
                except Exception as exc:  # noqa: BLE001 - fall back to the table
                    LOG.warning("preview export set unavailable: %s", exc)
        if not questions:
            questions = self._questions_for_export()
        if not questions:
            self.statusBar().showMessage("Nothing to export - every question is excluded.", 5000)
            return
        cfg_fn = getattr(self.wordprev, "config", None)
        if callable(cfg_fn):
            try:
                cfg = cfg_fn()
                if cfg is not None:
                    self.settings.word = cfg
                    save_settings(self.settings)   # the layout becomes the default
            except Exception as exc:  # noqa: BLE001 - keep the saved layout
                LOG.warning("could not read the preview's layout: %s", exc)
        rebuild = True
        needs = getattr(self.wordprev, "needs_rebuild", None)
        if callable(needs):
            try:
                rebuild = bool(needs())
            except Exception as exc:  # noqa: BLE001 - rebuilding is the safe side
                LOG.debug("needs_rebuild failed: %s", exc)
        self._write_export("word", questions, rebuild=rebuild)

    def _write_export(self, which: str, questions: list[Question], *, rebuild: bool) -> None:
        # Whatever the user typed on the home screen is the branding of record.
        self.settings.brand = self._current_brand()
        out_dir = resolve_out_dir("", self.settings, self.paths)
        stem = Path(self.paths[0]).stem if self.paths else "questions"
        # A deck is named exactly as the Subject typed in DECK DETAILS. The
        # characters Windows forbids become "-", as the deck writer does.
        subject = ""
        if which == "pptx":
            subject = re.sub(r'[\\/:*?"<>|]', "-", self.settings.brand.subject or "")
            subject = subject.strip().rstrip(". ")
        if subject:
            target = out_path_for(out_dir, subject, which, tag="")
        else:
            target = out_path_for(out_dir, stem, which)
        settings = self.settings
        act = self.action

        def job(progress: ProgressFn | None = None) -> Any:
            _emit(progress, 0, 1, f"Writing {target.name}")
            try:
                path = run_export(which, questions, target, settings, action=act,
                                  rebuild=rebuild)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("export failed")
                return _JobFailure(f"{type(exc).__name__}: {exc}")
            _emit(progress, 1, 1, f"Wrote {path}")
            return path

        self._start_job(job, f"Writing {target.name}...", self.on_export_done)

    def _questions_for_export(self) -> list[Question]:
        """Prefer the review table's edited questions over the raw parse."""
        for name in ("questions", "edited_questions", "current_questions",
                     "get_questions"):
            value = getattr(self.review, name, None)
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if isinstance(value, list) and value and isinstance(value[0], Question):
                return value
        return list(self.result.questions) if self.result else []

    def on_export_done(self, path: Any) -> None:
        target = Path(str(path))
        self.statusBar().showMessage(f"Saved {target}", 10000)
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Export finished.")
        box.setInformativeText(str(target))
        open_folder = box.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))

    # -- shutdown ----------------------------------------------------------- #

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Draw the dark theme's wallpaper behind everything.

        Painted here rather than set as a QSS ``background-image`` because Qt
        tiles a background image instead of cover-fitting it: on any window that
        is not exactly the picture's aspect ratio, the tiled version repeats the
        moon down the side of the screen. `theme.wallpaper` returns a null pixmap
        in the light theme, so this costs nothing there.
        """
        theme = _import("app.ui.theme", purpose="the theme", required=False)
        if theme is not None and hasattr(theme, "wallpaper"):
            try:
                picture = theme.wallpaper(self.width(), self.height())
            except Exception as exc:  # noqa: BLE001 - never fail a repaint
                LOG.debug("wallpaper unavailable: %s", exc)
                picture = None
            if picture is not None and not picture.isNull():
                painter = QPainter(self)
                try:
                    painter.drawPixmap(0, 0, picture)
                finally:
                    painter.end()
        super().paintEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        try:
            self._qsettings.setValue("geometry", self.saveGeometry())
            self._qsettings.setValue("windowState", self.saveState())
            save_settings(self.settings)
        except Exception as exc:  # noqa: BLE001 - never block a close
            LOG.debug("could not persist state on close: %s", exc)
        super().closeEvent(event)


def run_gui(settings: Settings, initial_files: Sequence[str] = ()) -> int:
    """Start the desktop app. Returns the Qt exit code."""
    if _QT_ERROR:
        print(
            f"error: PySide6 is not available ({_QT_ERROR}).\n"
            "Install it with: pip install -r requirements.txt\n"
            "Or run headless:  python main.py --cli FILE --action extract",
            file=sys.stderr,
        )
        return 2

    # High-DPI Windows laptops: keep the 1280x860 layout honest.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Cerebellum Academy")

    theme = _import("app.ui.theme", purpose="the theme", required=False)
    if theme is not None and hasattr(theme, "apply_theme"):
        try:
            theme.apply_theme(app, dark=str(settings.theme).lower() == "dark")
        except Exception as exc:  # noqa: BLE001 - ugly beats not starting
            LOG.warning("could not apply the theme: %s", exc)
    if theme is not None and hasattr(theme, "app_icon"):
        try:
            icon = theme.app_icon()
            if not icon.isNull():
                app.setWindowIcon(icon)
        except Exception as exc:  # noqa: BLE001 - an icon is never worth a crash
            LOG.warning("could not set the window icon: %s", exc)

    try:
        window = MainWindow(settings)
    except PipelineError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 3

    window.show()
    if initial_files:
        # Files handed in on the command line behave exactly like a drop.
        window.on_files_ready(list(initial_files))
    return int(app.exec())


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(
        list(argv) if argv is not None else sys.argv[1:]
    )
    _setup_logging(args.verbose)

    if args.cli:
        return run_cli(args)

    settings = load_settings()
    if args.no_ai:
        settings.use_ai = False  # honoured in the GUI too, for a demo without a key
    files, problems = _expand_inputs(args.files) if args.files else ([], [])
    for p in problems:
        LOG.warning("%s", p)
    return run_gui(settings, files)


if __name__ == "__main__":
    sys.exit(main())
