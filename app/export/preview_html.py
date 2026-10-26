"""On-screen rendering of the Word box layout -- the preview behind the
"Extract & Format" screen.

Produces the same five-row box `app.export.word_box` writes, as HTML that
`QTextBrowser` (a deliberately small HTML subset: tables, paragraphs, bold,
images) can show:

    Question 3                          header row, black bold on white
    <stem> (+ pictures, + grids)
    A.  option   B.  option ...         aligned labels
    Ans. B) text                        bold
    Ans. B) text  + Explanation ...     bold, then the block with the house
                                        keyword rules applied

The keyword rules are the exporter's own (`docx_format.match_keyword`,
its option-entry pattern and block enders), called from here rather than
re-implemented, so what the screen shows is what the file will contain. The
one thing this module adds is the review furniture: a tint for a question
that needs attention, a greyed box for one the user has excluded, and an
outline on the one being edited.

No Qt in this file: the image resolver is a callback, which lets the Qt
screen register each picture as a document resource and a test hand in a
data URL instead.
"""

from __future__ import annotations

import base64
import html
import re
from typing import Callable, Iterable, Sequence

from app.export import docx_format as fmt
from app.models import (
    AnswerOrigin,
    ImageAsset,
    Question,
    Spans,
    TableAsset,
    WordExportConfig,
    clamp_spans,
)

#: ``image_src(asset, key) -> URL`` -- how a picture is referenced from the HTML.
ImageResolver = Callable[[ImageAsset, str], str]

#: Colours that carry meaning on screen (never written to the file).
ATTENTION_TINT = "#FFF3D6"
EXCLUDED_TEXT = "#9A9A9A"
SELECTED_OUTLINE = "#1AB7EA"
MARKER_COLOUR = "#B26B00"

_OPTION_ENTRY_RE = fmt.OPTION_RE


def data_url(asset: ImageAsset, _key: str = "") -> str:
    """A ``data:`` URL for `asset` -- the resolver used by tests and exports."""
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff"}.get(
        asset.fmt.lower(), "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(asset.data).decode('ascii')}"


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _line_spans(text: str, bold: Spans) -> list[tuple[str, Spans]]:
    """Split `text` into visual lines, carrying each line's share of `bold`.

    The same arithmetic as `word_box._line_spans`, so bold lands on the same
    characters on screen as in the file.
    """
    out: list[tuple[str, Spans]] = []
    at = 0
    for line in _split_lines(text):
        end = at + len(line)
        out.append(
            (
                line,
                tuple(
                    (max(s, at) - at, min(e, end) - at)
                    for s, e in bold
                    if s < end and e > at
                ),
            )
        )
        at = end + 1
    return out


def _runs_html(text: str, bold: Spans = ()) -> str:
    """`text` with the bold ranges wrapped in ``<b>``; tabs become spacing."""
    spans = clamp_spans(bold, len(text))
    parts: list[str] = []
    at = 0
    for start, end in spans:
        if start > at:
            parts.append(_esc(text[at:start]))
        parts.append("<b>" + _esc(text[start:end]) + "</b>")
        at = end
    if at < len(text):
        parts.append(_esc(text[at:]))
    return "".join(parts).replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;") or "&nbsp;"


def _para(inner: str, *, bold: bool = False, italic: bool = False,
          colour: str | None = None, extra_style: str = "") -> str:
    style = "margin:0;" + extra_style
    if colour:
        style += f"color:{colour};"
    body = inner
    if bold:
        body = f"<b>{body}</b>"
    if italic:
        body = f"<i>{body}</i>"
    return f'<p style="{style}">{body}</p>'


# --------------------------------------------------------------------------- #
# Explanation: the house keyword rules, as the exporter applies them
# --------------------------------------------------------------------------- #


