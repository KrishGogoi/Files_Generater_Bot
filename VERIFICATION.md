# Verification record

Everything below is a measured result, not an expectation. Commands were run on
Windows 11, Python 3.14.3, PySide6 6.11, google-genai 1.62.

Reproduce the headline number with:

```
python "<scratchpad>/verify_corpus.py"
```

---

## 1. Verbatim preservation — the core guarantee

The app's central promise is that wording is **never** altered, only reformatted.
It is enforced structurally: the AI returns *line indices*, and all text is
sliced from the source by `SourceDocument.text_of()`.
`reconcile.verify_question()` proves it by re-deriving every stem and option from
its stored indices and comparing.

Run across every `.docx` in the fixture set, `D:\Bhanu Sir` and `D:\Anthony Sir`
(29 files, read-only):

| metric | result |
|---|---|
| files parsed | 29 |
| questions parsed | **409** |
| answers found stated in the source | 180 |
| questions flagged for human review | 232 |
| **verbatim mismatches** | **0** |

> **Note on re-running this.** `D:\Bhanu Sir` (19 flat MCQ files, 188 of those
> questions) was removed from the machine after the measurement above. Re-running
> the script now covers the 10 remaining files and reports **221 questions, 0
> verbatim mismatches** — every per-file count identical to the table in §2. The
> 409 figure stands as measured; it simply cannot be reproduced until those files
> are back. Point the script at their new location to restore full coverage.

## 2. Per-file question counts (rule-based parser)

| file | questions | options | answers | note |
|---|---|---|---|---|
| `box_5row_Qdot.docx` | 60 | 240 | 60 | 60 × 4, complete |
| `Question_1_boxes.docx` | 60 | 240 | 60 | 60 × 4, complete |
| `Mission 3.0 E&D .docx` | 60 | 240 | 60 | 60 × 4 after two bug fixes (was 210) |
| `flat_Qprefix_noans.docx` | 13 | 52 | 0 | was 0 before `Q1.` support |
| `OBG Recall File.docx` | 13 | 52 | 0 | `Q1.`-prefixed |
| `flat_substems_noans.docx` | 3 | 8 | 0 | correct — **not** 9 |
| `notes_formatted.docx` | 1 | 2 | 0 | revision notes, not an MCQ bank |
| 19 × `* Recall Question.docx` | 3–19 each | ~4 per question | 0 | no answer keys in source |

`flat_substems_noans.docx` is the numbered-sub-item trap: a stem reading
*"What is the order of structures pierced during lumbar puncture?"* is followed
by its own `1.`–`6.` list. A bare `^\d+[.)]` splitter yields **9** questions; the
parser yields **3** and marks the sub-list as stem content.

## 3. Bugs found by verification and fixed

Each was found by running against the real corpus, not by inspection.

| # | Bug | Impact | Fix |
|---|---|---|---|
| 1 | `gemini-2.5-flash` retired (`404`) | every AI call would fail on a new key | default → `gemini-3.1-flash-lite`, 4 files |
| 2 | Inline options (`Options: A) x B) y`) compared against the whole line | 29 false "verbatim mismatch" reports | `reconcile._derive_option_forms` splits inline options |
| 3 | A bare `B.` option (empty body) reported as a mismatch | 1 false report | empty body accepted as a legitimate form |
| 4 | Non-label lines appended to an inline option as "continuations" | option provenance claimed lines its text never came from | continuation disabled when `inline_options` |
| 5 | `Options: A. <text>` lost option A into the stem | **30 of 60** questions in a real file lost option A while stating `Answer: A` | `strip_options_prefix` retry in `_try_options` |
| 6 | `Options: A <text>` (no punctuation) lost option A | 1 more question, same failure mode | `_bare_letter_label`, guarded to the expected label only |
| 7 | `Q1.` rejected when the stem starts with a digit | **first question of a real file silently dropped** | the "1.5 mg" guard now applies only to bare numbers |
| 8 | `"" in ".)]"` is `True` in Python | `5 year old boy presents` opened a phantom question 5 | explicit empty-delimiter check |
| 9 | Answer letter not among the options was **not** blocking | a broken answer key would export with no warning | new blocking `Flag.ANSWER_NOT_IN_OPTIONS` |
| 10 | Single-letter option `"A"` matched the dangling-connector regex | false "CUT OFF" flags on image-labelling questions | connector test now needs ≥ 2 words |

Bug 9 is the one worth re-reading: the source said `Answer: A` while listing only
B/C/D, and `needs_attention` was `False`. Silently wrong exam material is the
worst thing this app could produce, so that case is now always blocking.

## 4. Header recognition

14/14 cases correct:

**Accepted:** `Q1. 23-year-old G2P1 woman…`, `1. A patient is posted`, `Q.1`,
`Question 1`, `Question 1 25-year-old man`, `3) What is the order`, `Q7)  Stem`

**Rejected:** `1.5 mg of drug`, `12. 05. 2024 was the date`, `Q1.5 mg dose`,
`Q10-12 are based on the following`, `5 year old boy presents`,
`2 weeks later the patient`, `H. pylori causes ulcers`

## 5. Exports

Checked by an independent script that re-derives the facts rather than trusting
the app's own verifier.

**Word** (`box_5row_Qdot_formatted.docx`, 60 questions):

| check | result |
|---|---|
| question tables | 60 |
| rows-per-table shapes | `{4: 60}` — every question identically structured |
| runs not Calibri 14 | **0** of 422 |
| paragraphs without zero before/after spacing | **0** |
| option/stem text divergences vs source | **0** |

**PowerPoint** (same source): 122 slides = 1 title + 60 question + 60 answer +
1 thank-you. **0 out-of-bounds shapes** — shape overflow was the most common real
defect in the original scripts.

## 6. PowerPoint contents panel

A pre-flight panel (`app/ui/pptx_options.py`) opens before any deck is built —
from the action menu *and* from the review table's Export button — so deck
contents are a per-deck decision. Every choice was built through the CLI and the
resulting deck read back with python-pptx:

| choice | slides | first question slide holds |
|---|---|---|
| default | 122 | `Q1. A 40-year-old man is referred…` |
| label `Just "Q."` | 122 | `Q. A 40-year-old man…` |
| label off | 122 | `A 40-year-old man…` (no prefix) |
| options off | 122 | stem only |
| answer off | 62 | 1 title + 60 questions + 1 closing |
| answer as banner | 62 | answer on the question slide |
| no title/closing slide | 120 | 60 questions + 60 answers |
| question text off | 122 | `Q1.` + the four options |
| options only | 62 | the four options, no heading |

