"""Rule-parser checks for the shapes chaotic source files actually take.

Run directly (``python tests/test_rules_chaotic.py``) or under pytest. Every
case below is a real layout seen in a user document, reduced to a few lines:
the assertion says what the parser must make of it, and the name says why.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import DocKind, Flag, LineKind, SourceDocument, SourceLine  # noqa: E402
from app.parse import reconcile, rules  # noqa: E402


def doc_from(text: str) -> SourceDocument:
    lines = [
        SourceLine(idx=i, text=t, kind=LineKind.PARAGRAPH, block_id=i)
        for i, t in enumerate(text.strip("\n").split("\n"))
    ]
    return SourceDocument(path="synthetic.txt", kind=DocKind.TXT, lines=lines)


def parse(text: str):
    d = doc_from(text)
    r = rules.parse(d)
    for q in r.questions:
        ok, detail = reconcile.verify_question(q, d)
        assert ok, f"Q{q.number} not verbatim: {detail}"
    return r.questions


# --------------------------------------------------------------------------- #


def test_numbered_subitems_are_not_questions() -> None:
    qs = parse(
        """
2. Choose the correct statements from the following:
A. Low BP stimulates baroreceptors
B. Cardiac output lowers with deepening plane of anesthesia
Ans. A

3. What is the order of structures pierced during lumbar puncture?
1. Ligamentum Flavum
2. Supraspinatus ligament
3. Interspinous ligament
4. Dura
5. Sub dural
6. Arachnoid
A. 1,2,3
B. 2,3,1
Ans. B
"""
    )
    assert [q.source_number for q in qs] == [2, 3]
    assert qs[1].has(Flag.NUMBERED_SUBITEMS)
    assert qs[1].stem.count("\n") == 6
    assert [o.label for o in qs[1].options] == ["A", "B"]


def test_numbered_list_inside_explanation_is_not_a_question() -> None:
    """The OPHTHAL phantom: an explanation re-lists statements 1-4 and the
    '4.' arrives exactly when question 4 is expected."""
    qs = parse(
        """
3. Regarding CRAO, evaluate the following statements:
A. T F F T
B. T F T F
C. F T F T
D. T T F T
Correct Answer
D. T T F T
Explanation
1. Sudden, painless vision loss is an early feature.
True
2. RAPD may be present.
True
3. Photophobia is typical.
False
4. A history of amaurosis fugax may precede CRAO.
True
Educational Objective
CRAO classically presents with sudden painless loss.

4. A 68-year-old woman develops gradual visual decline after cataract surgery.

A. Nd:YAG laser capsulotomy
B. Surgical capsule scraping
C. Intraocular lens exchange
D. Intravitreal triamcinolone
Correct Answer
A. Nd:YAG laser capsulotomy
"""
    )
    assert [q.source_number for q in qs] == [3, 4]
    assert qs[0].answer.label == "D"
    assert qs[1].answer.label == "A"
    assert qs[1].stem.startswith("A 68-year-old")
    assert "amaurosis" in qs[0].explanation


def test_numbering_restart_and_section_title() -> None:
    qs = parse(
        """
5. A 27-year-old woman develops a postural headache after neuraxial anesthesia.
A. Age
B. Timing of ambulation
C. Pregnancy
D. Type of needle
Correct Answer
B. Timing of ambulation
Explanation
Timing of ambulation does not influence the incidence of PDPH.

FMGE MOCK 2  / USE SAME for 29th sept NEET GT :
1.A patient taking low-dose aspirin is scheduled for an elective procedure.
A. Continue aspirin
B. Stop aspirin and replace with heparin
C. Stop aspirin and give a platelet transfusion
D. Stop aspirin on morning of surgery
Correct Answer
A. Continue aspirin
"""
    )
    assert [q.source_number for q in qs] == [5, 1]
    assert [q.number for q in qs] == [1, 2]
    assert qs[0].answer.label == "B", "the second question's key must not overwrite the first"
    assert "FMGE MOCK" not in qs[0].explanation
    assert qs[1].answer.label == "A"


def test_out_of_sequence_number_with_options_opens_a_question() -> None:
    qs = parse(
        """
5. Regarding malignant hyperthermia, which statements are correct?
i. It is triggered by succinylcholine.
ii. It involves RYR1 on chromosome 19.
A. i only
B. i and ii
Answer: B. i and ii
Explanation
Both statements are correct.

2. In the context of capnography waveforms, identify the correct statements:
i. Phase II represents mixed gas.
ii. A shark fin indicates obstruction.
A. i only
B. i and ii
Answer: B. i and ii
"""
    )
    assert [q.source_number for q in qs] == [5, 2]
    assert [q.number for q in qs] == [1, 2]


def test_mcq_keyword_and_nested_header() -> None:
    qs = parse(
        """