def explanation_paragraphs(lines: Sequence[tuple[str, Spans]]) -> list[str]:
    """The explanation's paragraphs as HTML, keyword rules applied.

    Mirrors `docx_format.apply_keyword_rules` and `label_incorrect_options`:
    a keyword line gets its label bold and a blank line above and below (not
    doubled when one is already there); inside an Incorrect/Other Options
    block every "A. ..." entry gets a bold "Option " prefix, a bold letter and
    a blank line above. Leading and trailing blanks are dropped, as
    `word_box._drop_edge_blanks` drops them from the cell.
    """
    out: list[str] = []
    in_block = False

    def blank() -> None:
        if out and out[-1] != _BLANK:
            out.append(_BLANK)

    for text, spans in lines:
        stripped = text.strip()
        base = fmt.match_keyword(text)
        if base is not None:
            llen = fmt.label_length(text, base)
            label, rest = text[:llen], text[llen:]
            blank()
            out.append(_para("<b>" + _esc(label.strip()) + "</b>"))
            if rest.strip():
                shift = llen
                rest_spans = tuple(
                    (max(s, shift) - shift, e - shift) for s, e in spans if e > shift
                )
                out.append(_para(_runs_html(rest, rest_spans)))
            blank()
            in_block = base in (fmt.KW_INCORRECT, fmt.KW_OTHER)
            continue
        if not stripped:
            blank()
            continue
        if in_block and (
            _OPTION_ENTRY_RE.match(stripped) or fmt.LABELLED_OPTION_RE.match(stripped)
        ):
            lead = len(text) - len(text.lstrip())
            body = text[lead:]
            if fmt.LABELLED_OPTION_RE.match(body):
                head_len = len(fmt.OPTION_LABEL_TEXT) + 2
                head, tail = body[:head_len], body[head_len:]
                prefix = ""
            else:
                head, tail = body[:2], body[2:]
                prefix = "<b>" + _esc(fmt.OPTION_LABEL_TEXT) + "</b>"
            tail_spans = tuple(
                (max(s, lead + len(head)) - (lead + len(head)), e - (lead + len(head)))
                for s, e in spans
                if e > lead + len(head)
            )
            blank()
            out.append(_para(prefix + "<b>" + _esc(head) + "</b>" + _runs_html(tail, tail_spans)))
            continue
        out.append(_para(_runs_html(text, spans)))

    while out and out[0] == _BLANK:
        out.pop(0)
    while out and out[-1] == _BLANK:
        out.pop()
    return out


_BLANK = _para("&nbsp;")


# --------------------------------------------------------------------------- #
# Pieces of a box
# --------------------------------------------------------------------------- #


def _grid_html(asset: TableAsset, cfg: WordExportConfig) -> str:
    if asset.is_empty:
        return ""
    rows: list[str] = []
    for r, row in enumerate(asset.rows):
        cells: list[str] = []
        for c in range(asset.n_cols):
            text = row[c] if c < len(row) else ""
            if cfg.color_nested_tables and r == 0:
                cells.append(
                    f'<td bgcolor="#{cfg.header_fill}" style="padding:3px;">'
                    f'<b><span style="color:#FFFFFF;">{_esc(text) or "&nbsp;"}</span></b></td>'
                )
            else:
                band = f' bgcolor="#{cfg.band_fill}"' if cfg.color_nested_tables and r % 2 == 1 else ""
                cells.append(f'<td{band} style="padding:3px;">{_esc(text) or "&nbsp;"}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table width="100%" border="1" cellspacing="0" cellpadding="3" '
        f'style="border-collapse:collapse; border:1px solid #{cfg.border_color};">'
        + "".join(rows) + "</table>"
    )


def _image_html(asset: ImageAsset, key: str, image_src: ImageResolver) -> str:
    src = image_src(asset, key)
    if not src:
        return ""
    width = min(int(asset.width) if asset.width else 480, 520)
    return f'<p style="margin:0; text-align:center;"><img src="{_esc(src)}" width="{width}"></p>'