**0 out-of-bounds shapes in all nine variants.** Panel logic verified: the label
radios disable when the label is unticked, the answer-placement radios disable
when answers are off, the config passed in is never mutated (so Cancel really
cancels), and ticking nothing off shows a red warning and disables **Build deck**
rather than producing 60 blank slides.

## 6b. Three defects reported from real use — fixed and re-verified

Reported after building a deck from
`Subjects\Radiology\Radiology August GTs_revised.docx` (27 questions). That file
introduced a format none of the earlier fixtures had: **all four options mashed
into one paragraph with no separator** (`…of the abdomenB. Multidetector…`).

| # | Reported | Cause | Fix |
|---|---|---|---|
| 1 | Deck showed `Q1. 1. A 55-year-old woman…` | `ai.py` built the stem with `doc.text_of()`, which never stripped the source's own numbering. The rule-based parser always had (`_stem_text`), so only the AI path was affected. | `_stem_from_lines()` strips the source prefix at both stem-construction sites |
| 2 | Reference tables appeared on question slides | Tables were attached anywhere inside a question's line span, so explanation charts (anchored at lines 318–652) were pulled in | `Question.tables_between_stem_and_options()` — only grids anchored between the stem's first line and the first option; also narrowed at attachment time in `ai.py` |
| 3 | Question box had a black outline | `_set_box_border` wrote `a:ln` with a solid black fill | `_set_no_border` writes `a:ln/a:noFill`; the dead border helper and its constants were removed |

Re-verified on the rebuilt deck (27 questions, 29 slides, AI on, 16.3 s):

- duplicated question numbers: **0** of 27 stem boxes — they read `Q1. A 55-year-old woman…`
- tables on question slides: **0**
- outlined stem boxes: **0**; `<a:ln><a:noFill/></a:ln>` present on **29/29** slides

Regression on the table filter, so the fix does not over-reach: across the
fixtures plus the Radiology file, 20 tables were attached and **1** kept — and a
synthetic question with a grid anchored between its stem and its options keeps
that grid and renders it (1 table in the deck, 0 out-of-bounds shapes). Genuine
match-the-columns grids survive; explanation tables do not.

Note 3 is why `_set_no_border` writes an explicit `a:noFill` rather than just
omitting `a:ln`: a textbox with no line element inherits the theme's line style,
which is how a stray outline comes back in PowerPoint.

## 6c. CLI deck flags are two-way

The first version of the `--no-*` deck flags could only turn things **off**, so
once the GUI panel had saved "no answers" there was no way to re-enable answers
headlessly. Every flag is now a `--x / --no-x` pair defaulting to `None`
(unspecified = keep the saved preference). Verified across 10 variants, including
a `re-enable answers` case that produces 122 slides from a saved
`include_answers: false`. **0 out-of-bounds shapes in all 10.**

## 6d. Light theme, and a colour bug it exposed

The app now opens **light** (`Settings → Branding & output → Appearance`, stored as
`Settings.theme`, validated against `("light", "dark")` and falling back to light).
Both themes were screenshot-reviewed across Home, Actions, Review, Settings and
the deck panel.

Switching to light surfaced three defects that had been present all along:

| Defect | Why it happened | Fix |
|---|---|---|
| **Amber "needs attention" and red "verbatim mismatch" row tints never rendered** — the table said *"6 need attention"* while every row looked identical | Qt discards a model's `BackgroundRole` for item painting as soon as the style sheet contains *any* `QTableView::item` rule. Ours styles `::item` for padding, so the tints were computed and thrown away | `_RowTintDelegate` paints the tint itself, independent of the sheet |
| Settings tab read **"Branding _output"** | `QTabWidget` treats `&` as a mnemonic marker | escaped to `&&` |
| **"Build deck"** and **"Save"** were not primary-styled | `setProperty("variant", …)` on a `QDialogButtonBox` standard button has no effect — those buttons are already polished, and a dynamic-property selector needs a re-polish | `theme.set_variant()`, which re-polishes |

The row-tint bug is the significant one: the review table's entire purpose is that
a flagged question is impossible to miss, and until now a flagged row was pixel-
identical to a clean one in **both** themes. Verified fixed in light and dark.

Note the two `&` bugs point opposite ways, which is why each needed its own fix:
`QLabel` does **not** consume `&` (so the action cards need a single `&`), while
`QTabWidget` and `QPushButton` **do** (so they need `&&`).

## 6e. Login gate, dashboard and Bulk Entry Automation

### Password store (`app/auth.py`)

Salted **scrypt** (n=32768, r=8, 64-byte key), in its own `auth.json` so a settings
reset cannot clear it. Measured: 0.25 s to set, 0.12 s to verify. Verified by test:
short passwords refused, wrong password refused, empty refused, change-with-wrong-
old refused, old password stops working after a change, clear removes the file, and
**the password does not appear anywhere in the stored file**.

One real bug found and fixed here: OpenSSL caps scrypt memory at 32 MiB by default,
which n=2¹⁵/r=8 lands exactly on, so every call was failing with "memory limit
exceeded" and silently falling back to PBKDF2 — and the availability probe hid it by
testing with n=2 instead of the real parameters. Both fixed; scrypt is now genuinely
in use (`describe()` reports `scrypt, n=32768, r=8`).

### Flow (`app/ui/login.py`, `app/ui/dashboard.py`)

20/20 checks passed in an end-to-end GUI walk, against a throwaway credential file:

| checked | result |
|---|---|
| starts on the lock screen, toolbar hidden | pass |
| first run offers create mode with a confirm field | pass |
| mismatched confirmation stores nothing | pass |
| too-short password refused | pass |
| correct creation lands on the dashboard, toolbar appears | pass |
| six feature tiles present, including Bulk Entry | pass |
| a document tile presets the action and opens the drop zone | pass |
| Lock returns to the lock screen in unlock mode, confirm hidden | pass |
| wrong password stays locked and counts the failure | pass |
| right password unlocks to the dashboard | pass |

