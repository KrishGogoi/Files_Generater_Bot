"""Multi-file merge with visible de-duplication.

Krishna drops several recall files at once and expects one clean, continuously
numbered question bank out of the other end. Two of those files usually share a
handful of questions.

The rule this module is built around: **a duplicate is never dropped silently.**
Every collapse is reported in ``ParseResult.warnings``, the survivor carries
``Flag.MERGED_DUPLICATE`` and a note naming what was folded into it, and the copy
that is kept is the one with the best answer provenance -- a question with the
document's own ``Ans. B)`` beats an AI-derived guess, which beats no answer at
all.
"""

from __future__ import annotations

import bisect
import copy
import difflib
import logging
import math
import os
import re
from typing import Sequence

from app.models import AnswerOrigin, Flag, ParseResult, Parser, Question

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: SOURCE > AI > NONE. Which copy of a duplicate survives.
_ORIGIN_RANK = {AnswerOrigin.SOURCE: 2, AnswerOrigin.AI: 1, AnswerOrigin.NONE: 0}

#: Below this many characters a stem carries too little signal for fuzzy
#: matching ("All of the following except:" is not an identity). Such questions
#: are still de-duplicated exactly, via Question.dedupe_key().
_MIN_FUZZY_STEM = 24

#: Minimum share of option bodies two near-identical stems must have in common
#: before they count as the same question. Real corpora are full of questions
#: whose stem is nothing but "All of the following are true EXCEPT:" followed by
#: completely different options; collapsing those would destroy real questions.
_MIN_OPTION_OVERLAP = 0.5

#: How many duplicates to name in the warning before summarising the rest.
_MAX_LISTED = 12


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _norm_stem(q: Question) -> str:
    return " ".join(q.stem.split()).casefold()


def _note(q: Question, text: str) -> None:
    if text and text not in q.notes:
        q.notes = f"{q.notes} | {text}" if q.notes else text


def _provenance_rank(q: Question) -> tuple[int, float, int]:
    """Sort key for "which copy of this question is the better one".

    Answer provenance first, then the parser's confidence, then option count --
    a copy that lost an option to a parse glitch should not win.
    """
    origin = q.answer.origin if q.answer.is_known else AnswerOrigin.NONE
    return (_ORIGIN_RANK.get(origin, 0), q.confidence, len(q.options))


def _option_bodies(q: Question) -> set[str]:
    return {b for b in (o.normalised() for o in q.options) if b}


def _options_compatible(a: Question, b: Question) -> bool:
    """True when two similar stems also agree about their options.

    Guards the near-duplicate test: without it, every "All of the following are
    true EXCEPT:" in the corpus collapses into a single question.
    """
    sa, sb = _option_bodies(a), _option_bodies(b)
    if not sa or not sb:
        return True  # one side never parsed options: nothing to contradict
    return len(sa & sb) / max(len(sa), len(sb)) >= _MIN_OPTION_OVERLAP


def _label(q: Question) -> str:
    """Short human identifier for a question, used in warnings and notes."""
    name = os.path.basename(q.source_path) if q.source_path else ""
    stem = " ".join(q.stem.split())
    if len(stem) > 60:
        stem = stem[:57] + "..."
    where = f" from {name}" if name else ""
    return f"#{q.number}{where}" + (f' "{stem}"' if stem else "")


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #


class _Clusterer:
    """Groups equal / near-equal questions, comparing only plausible candidates.

    Exact matches go through ``Question.dedupe_key()`` (a dict lookup). Fuzzy
    matches only ever compare a candidate against *cluster representatives*
    whose stem length could still reach `similarity`: for ``ratio = 2*M/T`` the
    shorter stem must be at least ``s/(2-s)`` of the longer one, which a bisect
    over a length-sorted index turns into a handful of comparisons instead of
    3000. The scan stays O(n^2) in the worst case but is cheap in practice.
    """

    __slots__ = ("similarity", "clusters", "_by_key", "_lens", "_norms", "_refs")

    def __init__(self, similarity: float) -> None:
        self.similarity = min(max(similarity, 0.0), 1.0)
        self.clusters: list[list[Question]] = []
        self._by_key: dict[str, int] = {}
        # Parallel arrays kept sorted by normalised stem length.
        self._lens: list[int] = []
        self._norms: list[str] = []
        self._refs: list[int] = []

    # -- public --------------------------------------------------------- #

    def add(self, q: Question) -> bool:
        """Add `q`. Returns True when it joined an existing cluster."""
        norm = _norm_stem(q)
        target = self._by_key.get(self._key(q))
        if target is None:
            target = self._find_similar(q, norm)
        if target is not None:
            self.clusters[target].append(q)
            return True
        self._open_cluster(q, norm)
        return False

    # -- internals ------------------------------------------------------ #

    def _key(self, q: Question) -> str:
        try:
            return q.dedupe_key()
        except (AttributeError, TypeError, ValueError):
            # A malformed question must not abort a 3000-question merge; it just
            # loses exact de-duplication and falls back to the fuzzy path.
            log.debug("dedupe_key failed for question %s", q.number, exc_info=True)
            return ""

    def _open_cluster(self, q: Question, norm: str) -> None:
        ref = len(self.clusters)
        self.clusters.append([q])
        key = self._key(q)
        if key:
            self._by_key.setdefault(key, ref)
        if len(norm) >= _MIN_FUZZY_STEM:
            pos = bisect.bisect_left(self._lens, len(norm))
            self._lens.insert(pos, len(norm))
            self._norms.insert(pos, norm)
            self._refs.insert(pos, ref)

    def _find_similar(self, q: Question, norm: str) -> int | None:
        s = self.similarity
        n = len(norm)
        if n < _MIN_FUZZY_STEM or s <= 0.0 or not self._lens:
            return None

        lo = int(math.floor(s * n / (2.0 - s)))
        hi = int(math.ceil(n * (2.0 - s) / s)) if s > 0 else n
        start = bisect.bisect_left(self._lens, lo)
        end = bisect.bisect_right(self._lens, hi)

        best: int | None = None
        best_ratio = 0.0
        for k in range(start, end):
            other = self._norms[k]
            sm = difflib.SequenceMatcher(None, norm, other)
            # real_quick_ratio/quick_ratio are cheap upper bounds; they reject
            # the overwhelming majority of pairs before the real O(n*m) work.
            if sm.real_quick_ratio() < s or sm.quick_ratio() < s:
                continue
            ratio = sm.ratio()
            if ratio < s or ratio <= best_ratio:
                continue
            ref = self._refs[k]
            if not _options_compatible(q, self.clusters[ref][0]):
                continue
            best, best_ratio = ref, ratio
        return best


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #


def _result_parser(results: Sequence[ParseResult]) -> Parser:
    parsers = {r.parser for r in results}
    if len(parsers) == 1:
        return parsers.pop()
    # Mixed inputs (e.g. one AI file and one offline file): AI_RULES is the
    # composite marker, and the per-question `parser` still tells the truth.
    return Parser.AI_RULES if parsers else Parser.RULES


def _shared_source(results: Sequence[ParseResult]):
    """The single SourceDocument, when every input came from the same file."""
    docs = [r.source for r in results if r.source is not None]
    if not docs:
        return None
    first = docs[0]
    return first if all(d.path == first.path for d in docs) else None


def merge(
    results: list[ParseResult],
    *,
    renumber: bool = True,
    drop_duplicates: bool = True,
    similarity: float = 0.92,
) -> ParseResult:
    """Concatenate several parses into one question bank.

    Questions keep their input order and their ``source_path`` (the review table
    shows provenance). With `renumber` they are numbered 1..N. With
    `drop_duplicates`, exact duplicates (``Question.dedupe_key()``) and
    near-duplicates (difflib ratio over the whitespace-normalised, casefolded
    stem ``>= similarity``) collapse to the copy with the best answer
    provenance, which is flagged ``Flag.MERGED_DUPLICATE``; what was dropped is
    reported in ``ParseResult.warnings``.
    """
    warnings: list[str] = []
    seen_warnings: set[str] = set()
    flat: list[Question] = []

    for res in results:
        for w in res.warnings:
            if w not in seen_warnings:
                seen_warnings.add(w)
                warnings.append(w)
        # Deep copies: merge flags, renumbers and annotates, and must never
        # mutate a ParseResult the caller still holds.
        flat.extend(copy.deepcopy(q) for q in res.questions)

    if drop_duplicates and len(flat) > 1:
        clusterer = _Clusterer(similarity)
        for q in flat:
            clusterer.add(q)
        clusters = clusterer.clusters
    else:
        clusters = [[q] for q in flat]

    # max() keeps the first maximum, so an equally-good earlier copy wins and the
    # output order follows first occurrence within the concatenated input.
    keepers = [max(cluster, key=_provenance_rank) for cluster in clusters]
    folded = [
        [q for q in cluster if q is not keeper]
        for cluster, keeper in zip(clusters, keepers)
    ]

    questions = keepers
    if renumber:
        # replace() is shallow, so annotate the *returned* objects from here on.
        questions = [q.renumbered(i) for i, q in enumerate(questions, start=1)]

    dropped: list[str] = []
    for keeper, others in zip(questions, folded):
        if not others:
            continue
        keeper.flag(Flag.MERGED_DUPLICATE)
        _note(
            keeper,
            f"merged {len(others)} duplicate(s): "
            + "; ".join(_label(q) for q in others),
        )
        dropped.extend(f"{_label(q)} -> kept {_label(keeper)}" for q in others)
        log.info("merged %d duplicate(s) into question %s", len(others), keeper.number)

    if dropped:
        listed = "; ".join(dropped[:_MAX_LISTED])
        if len(dropped) > _MAX_LISTED:
            listed += f"; ... and {len(dropped) - _MAX_LISTED} more"
        warnings.append(f"dropped {len(dropped)} duplicate question(s): {listed}")

    warnings.append(
        f"merged {len(results)} parse result(s): {len(flat)} question(s) in, "
        f"{len(questions)} out"
    )

    return ParseResult(
        questions=questions,
        source=_shared_source(results),
        parser=_result_parser(results),
        warnings=warnings,
        elapsed_s=sum(r.elapsed_s for r in results),
        ai_calls=sum(r.ai_calls for r in results),
    )
