r"""Plain text (.txt) -> `SourceDocument`.

The simplest reader, and the reference implementation of the package contract:
one `SourceLine` per line of the file, blank lines kept, nothing stripped.

Decoding order is utf-8 (BOM tolerated) then cp1252, because question banks
pasted out of Word on a Windows machine are usually one or the other. The last
resort is utf-8 with `errors="replace"`, which cannot fail -- a reader that
raises on one odd byte would lose the whole file for no good reason.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import DocKind, LineKind, SourceDocument
from . import _DocBuilder

log = logging.getLogger(__name__)

#: Tried in order; each entry is (codec, human name for the warning).
_ENCODINGS: tuple[tuple[str, str], ...] = (
    ("utf-8-sig", "utf-8"),
    ("cp1252", "cp1252"),
)


def read_txt(path: str | Path) -> SourceDocument:
    """Read a text file into one `SourceLine` per line."""
    p = Path(path)
    raw = p.read_bytes()
    builder = _DocBuilder(str(p), DocKind.TXT)
    text, encoding, replaced = _decode(raw)
    if replaced:
        builder.warn(
            "file is not valid utf-8 or cp1252; undecodable bytes were replaced"
        )
    log.debug("%s: decoded %d bytes as %s", p.name, len(raw), encoding)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # A file ending in a newline is not a file with a trailing blank line.
    if lines and lines[-1] == "":
        lines.pop()
    for n, line in enumerate(lines):
        builder.add_line(
            line, kind=LineKind.PARAGRAPH, block_id=n, ref=f"txt:{n}"
        )
    return builder.finish()


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """Return (text, encoding used, whether characters had to be replaced)."""
    for codec, name in _ENCODINGS:
        try:
            return raw.decode(codec), name, False
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace", True