def _answer_line(q: Question) -> str:
    ans = q.answer
    if not ans.is_known or not ans.label:
        return "Ans. (not determined)"
    body = ans.text
    if not body.strip():
        opt = q.option(ans.label)
        body = opt.text if opt is not None else ""
    body = body.strip()
    return f"Ans. {ans.label.strip().upper()})" + (f" {body}" if body else "")


def _explanation_window(q: Question) -> tuple[int, int] | None:
    if not q.explanation_lines:
        return None
    return min(q.explanation_lines), max(q.explanation_lines)


def _split_by_section(assets, window):  # noqa: ANN001 - mirrors word_box
    if window is None:
        return list(assets), []
    lo, hi = window
    return (
        [a for a in assets if not lo <= a.anchor_line <= hi],
        [a for a in assets if lo <= a.anchor_line <= hi],
    )


# --------------------------------------------------------------------------- #
# One question box
# --------------------------------------------------------------------------- #


def render_question(
    q: Question,
    number: int,
    cfg: WordExportConfig,
    *,
    uid: int = 0,
    selected: bool = False,
    included: bool = True,
    image_src: ImageResolver = data_url,
) -> str:
    """One question as its Word box. `number` is the number to print."""
    keep_bold = bool(cfg.preserve_bold)
    font = f"font-family:'{cfg.font_name}'; font-size:{cfg.font_size_pt}pt;"
    text_colour = EXCLUDED_TEXT if not included else "#000000"
    border = f"border:1px solid #{cfg.border_color};"
    outline = f"border:2px solid {SELECTED_OUTLINE};" if selected else border
    tint = ATTENTION_TINT if (included and q.needs_attention) else "#FFFFFF"

    rows: list[str] = []

    def cell(inner: str, *, bg: str | None = None) -> str:
        colour = f' bgcolor="{bg}"' if bg else ""
        return f'<tr><td{colour} style="{border} padding:4px 6px; color:{text_colour};">{inner}</td></tr>'

    # -- header ------------------------------------------------------------ #
    head = f'<a name="q{uid}"></a><a href="edit:{uid}" style="color:{text_colour}; text-decoration:none;"><b>Question {number}</b></a>'
    notes: list[str] = []
    if not included:
        notes.append("excluded — will not be exported")
    elif q.needs_attention:
        notes.append("needs attention")
    if q.source_number > 0 and q.source_number != number:
        notes.append(f"was {q.source_number} in the source")
    if notes:
        head += f'&nbsp;&nbsp;<span style="color:{MARKER_COLOUR}; font-size:{max(8, cfg.font_size_pt - 3)}pt;"><i>{_esc(" · ".join(notes))}</i></span>'
    rows.append(cell(_para(head), bg=f"#{cfg.question_fill}" if included else "#F2F2F2"))

    # -- stem ---------------------------------------------------------------- #
    window = _explanation_window(q)
    stem_images, expl_images = _split_by_section(q.images, window)
    stem_tables, expl_tables = _split_by_section(q.tables, window)
    stem_parts = [
        _para(_runs_html(line, spans))
        for line, spans in _line_spans(q.stem, q.stem_bold if keep_bold else ())
    ]
    for k, im in enumerate(stem_images):
        stem_parts.append(_image_html(im, f"q{uid}s{k}", image_src))
    for t in stem_tables:
        stem_parts.append(_grid_html(t, cfg))
    rows.append(cell("".join(stem_parts) or _para("&nbsp;"), bg=tint if q.needs_attention and included else None))

    # -- options ------------------------------------------------------------- #
    opt_rows: list[str] = []
    for k, opt in enumerate(q.options):
        label = (opt.label or "").strip() or "-"
        body = _runs_html(opt.text, opt.bold if keep_bold else ())
        opt_rows.append(
            f'<tr><td width="34" valign="top" style="padding:0;">{_esc(label)}.</td>'
            f'<td valign="top" style="padding:0;">{body}</td></tr>'
        )
        for j, im in enumerate(opt.images):
            opt_rows.append(f'<tr><td></td><td>{_image_html(im, f"q{uid}o{k}i{j}", image_src)}</td></tr>')
    options_html = (
        '<table border="0" cellspacing="0" cellpadding="0" width="100%">' + "".join(opt_rows) + "</table>"
        if opt_rows else _para("&nbsp;")
    )
    rows.append(cell(options_html))

    # -- answer ---------------------------------------------------------------- #
    if cfg.include_answer:
        ans = _para("<b>" + _esc(_answer_line(q)) + "</b>")
        if q.answer.origin is AnswerOrigin.AI and cfg.mark_ai_answers and cfg.ai_answer_marker:
            ans = _para("<b>" + _esc(_answer_line(q)) + "</b><i>" + _esc(cfg.ai_answer_marker) + "</i>")
        rows.append(cell(ans))

    # -- explanation row ------------------------------------------------------ #
    show_expl = bool(cfg.include_explanation and q.explanation.strip())
    keep_row = show_expl or (cfg.include_answer and cfg.repeat_answer_in_explanation)
    if keep_row:
        parts: list[str] = []
        if cfg.include_answer and cfg.repeat_answer_in_explanation:
            parts.append(_para("<b>" + _esc(_answer_line(q)) + "</b>"))
        if show_expl:
            lines = _line_spans(q.explanation, q.explanation_bold if keep_bold else ())
            if _needs_explanation_label(lines):
                parts.append(_para("<b>Explanation:</b>"))
            parts.extend(explanation_paragraphs(lines))
            for k, im in enumerate(expl_images):
                parts.append(_image_html(im, f"q{uid}e{k}", image_src))
            for t in expl_tables:
                parts.append(_grid_html(t, cfg))
        rows.append(cell("".join(parts) or _para("&nbsp;")))

    return (
        f'<table width="100%" border="1" cellspacing="0" cellpadding="4" '
        f'style="border-collapse:collapse; {outline} {font}">'
        + "".join(rows) + "</table>"
    )