The toolbar and status bar are hidden while locked, so Settings and Back cannot be
used to step around the lock, and locking clears the queued files and parsed result
rather than leaving a question bank on screen behind the lock.

#### After the shell was rebuilt around a nav rail

The toolbar became `app/ui/sidebar.py` (Home / History / Settings / Help down the
left edge). That moved the four `QAction`s onto the window, where their shortcuts
would have stayed live on the lock screen — hiding a toolbar used to disable them
for free. `_set_chrome_visible` now disables them explicitly. Re-checked with a
scripted walk over the real `MainWindow`, 23 checks, all passing:

| checked | result |
|---|---|
| starts on the lock screen, rail hidden | pass |
| `Ctrl+,` and `Ctrl+L` disabled while locked | pass |
| unlocking shows the rail and re-enables them | pass |
| each rail entry reaches its screen, and lights only itself | pass |
| a History row loads its file into the drop screen, starting no job | pass |
| a feature tile still presets the action and opens the drop zone | pass |
| the dashboard's Lock pill re-locks, hiding the rail and the shortcuts again | pass |
| Clear history empties `recent_files` and shows the empty state | pass |

### Bulk entry (`app/automation/bulk_entry.py`, `app/ui/bulk_entry.py`)

Coordinates are the original script's, verified identical: box 1 pastes at
(210, 221) and submits at (210, 252) — the original's `221 + 17 + 14`. Box 25:
(210, 856) and (210, 887).

| checked | result |
|---|---|
| layout validates against a 1920×1080 screen | all 25 boxes fit |
| layout on an 800×600 screen | 31 problems reported, run refused |
| spreadsheet paste, tabs + blank lines | first column only, 4 values from 5 lines |
| empty value list | refused |
| 30 values for 25 boxes | refused |
| dry run | walks all boxes, reports each coordinate, clicks nothing |
| cancellation | stops mid-run, reports `cancelled=True` |
| dry run through the real UI on a worker thread | "7 box(es) checked, nothing clicked"; table and buttons re-enabled |

A second real bug was found by that last test: if a job failed *immediately*, the
error handler cleared `self._thread` before `_start` assigned it, so the screen
stayed permanently disabled. State is now tracked in a separate `_running` flag and
the thread is only adopted if the job is still going.

**Not tested:** a live run against the real backend. Dry run exercises every step
except the clicks themselves — the actual mouse driving, and whether the backend
accepts what is pasted, can only be confirmed against the real window.

## 6f. Word box layout reworked — ten rules, checked against the real corpus

Requested after using the Word export on real subject files. Each rule is
asserted by `scratchpad/check_box_rules.py`, which runs the export and reads the
produced `.docx` back at the XML level.

| # | rule | how it is checked | result |
|---|---|---|---|
| 1 | no logo/subject/faculty/date page | every body paragraph outside a table must be blank | 0 non-box paragraphs in 4 files |
| 2 | questions in boxes only | first body child is a `w:tbl` | pass |
| 3 | question row plain white, black font | `w:shd/@w:fill` and the run's `w:color` | `FFFFFF` / `000000` on all 65 boxes |
| 4 | answer repeated, bold, above `Explanation:` | the row's first line starts `Ans. `, every run bold | 65 of 65 explanation rows |
| 5 | never a second `Explanation:` | count of lines opening with the word, per row | 0 added (see below) |
| 6 | no blank line around the keyword | neighbours of every keyword line | 0 blank neighbours of its own (see 6b) |
| 7 | same for `Educational Objective:`, bold | added to `KEYWORD_BASES`; label bolded | 184 keyword lines, 184 bold |
| 8 | same for `Other Options:`; bold `Option A.` | `Option ` prefix count, bold on the letter | 138 labels, both halves bold |
| 9 | source bold preserved | bold runs in the output vs. annotated questions | 1505 bold runs across 65 questions |
| 10 | all borders black | every side of every `w:tblBorders` | `000000`, 0.5 pt, no exceptions |

**Rule 6b — superseded. The house style is `format_docs.py`, rule for rule.**
Two rounds of trying to improve on it (removing the blank lines around headings,
then putting them only between option entries) both made the output worse, so
the rules are now exactly the ones in that script: a blank line above and below
every keyword line, and a blank line above every option entry.

The check that matters is no longer a count of blank lines, it is a direct
comparison. `scratchpad/check_final.py` imports the user's own `format_docs.py`,
runs `process_file()` over each source, and diffs its output against the app's,
paragraph by paragraph:

| box-format file | rows identical | paragraphs identical |
|---|---|---|
| `Ch 02 Medical Law, Ethics and Consent.docx` | yes | yes |
| `Ch 04 Thanatology.docx` | yes | yes |
| `Ch 05 Mechanical and Firearm Injuries.docx` | yes | yes |
| `Ch 06 Asphyxia.docx` | yes | yes |
| `Ch 07 Thermal, Electrical and Lightning Injuries.docx` | yes | yes |
| `Ch 08 Sexual Offences and Sexology.docx` | yes | yes |
| `ch13 Motor Systems & Higher Functions.docx` | yes | yes |

**Files differing from `format_docs.py`: 0.**

**The rule is idempotent**, which it was not when first written. An entry already
reading `Option A. …` — from an earlier run over the same file, or a source that
writes it that way — did not match `OPTION_RE`, so a second run found an empty
block, added no blanks, and `apply_keyword_rules` deleted the ones the first run
had added: 486 paragraphs became 446. `label_incorrect_options` now counts an
already-labelled line as an entry. Re-running the whole pass over its own output
leaves the paragraph count unchanged on all 9 files, and the verbatim check still
reports character-exact.

## 6g. Two defects found while verifying 6b

**Every figure inside an explanation was being moved into the question row.**
`_write_question_box` wrote `q.images` and `q.tables` — *all* of them — into the
stem cell, so a units chart or a radiograph that sat in the middle of an
explanation came out stacked under the question, as if it were part of it. In the
corpus that is 4 of 4 grids in the Forensic files, both grids in Radiology, and
14 of 15 pictures in the Anatomy file. Assets are now routed by `anchor_line`
against the explanation's own line span, and an explanation asset is emitted
after the line it was anchored to. Anything not clearly inside the explanation —
including an unanchored asset — stays with the stem, which is where everything
used to go, so no anchor problem can lose one.

