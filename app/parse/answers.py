"""Derive the correct option for questions whose source states no answer key.

This is the one place in the app where a model is allowed to produce *new*
information, so it is also the one place that needs a conscience:

* Text is still never rewritten. `Answer.text` is **copied** from the chosen
  `Option.text`, which is itself a verbatim slice of the source. The model
  returns a letter, a confidence and one sentence of rationale -- nothing else.
* Sending stem and option text here is correct (unlike extraction): choosing the
  right answer is a reasoning task, and reasoning needs to read the question.
* The bot refuses to guess. A stem that refers to a picture we do not have, an
  option letter that does not exist, or a confidence below `ACCEPT_FLOOR` all
  leave `AnswerOrigin.NONE` and `Flag.NO_ANSWER` in place. The user is a medical
  educator and these files become exam material: a blank key is a small nuisance,
  a wrong key is a taught mistake.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable

from app.models import (
    OPTION_LABELS,
    Answer,
    AnswerOrigin,
    Flag,
    ImageAsset,
    Question,
    SourceDocument,
    normalise_label,
)
from app.parse.providers import Provider, ProviderError

log = logging.getLogger(__name__)

#: Questions per request. Small enough that one bad reply costs little and the
#: model keeps every stem in working memory, large enough to keep the call count
#: (and free-tier rate limits) sane.
BATCH_SIZE = 10
#: Below this the answer is discarded entirely -- see the module docstring.
ACCEPT_FLOOR = 0.5
#: Accepted, but flagged for a human to confirm.
REVIEW_FLOOR = 0.7
REQUEST_TIMEOUT_S = 120.0
DEFAULT_CONCURRENCY = 4
#: Only so many pictures per request; a visual question is sent on its own, but a
#: stem can have several panels attached.
MAX_IMAGES_PER_CALL = 4

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "description": "One entry per question id supplied, in any order.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The id of the question being answered.",
                    },
                    "label": {
                        "type": "string",
                        "enum": list(OPTION_LABELS),
                        "description": (
                            "Letter of the single best option, chosen from the "
                            "letters printed for that question."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "0-1. Below 0.5 means 'do not use this answer'; the "
                            "program will leave the key blank."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "ONE short sentence justifying the choice, in your "
                            "own words. Never copy or restate the question."
                        ),
                    },
                },
                "required": ["id", "label", "confidence", "rationale"],
                "propertyOrdering": ["id", "label", "confidence", "rationale"],
            },
        }
    },
    "required": ["answers"],
    "propertyOrdering": ["answers"],
}

SYSTEM_PROMPT = """\
You are a senior medical educator marking a multiple-choice question bank. Each
question below has a stem and lettered options but NO printed answer key. For
each one, choose the single best option.

Return JSON matching the schema: for every question id, the option letter, a
confidence between 0 and 1, and ONE short sentence of rationale in your own
words.

Rules that matter more than being helpful:
1. Choose only from the letters actually printed for that question. Never invent
   a letter and never answer a question id that was not supplied.
2. Confidence must be honest. Use less than 0.5 when the question is ambiguous,
   when more than one option is defensible, when the stem depends on a figure,
   image, arrow or marking you cannot see, or when the stem looks truncated. The
   program discards every answer below 0.5 and leaves the key blank on purpose -
   a blank key is far better than a wrong one in an exam file.
3. Answer strictly from established clinical and basic-science knowledge. If the
   question follows a regional exam convention you are unsure of, lower the
   confidence rather than guessing.
4. Do not rewrite, translate, correct or comment on the question text. Your
   rationale is the only prose you produce, and it is shown to a reviewer, never
   printed as question text.
5. Answer every id supplied, exactly once.