1.MCQ 1: Match the Following
Match the anesthetic agent with its most characteristic property.
OPTIONS
A) 1-b, 2-c, 3-d, 4-a
B) 1-c, 2-b, 3-d, 4-a
Correct Answer: A) 1-b, 2-c, 3-d, 4-a
Correct Option Explanation
Desflurane has a very high vapour pressure.

MCQ 2: Assertion-Reasoning
Assertion (A): Succinylcholine is contraindicated after major burns.
Reason (R): Burns upregulate extrajunctional receptors.
OPTIONS
A) Both A and R are true, and R explains A
B) Both A and R are true, but R does not explain A
Correct Answer: A) Both A and R are true, and R explains A
"""
    )
    assert [q.source_number for q in qs] == [1, 2]
    assert qs[0].stem.startswith("Match the Following\nMatch the anesthetic")
    assert qs[0].explanation.startswith("Correct Option Explanation")
    assert qs[1].stem.startswith("Assertion-Reasoning\nAssertion (A)")
    assert [o.text for o in qs[0].options][0] == "1-b, 2-c, 3-d, 4-a"


def test_unnumbered_question_with_choices_header() -> None:
    qs = parse(
        """
OBGY September GTs :
FMGE MOCK - 1

A 26-year-old woman presents with sudden severe lower abdominal pain.

Choices:
A. Ultrasound + Xray
B. Ultrasound + Color Doppler
C. CECT pelvis
D. MRI
CorrectChoice:
Ultrasound + Color Doppler
Solution:
Ans. B) Ultrasound + Color Doppler
Ovarian torsion is a clinical emergency.

2. A 32-year-old woman presents with primary infertility.
A. Transvaginal ultrasound
B. MRI
C. CT scan
D. Laparoscopy
Correct Choice:
D. Laparoscopy
"""
    )
    assert len(qs) == 2
    assert qs[0].source_number == 0 and qs[0].number == 1
    assert qs[0].stem == "A 26-year-old woman presents with sudden severe lower abdominal pain."
    assert qs[0].answer.label == "B"
    assert [o.label for o in qs[1].options] == ["A", "B", "C", "D"], "Correct Choice must not become option D twice"
    assert qs[1].answer.label == "D"


def test_unnumbered_question_at_document_start_from_option_run() -> None:
    qs = parse(
        """
A patient presents with ear itching and a blocked sensation for 4 days.

A. Furunculosis
B. Keratosis
C. Malignant otitis externa
D. Otomycosis
Correct Answer
D. Otomycosis
Explanation
Otomycosis is a fungal infection.

2. A newborn boy is diagnosed with unilateral anotia.
A. 6 years of age
B. 12 years of age
Correct Answer
A. 6 years of age
"""
    )
    assert len(qs) == 2
    assert qs[0].stem.startswith("A patient presents with ear itching")
    assert qs[0].answer.label == "D"


def test_other_options_relisting_is_not_a_new_question() -> None:
    qs = parse(
        """
2. Which pairs a cell death pathway with its defining feature?
A. Pyroptosis is caspase-independent.
B. Necroptosis is caspase-dependent.
C. Ferroptosis is iron-dependent lipid peroxidation.
D. Anoikis is caused by misfolded proteins.
Explanation
Ferroptosis is an iron-dependent form of regulated cell death.
Other Options
A. Pyroptosis is caspase-independent. This option is Incorrect.
B. Necroptosis is caspase-dependent.
This option is Incorrect.
D. Anoikis is caused by misfolded proteins.
This option is Incorrect.
Educational Objective
Ferroptosis is distinct from apoptosis.

3. A myocardial sample after reversible ischaemia shows:
A. Councilman bodies
B. Myelin figures
Correct Answer
B. Myelin figures
"""
    )
    assert [q.source_number for q in qs] == [2, 3]
    assert qs[0].has(Flag.NO_ANSWER)
    assert "Other Options" in qs[0].explanation


def test_unlabelled_options_before_answer_line() -> None:
    qs = parse(
        """
16. Choose the correct statements regarding Halothane :
1) Exposure to halothane causes 20 to 30 mmHg drop in BP
2) Fall in BP starts early and parallel to the depth of anaesthesia
3) Halothane directly reduces cardiac output
1 and 2
1,2 and 3
Only 1
All of the above
Answer : B
17. What is the order of structures pierced during lumbar puncture?
1) Ligamentum flavum
2) Supraspinous ligament
3) Interspinous ligament
1 2- 3
2 3- 1
3 1- 2
Answer - 2 3- 1
"""
    )
    assert [q.source_number for q in qs] == [16, 17]
    assert [o.text for o in qs[0].options] == ["1 and 2", "1,2 and 3", "Only 1", "All of the above"]
    assert qs[0].answer.label == "B"
    assert qs[0].has(Flag.LOW_CONFIDENCE)
    assert qs[0].stem.count("\n") == 3
    assert [o.text for o in qs[1].options] == ["1 2- 3", "2 3- 1", "3 1- 2"]
    assert qs[1].answer.label == "B", "an answer given as text is matched to the option"


def test_unlabelled_options_after_blank_line() -> None:
    qs = parse(
        """
