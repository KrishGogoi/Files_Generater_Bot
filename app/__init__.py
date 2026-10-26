"""Krishna's Super Bot -- application package.

Layout: `models` (the shared data model, authoritative), `config` (settings),
`ingest` (any file -> `SourceDocument`), `parse` (`SourceDocument` -> questions),
`export` (questions -> .docx / .pptx) and `ui` (PySide6 screens).

Only the identity constants are re-exported here; everything else is imported
from its own module so that the headless CLI never drags in Qt.
"""

from __future__ import annotations

from app.config import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
