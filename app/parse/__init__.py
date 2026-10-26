"""Parsing: `SourceDocument` -> `list[Question]`.

`rules` is the deterministic parser (fast path, offline mode, and the AI's
cross-check). `providers` + `ai` add the span-anchored model pass, which returns
*line indices only* -- never question text. `reconcile` merges the two parses and
proves every emitted question is a verbatim slice of the source; `answers`
derives a missing answer key, and `merge` combines several files.

Nothing is imported here on purpose: pulling in `ai` would import an AI SDK for
callers that only ever need `rules`.
"""

from __future__ import annotations