Q 3. A 5-year-old child presents with fever and tender erythematous plaques.

Sweet Syndrome
Behcet's Syndrome
Pyoderma Gangrenosum
Juvenile Dermatosis
Ans. A) Sweet Syndrome
Explanation
Sweet syndrome is acute febrile neutrophilic dermatosis.
"""
    )
    assert len(qs) == 1
    assert [o.text for o in qs[0].options] == [
        "Sweet Syndrome", "Behcet's Syndrome", "Pyoderma Gangrenosum", "Juvenile Dermatosis",
    ]
    assert qs[0].answer.label == "A"
    assert qs[0].stem == "A 5-year-old child presents with fever and tender erythematous plaques."


def test_tick_mark_on_option_is_the_answer() -> None:
    qs = parse(
        """
9. Which statement about the action potential curve is correct?
A. The nerve has increased excitability at point E
B. The threshold occurs at point A
C. The interval from B to D is the refractory period ✅
D. The change from C to D is sodium-channel opening
"""
    )
    assert len(qs) == 1
    assert qs[0].answer.label == "C"
    assert qs[0].options[2].text == "The interval from B to D is the refractory period"
    assert not qs[0].has(Flag.TRUNCATED), "'point A' is a label, not a dangling article"
    assert not qs[0].has(Flag.REFERENCES_MISSING_IMAGE)


def test_false_positive_flags_are_gone() -> None:
    qs = parse(
        """
6. A newborn develops SCID with marked sensitivity to ionizing radiation. Which repair pathway is defective?
A. Homologous recombination
B. Non-homologous end joining
Ans. B
7. Which cylinder is MRI safe?
A. Stainless steel, Size E
B. Molybdenum steel, Size A
Ans. A
8. Which is correct about measuring height?
A. Child should be measured with shoes on
B. Child should be measured with shoes removed
Ans. B
9. What is the anion gap?
A. 26
B. 22
Ans. A
"""
    )
    assert len(qs) == 4
    for q in qs:
        assert not q.has(Flag.REFERENCES_MISSING_IMAGE), q.number
        assert not q.has(Flag.TRUNCATED), q.number


def test_letter_only_options_mean_a_picture() -> None:
    qs = parse(
        """
1. Which among the following muscles marked is an abductor of vocal cord?
A. A
B. C
C. B
D. D
2. Identify the structure shown in the image.
A. Ulnar nerve
B. Median nerve
"""
    )
    assert all(q.has(Flag.REFERENCES_MISSING_IMAGE) for q in qs)


def test_second_answer_line_does_not_overwrite_the_first() -> None:
    qs = parse(
        """
1. Stem one
A. x
B. y
Correct Answer
B. y
Explanation
Some text.
Correct Answer
A. x
"""
    )
    assert qs[0].answer.label == "B"
    assert "disagreed" in qs[0].notes


def test_renumbering_records_source_numbers() -> None:
    qs = parse(
        """
Q7. First
A. a
B. b
Q9. Second
A. a
B. b
"""
    )
    assert [q.number for q in qs] == [1, 2]
    assert [q.source_number for q in qs] == [7, 9]
    changed = rules.assign_numbers(qs)
    assert changed is False, "assign_numbers is idempotent"


def test_strip_answer_marker_forms() -> None:
    assert rules.strip_answer_marker("Aspirin ✅") == (True, "Aspirin")
    assert rules.strip_answer_marker("✅ Aspirin") == (True, "Aspirin")
    assert rules.strip_answer_marker("Aspirin (correct)") == (True, "Aspirin")
    assert rules.strip_answer_marker("Grade A*") == (False, "Grade A*")
    assert rules.strip_answer_marker("✅") == (False, "✅")


def test_strip_question_prefix_nested() -> None:
    assert rules.strip_question_prefix("1.MCQ 1: Match the Following") == (1, "Match the Following")
    assert rules.strip_question_prefix("1. 2 mg of morphine") == (None, "1. 2 mg of morphine")
    assert rules.strip_question_prefix("Q1. 2 mg of morphine") == (1, "2 mg of morphine")
    assert rules.strip_question_prefix("Q 4.A 34-year-old man") == (4, "A 34-year-old man")


# --------------------------------------------------------------------------- #


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report, keep going
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