Example output:
{"answers":[{"id":4,"label":"C","confidence":0.93,"rationale":"Intratympanic
steroids are the standard salvage therapy for sudden sensorineural hearing
loss."},{"id":5,"label":"A","confidence":0.35,"rationale":"The stem refers to a
structure marked in an image that is not available, so this is a guess."}]}
"""

_QUESTION_TEMPLATE = """\
--- question id {qid} ---
Stem: {stem}
Options:
{options}{extra}
"""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def detect_answers(
    questions: list[Question],
    doc: SourceDocument | None,
    provider: Provider | None,
    *,
    settings: Any = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Question]:
    """Fill in missing answer keys, in place, and return the same list.

    Only questions with `answer.origin is AnswerOrigin.NONE` are touched; a key
    the document stated is never second-guessed here. Batches run concurrently.
    Provider failures are recorded as warnings on `doc` and leave the affected
    questions unanswered -- never half-answered.
    """
    started = time.monotonic()
    if not questions:
        return questions

    pending = _select_pending(questions)
    if not pending:
        log.info("answer detection: nothing to derive")
        _report(progress, 0, 0)
        return questions

    if provider is None:
        for _, q in pending:
            _decline(q, "no AI provider configured", confidence=0.0)
        _warn(doc, "answer detection skipped: no AI provider configured")
        _report(progress, len(pending), len(pending))
        return questions

    batches = _make_batches(pending)
    total = len(pending)
    workers = _worker_count(settings, len(batches))
    log.info(
        "answer detection: %d question(s) in %d batch(es), %d worker(s), %s/%s",
        total, len(batches), workers, provider.name, provider.model,
    )

    done = 0
    calls = 0
    _report(progress, done, total)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ans") as pool:
        futures = {
            pool.submit(_run_batch, provider, batch): batch for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            calls += 1
            try:
                replies, warning = future.result()
            except Exception as exc:      # noqa: BLE001 - one batch must not stop the run
                log.exception("answer batch failed unexpectedly")
                replies, warning = {}, f"answer batch failed ({exc})"

            if warning:
                _warn(doc, warning)
            # Applied on this thread so no two threads ever touch one Question.
            _apply_batch(batch, replies)
            done += len(batch)
            _report(progress, done, total)

    derived = sum(1 for _, q in pending if q.answer.origin is AnswerOrigin.AI)
    log.info(
        "answer detection: %d/%d derived, %d declined, %d call(s), %.2fs",
        derived, total, total - derived, calls, time.monotonic() - started,
    )
    return questions


# --------------------------------------------------------------------------- #
# Selection / batching
# --------------------------------------------------------------------------- #


def _select_pending(questions: list[Question]) -> list[tuple[int, Question]]:
    """Questions we may honestly try to answer, as `(id, question)` pairs.

    The id is the question's position in the caller's list, so it is stable and
    unique even when several files were merged and question numbers repeat.
    """
    pending: list[tuple[int, Question]] = []
    for idx, q in enumerate(questions):
        if q.answer.origin is not AnswerOrigin.NONE:
            continue
        if len(q.options) < 2:
            # Nothing to choose between; flag it and move on.
            q.flag(Flag.NO_ANSWER)
            continue
        if q.has(Flag.REFERENCES_MISSING_IMAGE) and not q.images:
            _decline(
                q,
                "stem refers to an image that is not attached, so the answer "
                "cannot be derived honestly",
            )
            continue
        if not q.stem.strip():
            q.flag(Flag.NO_ANSWER)
            continue
        pending.append((idx, q))
    return pending


def _make_batches(
    pending: list[tuple[int, Question]],
) -> list[list[tuple[int, Question]]]:
    """Group questions into requests.

    Questions with attached images go one per request: inline images arrive as an
    unordered list of parts, so mixing two picture questions in one call makes
    the mapping from picture to question ambiguous -- and an answer derived from
    the wrong picture is precisely the failure mode we refuse to risk.
    """
    visual = [pair for pair in pending if pair[1].images]
    textual = [pair for pair in pending if not pair[1].images]

    batches: list[list[tuple[int, Question]]] = [[pair] for pair in visual]
    for start in range(0, len(textual), BATCH_SIZE):
        batches.append(textual[start : start + BATCH_SIZE])
    return [b for b in batches if b]


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #


def _run_batch(
    provider: Provider, batch: list[tuple[int, Question]]
) -> tuple[dict[int, dict], str]:
    """Ask for one batch of answers. Returns `({id: reply}, warning)`."""
    user = _render_batch(batch)
    images = _batch_images(batch)
    try:
        payload = provider.complete_json(
            SYSTEM_PROMPT,
            user,
            ANSWER_SCHEMA,
            images=images or None,
            timeout=REQUEST_TIMEOUT_S,
        )
    except ProviderError as exc:
        ids = ", ".join(str(i) for i, _ in batch)
        log.warning("answer batch [%s] failed: %s", ids, exc)
        return {}, f"answer detection failed for {len(batch)} question(s): {exc}"

    replies: dict[int, dict] = {}
    raw = payload.get("answers")
    if not isinstance(raw, list):
        raw = payload.get("items")
    if not isinstance(raw, list):
        return {}, "answer detection: reply had no 'answers' array"

    wanted = {qid for qid, _ in batch}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            qid = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if qid in wanted:
            replies[qid] = entry
    return replies, ""


def _render_batch(batch: list[tuple[int, Question]]) -> str:
    """Render the batch as plain text the model can reason over."""
    blocks: list[str] = []
    for qid, q in batch:
        options = "\n".join(
            f"{opt.label}. {_one_line(opt.text)}" for opt in q.options
        )
        extra = ""
        if q.images:
            extra = (
                f"\n({len(q.images)} image(s) for this question are attached to "
                f"this message.)"
            )
        elif q.tables:
            rows = "; ".join(
                " | ".join(cell.strip() for cell in row)
                for row in q.tables[0].rows[:8]
            )
            extra = f"\nTable accompanying the stem: {rows}"
        blocks.append(
            _QUESTION_TEMPLATE.format(
                qid=qid, stem=_one_line(q.stem), options=options, extra=extra
            )
        )
    return (
        f"{len(batch)} question(s) need an answer key.\n\n"
        + "\n".join(blocks)
        + "\nReturn one entry per question id."
    )


def _batch_images(batch: list[tuple[int, Question]]) -> list[ImageAsset]:
    images: list[ImageAsset] = []
    for _, q in batch:
        for img in q.images:
            if len(images) >= MAX_IMAGES_PER_CALL:
                return images
            images.append(img)
    return images


def _one_line(text: str) -> str:
    """Collapse a slice of source text onto one line for the prompt.

    Only the *prompt* copy is collapsed. Nothing here is ever written back into a
    `Question`, so verbatim text is untouched.
    """
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# Applying replies
# --------------------------------------------------------------------------- #


def _apply_batch(batch: list[tuple[int, Question]], replies: dict[int, dict]) -> None:
    for qid, q in batch:
        entry = replies.get(qid)
        if entry is None:
            _decline(q, "the model returned no answer for this question")
            continue
        try:
            _apply_reply(q, entry)
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("could not apply answer for id %d: %s", qid, exc)
            _decline(q, "the model's answer could not be read")


def _apply_reply(q: Question, entry: dict) -> None:
    """Adopt one reply, or decline it. Never invents text."""
    rationale = entry.get("rationale")
    rationale = _one_line(str(rationale))[:400] if isinstance(rationale, str) else ""

    try:
        confidence = float(entry.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    label = normalise_label(str(entry.get("label") or ""))
    option = q.option(label) if label else None

    if option is None:
        _decline(
            q,
            f"the model chose option {label or '?'}, which this question does "
            f"not have",
            confidence=confidence,
            rationale=rationale,
        )
        return

    if confidence < ACCEPT_FLOOR:
        _decline(
            q,
            f"the model was only {confidence:.0%} confident, below the "
            f"{ACCEPT_FLOOR:.0%} floor",
            confidence=confidence,
            rationale=rationale,
        )
        return

    q.answer = Answer(
        label=option.label,
        # Copied from the option, which is itself a verbatim source slice.
        text=option.text,
        origin=AnswerOrigin.AI,
        confidence=confidence,
        rationale=rationale,
        source_lines=list(option.source_lines),
    )
    _unflag(q, Flag.NO_ANSWER)
    q.flag(Flag.AI_ANSWER)
    if confidence < REVIEW_FLOOR:
        # Usable, but a human should confirm it before it becomes exam material.
        q.flag(Flag.LOW_CONFIDENCE)
    else:
        _unflag(q, Flag.LOW_CONFIDENCE)


def _decline(
    q: Question,
    reason: str,
    *,
    confidence: float = 0.0,
    rationale: str = "",
) -> None:
    """Leave the answer unknown, and say why in the review-only fields."""
    note = rationale
    detail = f"answer not derived: {reason}"
    q.answer = Answer(
        label=None,
        text="",
        origin=AnswerOrigin.NONE,
        confidence=confidence,
        rationale=f"{note} [{detail}]" if note else detail,
        source_lines=[],
    )
    q.flag(Flag.NO_ANSWER)
    q.flag(Flag.LOW_CONFIDENCE)
    log.debug("declined to answer Q%s: %s", q.number, reason)


def _unflag(q: Question, flag: Flag) -> None:
    if flag in q.flags:
        q.flags.remove(flag)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _worker_count(settings: Any, total: int) -> int:
    raw = getattr(settings, "max_concurrency", None) if settings is not None else None
    try:
        wanted = int(raw) if raw else DEFAULT_CONCURRENCY
    except (TypeError, ValueError):
        wanted = DEFAULT_CONCURRENCY
    return max(1, min(wanted if wanted > 0 else DEFAULT_CONCURRENCY, max(1, total)))


def _warn(doc: SourceDocument | None, message: str) -> None:
    """Surface a non-fatal problem where the UI can find it."""
    log.warning("%s", message)
    if doc is not None and message not in doc.warnings:
        doc.warnings.append(message)


def _report(progress: Callable[[int, int], None] | None, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:      # noqa: BLE001 - it is the caller's callback, not ours
        log.exception("progress callback raised")