| | result |
|---|---|
| questions carrying a picture or a grid | 33 |
| placed in the row the source had it in | **33** |
| placed in the wrong row | **0** |

**5 pictures were being dropped from the export in silence.** python-docx
identifies an image by sniffing literal signature bytes — `"JFIF"` or `"Exif"`
six bytes in — so it raises `UnrecognizedImageError` for an Adobe JPEG (one that
opens on an APP14 segment, `ff d8 ff ee`) and for a WMF, both of which Word
embeds without complaint. The old code logged a debug-level warning and carried
on, so 4 forensic photographs and 1 radiology metafile never reached the
handout and nothing said so on screen.

`_repair_image` grafts the 18-byte JFIF APP0 header onto an Adobe JPEG and
rasterises anything else through Pillow. The JPEG path touches no compressed
byte, which matters when the picture is a radiograph:

| picture | before | after | dimensions | pixels identical |
|---|---|---|---|---|
| Adobe JPEG ×4 | 47 425 / 30 412 / 25 631 / 24 602 B | +18 B each | unchanged | **yes** |
| WMF ×1 | 2 523 248 B | 371 497 B (PNG) | 843×749, unchanged | **yes** |

Across the 8 exported sources: **27 pictures in, 27 pictures out** (was 22).

## 6m. The AI parser dropped every "Take-Home Message" block in a real file

Reported against `Ch 06 proteins Corrected.docx`: the output had none of the
content under the `Take-Home Message:` heading.

The heading is spelled with a **hyphen**. The prompt shows the model the headings
an explanation is made of — `"Explanation:"`, `"Incorrect Options:"`,
`"Take Home Message:"`, `"Tags:"` — all with spaces, so it ended the block at the
last line it recognised. Every line after that belonged to no question: not
exported, not flagged, gone. The rule-based parser kept all 17, which is why this
only appears when AI is on, and why it was invisible to every `--no-ai` test.

| | Take-Home blocks in output | source lines absent |
|---|---|---|
| rules only | 17 of 17 | 38 |
| AI, before | **0 of 17** | 79 |
| AI, after | **17 of 17** | 38 |

(The 38 are the same in both paths and are deliberate reformatting: four options
printed on one source line become four paragraphs, and `Answer: A. …` is
rewritten as `Ans. A) …`.)

Two changes, because there were two faults:

**`_absorb_trailing_lines` (`app/parse/ai.py`).** No prompt wording makes lost
lines impossible, so the *span* decides rather than the heading list: a question
owns the lines from its own last line to the line before the next question, and
anything non-blank there is appended to its explanation. Trailing-only on
purpose — absorbing from the middle of a question would move stem text below the
answer, trading a silent loss for a silent reordering. The scan stops at a
question header, without which `"Question 2"` (a line no question claims, since
the exporter writes its own header row) was swept into question 1's explanation.
That was caught by checking the output, not by reasoning about it.

**Separator-tolerant keyword matching (`app/export/docx_format.py`).**
`match_keyword` now folds any run of spaces, tabs and hyphen-like characters, so
`Take-Home Message`, `Take Home Message` and `Take‑Home Message` are one
heading. Without it the block was *present but unformatted* — no bold, no blank
lines, and it did not close an Incorrect/Other Options list. `label_length` also
consumes repeated colons, because this file writes `Take-Home Message::` and
taking one would leave a stray `:` opening the content line.

Re-checked across all 29 `.docx` files now in `Subjects/` plus the reported file:
**29 of 29 verbatim character-exact and idempotent, 0 problems.**

## 6l. The fifth row of a question box was conditional on there being an explanation

`Sample.docx` is the format of record: four questions, each a five-row box, the
fifth row carrying the answer line a second time with the explanation underneath
when there is one. Its question 1 has **no** explanation and still has that fifth
row, which is the case the box writer got wrong — it created the row only when
`q.explanation` was non-empty.

Measured over `Test.docx` (57 questions, only one of which states an
explanation):

| | before | after |
|---|---|---|
| boxes with 5 rows | 1 | **57** |
| boxes with 4 rows | 56 | 0 |
| boxes whose 5th row is empty | — | **0** |

The repeated answer is now written on the same terms as the answer row above it,
rather than only when the answer is known, so the two rows always read alike.
The `Explanation:` label is written only when there is an explanation to label —
without that guard every answer-only question gained a heading over nothing.

Across the export configurations, no combination produces an empty trailing row:

| config | rows per box |
|---|---|
| defaults | 5 |
| `repeat_answer_in_explanation` off | 4 (5 where the source has an explanation) |
| `include_explanation` off | 5 |
| `include_answer` off | 3 (4 with an explanation) |
| answer *and* explanation off | 3 — the fifth row would be blank, so it is dropped |

## 6k. Question slides were packed to the top with the slack dumped underneath

Reported as the question and its options not being evenly spaced.

The slide is laid out top-down — stem box, rule, options — so whatever vertical
room the question does not use ends up as one dead band at the foot. The *gap*
between question and options was already constant at 0.35 cm; what varied was
everything below. Measured over a 17-question deck:

| | before | after |
|---|---|---|
| gap above the options | 0.35 cm on every slide | 0.98 – 2.99 cm, with the question's length |
| margin below the last option | **1.25 – 5.27 cm** | 0.63 – 2.64 cm |
| \|gap above − margin below\| | **up to 4.92 cm** | **0.35 cm, on every slide** |

The leftover is now split in half between the two. The residual 0.35 cm is
`_OPTS_TOP_GAP_CM`, the deliberate clearance under the blue rule, and it is the
same on every slide — so the block is evenly spaced, not merely centred.

Two things this does *not* do. It leaves the stem box and the rule exactly where
they were, so the rule stays tight under the question it underlines. And it is
skipped when the slide carries a table or an image: those are placed from
`opts_top` downwards and already fill the space this would be giving away, so
moving the options would only squeeze them.

`_options_height_cm` exists because `_estimate_text_height_cm` measures text
only. `_add_options_box` also adds 2 pt before and 6 pt after each option plus a
0.1 cm frame margin — with four options that is nearly a centimetre, and leaving
it out is the difference between evenly spaced and nearly evenly spaced.

Checked across the pre-flight combinations (`--no-question-text`,
`--no-options`, `--no-answers`, `--no-answer-next-slide`): all build. With the
in-slide answer banner on, 14 banner slides, **0 options overlapping the
banner**, smallest clearance 0.25 cm.

