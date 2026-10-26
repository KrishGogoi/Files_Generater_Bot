"""Checks for the Word preview's document model and HTML renderer (no Qt)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.export import preview_html  # noqa: E402
from app.models import (  # noqa: E402
    Answer,
    AnswerOrigin,
    Flag,
    ImageAsset,
    Option,
    Parser,
    Question,
    TableAsset,
    WordExportConfig,
)
from app.ui import word_doc_model as edits  # noqa: E402
from app.ui.word_doc_model import WordDocModel  # noqa: E402


def q(n: int, src: int | None = None, **kw) -> Question:
    return Question(
        number=n,
        source_number=n if src is None else src,
        stem=f"Stem {n}",
        stem_lines=[n * 10],
        options=[Option("A", "one", [n * 10 + 1]), Option("B", "two", [n * 10 + 2]),
                 Option("C", "three", [n * 10 + 3])],
        answer=Answer(label="B", text="two", origin=AnswerOrigin.SOURCE, confidence=1.0),
        explanation=kw.get("explanation", ""),
        parser=Parser.RULES,
    )


def test_clean_untouched_document_needs_no_rebuild() -> None:
    m = WordDocModel.from_questions([q(1), q(2), q(3)], WordExportConfig())
    assert not m.modified and not m.needs_rebuild()
    assert [m.display_number(e.uid) for e in m.entries] == [1, 2, 3]


def test_chaotic_numbering_forces_rebuild_and_can_be_switched_off() -> None:
    m = WordDocModel.from_questions([q(1, src=5), q(2, src=1), q(3, src=0)], WordExportConfig())
    assert m.needs_rebuild()
    assert [m.display_number(e.uid) for e in m.entries] == [1, 2, 3]
    m.cfg.renumber = False
    assert [m.display_number(e.uid) for e in m.entries] == [5, 1, 3]
    assert m.needs_rebuild(), "a question the source never numbered still has to be written"


def test_exclude_reorder_and_export_numbers() -> None:
    m = WordDocModel.from_questions([q(1), q(2), q(3)], WordExportConfig())
    m.set_included(2, False)
    assert m.any_excluded and m.needs_rebuild()
    assert [m.display_number(e.uid) for e in m.entries] == [1, 0, 2]
    m.move(3, -2)
    assert [e.uid for e in m.entries] == [3, 1, 2] and m.order_changed
    out = m.export_questions()
    assert [x.number for x in out] == [1, 2] and [x.stem for x in out] == ["Stem 3", "Stem 1"]
    assert m.counts() == (3, 1, 0)


def test_layout_toggle_forces_rebuild() -> None:
    m = WordDocModel.from_questions([q(1)], WordExportConfig())
    m.cfg.include_explanation = False
    assert m.needs_rebuild()
    m.cfg.include_explanation = True
    assert not m.needs_rebuild()


def test_edit_functions_keep_the_review_semantics() -> None:
    x = q(1)
    x.flags = [Flag.VERBATIM_MISMATCH]
    x.verbatim_ok = False
    assert edits.edit_stem(x, "New stem")
    assert x.parser is Parser.MANUAL and x.verbatim_ok and Flag.VERBATIM_MISMATCH not in x.flags
    assert not edits.edit_stem(x, "New stem")
    assert edits.edit_option_text(x, 1, "TWO")
    assert x.answer.text == "TWO", "the answer body follows its option"
    changed, err = edits.set_answer_label(x, "Z")
    assert not changed and "not an option" in err
    changed, err = edits.set_answer_label(x, "c")
    assert changed and x.answer.label == "C" and edits.MANUAL_ANSWER_NOTE in x.notes
    assert edits.add_option(x, "four").label == "D"
    assert edits.remove_option(x, 0)
    assert [o.label for o in x.options] == ["A", "B", "C"]
    assert x.answer.label == "B", "the key follows its wording, not its old letter"
    assert edits.remove_option(x, 1)
    assert x.answer.label is None and Flag.NO_ANSWER in x.flags
    assert edits.edit_explanation(x, "Because.")
    assert x.explanation == "Because."


def test_model_sees_edits_made_elsewhere() -> None:
    a = q(1)
    m = WordDocModel.from_questions([a], WordExportConfig())
    assert not m.needs_rebuild()
    edits.edit_stem(a, "changed in the review table")
    assert m.any_edited and m.needs_rebuild()


def test_render_mirrors_the_box_layout() -> None:
    cfg = WordExportConfig()
    x = q(7, explanation="Explanation:\nBecause of X.\nOther Options:\nA. one is wrong\nC. three is wrong\nTake Home Message:\nRemember X.")
    x.images = [ImageAsset(data=b"\x89PNG\r\n\x1a\n", width=100, height=50, fmt="png", anchor_line=70)]
    x.tables = [TableAsset(rows=(("Column I", "Column II"), ("1. a", "b")), anchor_line=70)]
    html = preview_html.render_question(x, 4, cfg, uid=9, selected=True, image_src=lambda a, k: f"img:{k}")
    assert 'name="q9"' in html and 'href="edit:9"' in html
    assert "<b>Question 4</b>" in html and "was 7 in the source" in html
    assert html.count("<b>Ans. B) two</b>") == 2, "answer line, then repeated above the explanation"
    assert "<b>Explanation:</b>" in html and html.count("Explanation:") == 1, "no second label"
    assert "<b>Option </b><b>A.</b> one is wrong" in html and "<b>Option </b><b>C.</b>" in html
    assert "Column II" in html and 'src="img:q9s0"' in html
    assert "Remember X." in html and "<b>Take Home Message:</b>" in html
    plain = preview_html.render_question(x, 4, cfg, uid=9, included=False, image_src=lambda a, k: "")
    assert "excluded" in plain
    cfg.include_answer = False
    cfg.include_explanation = False
    bare = preview_html.render_question(x, 4, cfg, uid=9, image_src=lambda a, k: "")
    assert "Ans. B)" not in bare and "Because of X" not in bare


def test_render_document_orders_and_selects() -> None:
    cfg = WordExportConfig()
    items = [(1, q(1), 1, True), (2, q(2), 0, False), (3, q(3), 2, True)]
    html = preview_html.render_document(items, cfg, selected_uid=3, image_src=lambda a, k: "")
    assert preview_html.anchors(html) == [1, 2, 3]
    assert html.count("border:2px solid") == 1
    hidden = preview_html.render_document(items, cfg, show_excluded=False, image_src=lambda a, k: "")
    assert preview_html.anchors(hidden) == [1, 3]


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
