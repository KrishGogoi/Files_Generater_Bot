"""Screen-coordinate automation helpers.

Kept apart from the document pipeline: nothing here parses or writes files, and
nothing in `app.parse` / `app.export` depends on it. Importing this package pulls
in pyautogui, so it is imported lazily by the UI.
"""

from __future__ import annotations
