"""Export: `list[Question]` -> formatted Word and PowerPoint files.

`docx_format` holds the low-level OOXML surgery (fonts, zero spacing, nested
table colouring), `word_box` writes the one-table-per-question box document,
`verify` proves the written file still contains the source wording, and
`pptx_deck` builds the branded slide deck.

Left import-free so that `word_box` users never load python-pptx (and the other
way round).
"""

from __future__ import annotations