## 6j. Every deck was being built without the Academy logo

Reported after a deck came out with no logo on the title or thank-you slide.

`_add_logo` was being called for both slides and was working. What had gone was
the file: `DEFAULT_LOGO_PATH` was a hard-coded absolute string,
`d:/Python project/MY OWN FUCKING PROJECT/Cerebellum_Logo.png`, and the project
folder had since been renamed to `...BATCOMPUTER`. So `default_logo_path()`
returned `""`, the saved profile kept the dead path, the migration only filled a
logo in when the setting was *empty* rather than when it was *broken*, and
`_add_logo` skipped the missing file at `logger.info` — invisible at the default
level. Four separate things each declining to mention it.

The logo is now found relative to `config.py` (beside `main.py`, or the folder
above it), so no absolute path is stored anywhere and renaming the project cannot
break it again. `_resolve_logo` heals a stored path that no longer resolves, and
the skip in `_add_logo` is a warning.

| stored `logo_path` | resolves to |
|---|---|
| empty | the bundled house logo |
| the house logo at its old path | **re-pointed** to where it actually is, logged |
| the house logo at a valid path | unchanged |
| a *custom* logo that is missing | **left alone**, warned — not ours to replace |

The logo now ships in `app/ui/assets/Cerebellum_Logo.png`, beside the app's own
`app_logo.jpg`. Moving it there needed no settings change: the search order
begins at `assets`, and the stored path healed itself on the next start —

```
INFO app.config: house logo moved:
  ...\Cerebellum_Logo.png -> ...\KrishnasSuperBot\app\ui\assets\Cerebellum_Logo.png
```

which is the same mechanism working for its own maintenance rather than for a
user's renamed folder.

Deck rebuilt from `Ch 04 Thanatology.docx`, 22 slides:

| slide | pictures | size | position |
|---|---|---|---|
| 1 (title) | 1 | 5.51 × 5.50 cm | x = 14.18 cm, centred on a 33.9 cm slide |
| 22 (thank you) | 1 | 5.51 × 5.50 cm | x = 14.18 cm, centred |

The embedded image's SHA-1 matches `Cerebellum_Logo.png` byte for byte on both.

## 6i. A box document is restyled, never rebuilt

Reported against `ch13 Motor Systems & Higher Functions.docx`: the
`Tags: #Hyperrevision#Factual` row had been swallowed into the row above it.

