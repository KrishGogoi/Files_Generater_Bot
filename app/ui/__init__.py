"""PySide6 user interface: Home -> Actions -> Review, plus the settings dialog.

`theme` supplies the palette and stylesheet, `sidebar` the nav rail that frames
every screen, `dashboard` the feature tiles you land on, `home` the drag-and-drop
start screen, `actions` the four action cards, `review` the editable results
table, `history` the recent-files list, `help_dialog` the short summary of all of
it and `settings_dialog` the API key / branding editor. `worker` runs every long
job on a QThread -- the golden rule here is that no document work happens on the
GUI thread.

Importing this package must not create any Qt object; `main.py` imports the
screens lazily so `--cli` works without a display.
"""

from __future__ import annotations