def _needs_explanation_label(lines: Sequence[tuple[str, Spans]]) -> bool:
    """As `word_box._needs_explanation_label`: no label when the block has one."""
    explanation_bases = getattr(fmt, "EXPLANATION_BASES", frozenset({fmt.KW_EXPLANATION}))
    for text, _ in lines:
        if fmt.match_keyword(text) in explanation_bases:
            return False
    first = next((text for text, _ in lines if text.strip()), "")
    return fmt.match_keyword(first) is None


# --------------------------------------------------------------------------- #
# The whole document
# --------------------------------------------------------------------------- #


def render_document(
    items: Iterable[tuple[int, Question, int, bool]],
    cfg: WordExportConfig,
    *,
    selected_uid: int | None = None,
    image_src: ImageResolver = data_url,
    show_excluded: bool = True,
) -> str:
    """Every box in order. `items` are ``(uid, question, number, included)``.

    The gap between boxes stands in for the blank paragraph the exporter puts
    after every table so two boxes never merge into one.
    """
    body: list[str] = []
    for uid, q, number, included in items:
        if not included and not show_excluded:
            continue
        body.append(
            render_question(
                q, number, cfg, uid=uid, selected=(uid == selected_uid),
                included=included, image_src=image_src,
            )
        )
        body.append('<p style="margin:0; font-size:6pt;">&nbsp;</p>')
    if not body:
        body.append('<p style="margin:12px; color:#777;"><i>No questions to show.</i></p>')
    font = f"font-family:'{cfg.font_name}'; font-size:{cfg.font_size_pt}pt;"
    return f'<div style="{font}">' + "".join(body) + "</div>"


_ANCHOR_RE = re.compile(r'name="q(\d+)"')


def anchors(html_text: str) -> list[int]:
    """The uids of the boxes present in a rendered document (for tests)."""
    return [int(m.group(1)) for m in _ANCHOR_RE.finditer(html_text)]