The cause was rebuilding. That file is **already** a box document — question 1 is
`tbl:0` with rows 0-5, and `Tags:` is row 5 of its own (`tbl:0/r:5/c:0/p:0` in the
reader's line refs). The box writer has five slots — header, stem, options,
answer, explanation — and no slot for a sixth row, so `Tags:` fell into the
explanation. Everything §6g and §6h describe was the same mistake seen from a
different angle: a file that needed no rebuilding was being rebuilt, and each
round of repairs was patching a model that should not have been applied.

`has_question_boxes()` now asks the question directly, using the reader's own
definition of a question box, and `extract` restyles such a file in place instead
— which is what `format_docs.py` does and always did. The box writer is reached
only by a genuinely flat document, which has no rows to keep.

| source | route | verbatim |
|---|---|---|
| 6 × Forensic `Ch NN …docx` | restyled in place | character-exact |
| `ch13 Motor Systems & Higher Functions.docx` | restyled in place | character-exact |
| `ANATOMY AUG 5TH FMGE GT.docx` (flat) | box writer | 17 boxes built |
| `Radiology August GTs_revised.docx` (flat) | box writer | 26 boxes built |

Restyling in place also retires the two problems below for every box document:
nothing moves between sections because nothing is re-laid-out, and no row grows,
so nothing new can overflow a page. Both still apply to the box writer, and both
fixes stay in it for the flat case.

## 6h. Word was clipping every question whose explanation embedded a grid

Reported against `ch13 Motor Systems & Higher Functions.docx`: correct through
question 16, then from the `Incorrect Options:` heading of question 17 to the
start of question 18 nothing appeared on the page.

Nothing was missing from the file. All 23 boxes held their heading, their three
`Option n.` entries, their `Take Home Message:` and their `Tags:` line, and the
blank-line rule measured clean on every one of them. The content was written and
then **not drawn**.

**Word cannot break a table row across pages when that row contains a nested
table.** It lays out what fits and clips the remainder — no warning, no overflow
mark. Moving grids into the explanation row (§6g) is what created the condition:
the explanation is the tall row, and 11 of the 23 questions in that file put a
grid inside it. Question 17's is one of the largest, ~2 000 characters of prose
plus a five-row grid, which is over a page on its own.

The fix is structural rather than a size tweak: **an embedded grid never shares a
row with prose.** Each one takes a row of its own — short, so it always fits —
and the prose either side gets rows with no nested table in them, which Word is
free to break wherever it likes. `fmt.weld_rows` then sets the touching cell
borders to `nil`, which beats the table's own `insideH`, so the box still reads
as one unbroken box rather than one with a rule through the middle of an
explanation. The same applies to a grid in the stem row.

Measured over the 9-file corpus (170 questions):

| | result |
|---|---|
| rows holding a nested grid | 21 |
| ...that also carry prose | **0** (want 0 — prose is what makes a row overflow) |
| ...with their internal rules erased | **21 of 21** |
| questions whose assets are in the right section | **33 of 33** |

Box 17 of the reported file now reads: row 4 the prose before the grid, row 5 the
grid alone, row 6 the `Incorrect Options:` block through to `Tags:` — three rows
Word can lay out independently, welded so they look like one.

**Rule 5 was a case-sensitivity bug, not a missing check.** The old code did test
for the source's own heading, but `match_keyword` compared case-sensitively while
the real files write `EXPLANATION:` (Anatomy), `Explanation:` (Radiology) and
`Other options:` (Pathology) interchangeably. `Explanation` ≠ `EXPLANATION` meant
the guard never fired and a second label was added above the first. Matching is
now case-insensitive; the canonical spelling is used only for comparison and is
never written back, so the source's own casing survives untouched.

**A second defect was found while verifying rule 4.** Repeating the answer line
made it obvious that it read `Ans. B) ✅ Correct Answer: B) Facial vein → …` — the
label twice. `ANSWER_RE` anchored on the keyword, and a leading `✅` defeated the
anchor, so the whole line was stored as the answer *body*. A corpus scan for
ornament-prefixed lines found exactly three glyphs in 40 files (`✅` ×10, `◦` ×9,
`❌` ×2), so a bounded leading-ornament allowance was added to `ANSWER_RE` and
`SECTION_RE`. The line now reads `Ans. B) Facial vein → …`.

Bold preservation (rule 9) is span-based, not text-based, which is what keeps it
inside the verbatim guarantee: `app/ingest/docx_reader.py` records the character
ranges each source line shows in bold, `app/parse/styles.py` re-bases them onto
the text the parsers sliced out, and `word_box` splits that text into runs at
those offsets. No character is added, removed or re-cased. A run whose text
cannot be located in the paragraph aborts the whole paragraph's styling rather
than shifting the offsets, and editing a question in the review table clears its
recorded ranges.

Whole-corpus regression after all of the above: **593 questions across 29 files,
0 verbatim mismatches.**

Two rows in the rules-only run still hold two `Explanation` lines each
(Radiology Q21, Pathology Q14). Neither is added by the exporter — both are the
source's own, inside a question whose explanation the rule parser over-extended
across a section boundary where the file restarts numbering at `5.`. Through the
real path (AI + reconcile) both files split correctly and no row has more than
one: Radiology 26 → 27 questions, Pathology 14 → 16, Anatomy 17 (the file's own
header says "TOTAL Q'S : 17"), all with 0 verbatim mismatches.

## 6g. Compact UI

Chrome metrics reduced across the board rather than per screen: base font 14 → 13
px, button padding 8/16 → 6/13, input padding 7/10 → 5/9, table rows 5/8 → 3/7,
header sections 8/10 → 5/9, `h1` 30 → 24 px, card radius 10 → 8, scrollbars 12 →
10 px, and screen margins roughly a quarter tighter. The review splitter now
opens 420/420 instead of 520/320 — the detail pane was clipping its own option
rows, which is the pane where the actual fixing happens.

Re-shot on the real display and reviewed by eye: dashboard, drop screen, action
menu, review table, settings dialog, deck panel, login and bulk entry. No
clipping, no overlap, no Qt warnings. The login, bulk-entry, deck-options and
table-regression suites all still report no problems; the login suite's assertion
about the removed "Change action" toolbar item was updated to assert its absence.

## 6h. Two logos, kept apart

The app logo (`app/ui/assets/app_logo.jpg`, 669×630) now appears on the lock
screen, in the dashboard header and as the window/taskbar icon. The academy logo
stays on the PowerPoint title slide and nowhere else.

| checked | result |
|---|---|
| `theme.app_logo()` at 44 / 132 / 210 px, square and free aspect | correct sizes, non-null, rounded corners |
| `theme.app_icon()` | 7 sizes, 16 → 256 px |
| dashboard header | app logo, centre-cropped square; no longer reads `brand.logo_path` |
| lock screen, both modes | app logo above the title; padlock glyph is the fallback when the asset is absent |
| deck media parts, by SHA-1 | academy logo present, **app logo absent** |
| login suite | all 20 checks pass at the new logo size |

The deck check compares SHA-1 digests of the actual `ppt/media/*` entries against
both files, so it would catch the app logo leaking into an export rather than
merely trusting the code path.

## 6i. Batman dark theme + wallpaper

The dark palette is **sampled from the wallpaper**, not eyeballed. The image is a
single hue family — everything in it sits at h≈200–208°, with saturation falling
from 66% in the deepest part of the suit to 20% in the lit cloud, and lightness
running 5% (night sky, stone plinth) to 85% (moon):

| sampled region | hex | role it became |
|---|---|---|
| night sky / stone plinth | `#080F15`, `#080D13` | `bg` `#070C13` |
| chest armour | `#06141D` | `surface` `#0C1721` |
| cape mid-tone | `#0D1C26` | `surface_alt` `#111E29` |
| lit edge of the cape | `#264354` | `border_strong` |
| moonlit rim of the cowl | — | `accent` `#8FC0D6` |
| moon core | `#C6E3EB` | `accent_hover`, `focus` |

| checked | result |
|---|---|
| wallpaper in light theme | `wallpaper()` returns a null pixmap; light sheet contains no `rgba(`, no wallpaper layer |
| wallpaper in dark theme | 1360×900 and 1920×1009 both cover-fitted, non-null |
| second call for the same size | returns the cached pixmap (identity check) |
| colour cast | rendered pixmap and the on-screen capture agree exactly, pixel for pixel, at four sample points |
| dialogs | opaque — verified on the settings dialog and the deck panel |
| dark screens walked | dashboard, drop, action menu, review, settings, deck panel, lock (both modes), bulk entry |
| light screens re-walked | unchanged apart from the export buttons, below |
| suites | login 20/20, bulk-entry dry run, table regression, corpus 0 verbatim mismatches |

Painted in `MainWindow.paintEvent` rather than set as a QSS `background-image`,
because Qt **tiles** a background image instead of cover-fitting it: on any window
that is not the picture's aspect ratio, the tiled version repeats the moon down
the side of the screen.

The panels use integer-alpha `rgba()` (224–244 of 255). That is the one form Qt's
stylesheet parser accepts — a fractional alpha silently becomes fully transparent,
which is why the module has a standing warning about it. Anything holding editable
text sits at 242+; the first attempt at 214 let a cape edge run behind a line of
option text, which was visible in the capture and fixed before it shipped.

**Two defects found while looking at the captures.**

1. The review table's export buttons were pinned to the light theme's Cerebellum
   cyan by a hard-coded stylesheet in `review.py`, so they stayed cyan on the
   Batman palette. They now take the shared `primary` variant. That module still
   owns its *signal* colours (amber rows, red mismatch banner) — those are
   meaning, not decoration, and are deliberately theme-independent.
2. The lock screen kept a 55 px hole under its subtitle after switching from
   create mode to unlock mode. A word-wrapped `QLabel` keeps the height it was
   given, so the two-line "Set a password…" allocation survived the change to the
   one-line "Enter your password…". Measured at 72 px for text needing 17 px;
   fixed by invalidating and re-activating the card's layout, verified back at
   17 px. This affected the light theme too — it was reachable any time **Lock**
   was pressed.

## 6n. Chaotic source files: 18 real September files, before and after

The user reported that "the source file is very chaotic and un-uniform, which
increases the percentage of mistakes". Every `.docx` under `D:\BatComputer\Subject`
plus the attached `E AND D NEET 2026 SEPTEMBER.docx` was parsed with the
rule-based parser (`--no-ai`) before and after the changes.
Reproduce with `python tests/test_rules_chaotic.py` (reduced cases) and the
corpus script (`tools/corpus.py` in the session scratchpad; it is the CLI with
`--no-export --json`).

| file | before: questions / flagged | after: questions / flagged | answers stated |
|---|---|---|---|
| `ANAT SEPT 11TH FMGE GT.docx` | 17 / 0 | **17** / 0 | 17/17 |
| `Anesthesia Mock GT 11th September 2026.docx` | 5 / 0 | **5** / 0 | 5/5 |
| `Anesthesia Mock GT 25th September 2026.docx` | 5 / 0 | **5** / 0 | 5/5 |
| `BIOCHEM_SEPT_11th_FMGE_GT_CORRECTED.docx` | 17 / 1 | **17** / 0 | 17/17 |
| `E AND D NEET 2026 SEPTEMBER.docx` | 3 / 1 | **23** / 2 | 23/23 |
| `ENT SEPT 11TH FMGE GT FINAL - SHUFFLED AND ARRANGED.docx` | 15 / 0 | **16** / 0 | 16/16 |
| `FINAL PEDIA SEPT 11H FMGE GT.docx` | 16 / 0 | **16** / 0 | 16/16 |
| `FINAL PEDIA SEPT 29TH NEETPG FINAL FILE.docx` | 10 / 1 | **10** / 1 | 10/10 |
| `OBGY September GT 11th edited.docx` | 32 / 0 | **33** / 0 | 33/33 |
| `OBGY September GT 11th edited_formatted.docx` | 30 / 1 | **30** / 0 | 30/30 |
| `OPHTHAL SEPT 11TH FMGE GT (1).docx` | 4 / 1 | **15** / 0 | 15/15 |
| `OPHTHAL SEPT 25TH FMGE MOCK FINAL.docx` | 14 / 1 | **14** / 0 | 14/14 |
| `OPHTHAL SEPT 28TH INI MOCK FINAL.docx` | 5 / 1 | **5** / 0 | 5/5 |
| `PATHO SEPT 11TH FMGE GT.docx` | 12 / 1 | **13** / 1 | 12/13 |
| `PHYSIO FMGE GT SEPT 25TH FINAL.docx` | 17 / 0 | **17** / 0 | 17/17 |
| `PHYSIO SEPT 28TH NEETPG GT FINAL.docx` | 9 / 0 | **9** / 0 | 9/9 |
| `Sept anesthesia questions.docx` | 5 / 0 | **10** / 0 | 10/10 |
| `September Dermat MOCK GT MCQs.docx` | 10 / 1 | **10** / 1 | 10/10 |
| **total** | 226 | **265** / 5 | 264/265 |

**Verbatim mismatches: 0** on all 265 questions, before and after. Every
question is still a provable slice of its source.

The five remaining flags are all honest: three questions whose options carried
no letters in the source (labelled A–D in printed order, `LOW_CONFIDENCE` so a
human looks), one question whose source states no answer (`PATHO` Q2), one
duplicate.

### What was wrong, by file

| file | failure | cause | fix |
|---|---|---|---|
| `E AND D NEET 2026` | 3 of 23 found; 20 questions swallowed into Q1–Q3's explanations | `MCQ 2:` not a header; numbering 1,2,3,4,5,**2**,6…; bare numbers rejected once out of sequence | `mcq` keyword; a bare number followed by answer choices opens a question whatever its number (`_Scanner._structure_follows`) |
| same, Q16–Q17 | options lost into the stem, answer key unusable | options printed with no letters at all | `_recover_unlabelled_options`: the short unnumbered lines above the answer line become A–D, flagged |
| same, Q1 | stem began "MCQ 1: Match the Following" | header nested in a header ("1.MCQ 1: …") | `strip_question_prefix` strips an explicit inner header once |
| `OPHTHAL SEPT 11TH` | 4 of 15 found; **11 questions lost** | an explanation re-listed statements "1. … 4. A history of amaurosis fugax" and the "4." arrived exactly when Q4 was expected → phantom Q4, real Q4 then rejected as a duplicate number, everything after it swallowed | inside an explanation, numbering alone never opens a question; answer choices must follow |
| `Sept anesthesia questions` | 5 of 10 found; **Q5's answer key silently overwritten** (B → D) | second section restarted at "1."; later "Correct Answer" lines of the swallowed questions overwrote Q5's | restart handled by structure; `_absorb_answer_body` never overwrites an existing key; the section title "FMGE MOCK 2 / …" is detached from Q5 |
| `OBGY September GT 11th edited` | Q1 missing (the source printed no number for it); Q3 had **five options A, B, C, D, D** | "Choices:" not an options header; "Correct Choice:" not an answer keyword, so the answer line became option D again | `_open_unnumbered` opens a question on a `Choices:` header (stem = the paragraph above); `ANSWER_RE` knows "Correct Choice" / "CorrectChoice" / "Right answer" |
| `ENT SEPT 11TH` | Q1 missing | no number, no options header — just a stem, a blank line, the options and "Correct Answer" | an A–D run that ends in an answer line opens an unnumbered question at the top of a file |
| `PATHO SEPT 11TH` | Q11 missing; its picture attached to Q10 | the stem sat in a 1×2 table beside its picture, filed as a data grid | `docx_reader._is_layout_table`: a multi-column table with text in at most one cell per row is flattened; its pictures anchor to that text |
| `PHYSIO` ×2 | every option C ended in "✅" | the tick marked the correct option | `strip_answer_marker`; the tick becomes the answer when no answer line exists; `reconcile` accepts the marker-less form |
| `BIOCHEM` Q6, `ENT` ×3, `OBGY` ×2, `PEDIA`, `PHYSIO` | false `REFERENCES_MISSING_IMAGE` | "marked sensitivity", "a lateral neck X-ray shows…", numeric options "3 / 6 / 10 / 12" | `IMAGE_REFERENCE_RE` narrowed to caption phrasing; letter-only (not numeric) options imply a picture |
| `Anesthesia`, `BIOCHEM` Q15, `PEDIA` Q14 | false `TRUNCATED` | "Size A", "4 → A", "with shoes on" | trailing capital "A" is a label; prepositions dropped from the dangling-word list |

### The one that must not regress

`tests/test_rules_chaotic.py::test_numbered_subitems_are_not_questions` is the
contract's own trap — "3. What is the order of structures pierced…" followed
by its "1.–6." list — kept as **one** question with `NUMBERED_SUBITEMS`, even
though its answer choices follow the list (the sub-list rule: a number one
higher than the candidate's own, directly after it, means a list; once inside
a sub-list a later "4." is that list's item, not a sibling). 17 of 17 cases
pass.

## 6o. The Word preview & edit screen

`app/ui/word_preview.py` is exercised two ways, because PySide6 could not be
installed in the verification sandbox (PyPI is not reachable from it):

* the pure parts — `app/ui/word_doc_model.py` (edit semantics, ordering,
  inclusion, numbering, `needs_rebuild`) and `app/export/preview_html.py` —
  by `tests/test_word_preview_model.py`, 8 of 8 passing;
* the screen and `main.py`'s wiring on a permissive stand-in for PySide6:
  pipeline → preview (33 questions) → exclude two, move one, edit a stem, tick
  an answer, add and remove options → **Export Word** wrote 31 boxes numbered
  1..31 containing the edited stem, and a PowerPoint export from the review
  table still worked afterwards. The stand-in proves the logic and the
  attribute names, not the pixels; **the screen must be opened once on the
  Windows machine** (`python main.py`, Extract & Format on any file) to confirm
  the layout.

The preview HTML was rendered in a browser next to the LibreOffice rendering of
the exported `.docx` of the same file: same five rows, same bold, same grids
(blue header band), same option alignment, same keyword headings. What the
screen shows is what the file contains.

`needs_rebuild()` — an untouched clean box file reports *unchanged from the
source* and is restyled in place; unticking the explanation, excluding a
question, editing anything, or a file whose own numbering had to be corrected,
all switch to the box writer. Verified in both test layers.

## 7. Live AI path — tested, with a real key

`--action answers` on `flat_clean_noans.docx` (ENT, 6 questions, no answer key),
`gemini-3.1-flash-lite`, **12.53 s**, reconcile reported *6 agreed by both
parsers, 0 disagreements*.

| Q | outcome | detail |
|---|---|---|
| 1 | **refused** | "muscles **marked**" → image not in file; `origin=none`, flagged |
| 2 | B @ 0.95 | antrochoanal polyp does not cause bony invasion |
| 3 | A @ 0.85 | oral steroids, first line for sudden SNHL |
| 4 | A @ 0.95 | Rhizopus — HIV+, black eschar |
| 5 | D @ 0.95 | voriconazole inactive against Mucorales |
| 6 | **refused** | "muscles is **marked**" → image not in file |

All six `verbatim_ok=True`. The four medical answers are correct. The two refusals
are the designed behaviour: a question that depends on an image the file does not
contain gets no invented answer.

Extraction measured separately against the same design:

| file | questions | options | answers | mismatches | time |
|---|---|---|---|---|---|
| `box_5row_Qdot.docx` (539 lines) | 60 | 4 × 60 | 60/60 | 0 | 22.6 s |
| `flat_Qprefix_noans.docx` (87 lines) | 13 | 4 × 13 | 0/13 | 0 | 7.7 s |

## 8. GUI

Launched on the real display; Home → Actions → Review → Settings walked, with
screenshots reviewed by eye. No exceptions, no Qt warnings, no problems reported.
Confirmed: the API-key banner stays hidden when a key is configured, the Merge
card is disabled with 1 file and enabled with 2, the review table shows 6 rows
for 6 questions, and the footer names the active model.

## 9. Hygiene

- `python -m compileall -q app main.py` → clean
- no `print()` anywhere in `app/`
- no `TODO`, `FIXME` or `NotImplementedError`
- all four CLI actions (`extract`, `answers`, `pptx`, `merge`) exit 0 and write
  real, openable files; merge renumbered 6 + 13 → 19

---

## What is NOT verified

Stated plainly, because these gaps are real:

1. **Only Gemini is live-tested.** The OpenAI, Anthropic and Groq providers are
   written to their documented APIs but have never been called — no keys for
   them. Expect to fix something on first use.
2. **`.pdf`, `.txt` and `.pptx` ingestion is untested against real files.** All
   29 corpus files are `.docx`. The readers are implemented and import cleanly;
   they have not met a real scanned PDF or a real deck. Scanned/image-only PDFs
   are explicitly *not* supported — there is no OCR, and such pages produce a
   warning rather than text.
3. **AI extraction was live-tested on 3 documents, not all 29.** The full-corpus
   numbers in §1 and §2 are the rule-based parser. The AI path was verified on
   `box_5row_Qdot`, `flat_Qprefix_noans` and `flat_clean_noans`.
4. **Concurrency is not load-tested.** Chunked parallel extraction runs at
   `max_concurrency=4`; a 3000-question merge has not been attempted, and the
   free tier will rate-limit long before that. `503 UNAVAILABLE "high demand"`
   was observed repeatedly on `gemini-3.5-flash` during testing, which is why
   retry-with-backoff and the rules fallback exist.
5. **Answer *correctness* is spot-checked, not audited.** Four AI answers were
   verified against medical knowledge. Nobody has checked hundreds. Every
   AI-derived answer is flagged `AI` in the review table and marked
   "AI-derived — please verify" on the slide for exactly this reason.
6. **The wallpaper is checked at two window sizes, not every size.** Cover-fit is
   verified at 1360×900 and 1920×1009, plus the cache and the light-theme
   no-op. A very tall or very narrow window will crop the composition harder —
   that is what cover-fit does, and the palette still holds — but no one has
   dragged the window through every shape looking for a label that lands
   somewhere awkward on the picture.
7. **Bold preservation is verified by counting runs, not by eye in Word.** The
   output was read back with python-docx and 1505 bold runs were confirmed at the
   expected offsets, and four question boxes were printed run-by-run and checked.
   Nobody has opened all 65 boxes in Word and compared them side by side with the
   source. Only **bold** is carried over — italic, underline, colour, superscript
   and highlighting are not.
