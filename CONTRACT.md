# Krishna's Super Bot — module contract

> ## ⚠ LIVE API FINDINGS — 2026-07-25 — these OVERRIDE anything below
>
> The user supplied a real Gemini key and the API was tested live against their own
> documents. Five things were learned. **Apply all of them.**
>
> 1. **`gemini-2.5-flash` is RETIRED** and returns `404 NOT_FOUND: "no longer available
>    to new users"`. Any code defaulting to it is dead on arrival.
>    **The new default model is `gemini-3.1-flash-lite`.**
>    Offer in Settings, in this order: `gemini-3.1-flash-lite` (default),
>    `gemini-3.5-flash`, `gemini-flash-latest`, `gemini-3.6-flash`,
>    `gemini-3-flash-preview`. Do **not** list any `2.5` model.
> 2. **Measured on the user's real files** (span-anchored extraction, indices only):
>
>    | fixture | questions | options | answers from source | verbatim mismatches | time |
>    |---|---|---|---|---|---|
>    | `box_5row_Qdot.docx` (539 lines) | 60 | 4 × 60 | 60/60 | **0** | 22.6 s |
>    | `flat_Qprefix_noans.docx` (87 lines) | 13 | 4 × 13 | 0/13 | **0** | 7.7 s |
>    | `flat_substems_noans.docx` (19 lines) | 3 | `{0:1, 4:2}` | 0/3 | **0** | 1.6 s |
>
>    The design is validated: **zero verbatim mismatches** on every real document, and
>    the numbered-sub-item trap is handled (3 questions, not 9 — the `1.`–`6.` list was
>    correctly absorbed into Q3's stem). `flash-lite` is ~4× faster than `3.5-flash`
>    (22.6 s vs 91.9 s on the 60-question file) at identical accuracy, which is why it is
>    the default.
> 3. **`503 UNAVAILABLE "high demand"` happens in normal use** (seen repeatedly on
>    `gemini-3.5-flash`). Retry on 429 **and 503** with backoff — this is not optional,
>    and it is why the rules-parser fallback must work.
> 4. **`gemini-2.5-pro` returns `429 RESOURCE_EXHAUSTED`** on the free tier. Pro models
>    must not be the default and must surface a clear quota message.
> 5. **BUG to guard in `answers.py`:** on a question with **no options**, the model
>    returned `label = "2, 3, 1, 4, 5, 6"` — free text, not a letter. Therefore:
>    - **skip** answer derivation entirely when a question has fewer than 2 options;
>    - **validate** the returned label against that question's actual option labels and
>      reject anything else (leave `origin=NONE`, add `Flag.NO_ANSWER`).
>    Never write an unvalidated model string into `Answer.label`.
>
> Confirmed working as designed: the model **refuses to guess** image-dependent questions
> ("The question refers to an image that was not provided" → `unanswerable=true`), and
> `types.GenerateContentConfig(response_mime_type="application/json",
> response_schema=<plain dict schema>, temperature=0)` is the correct call surface for
> google-genai 1.62.
>
> The user's key is already stored at `%APPDATA%/KrishnasSuperBot/settings.json`.
> Read it from there for live tests. **Never print, log or echo the key.**

**Read `app/models.py` first.** It is authoritative. Every signature below is
fixed: implement it exactly, import only from `app.models` and the stdlib plus
the libraries listed in `requirements.txt`. Do not invent cross-module helpers —
if you need something from a sibling module, it is already listed here.

---

## The one architectural rule

> **The AI never emits question content. It returns line indices.**

`app.ingest.*` flattens any input file into an ordered `list[SourceLine]`. The AI
sees that list rendered as `L0| text`, `L1| text`, … and answers *which lines*
form the stem, which form each option, and which option is correct. All question
text is then produced by `SourceDocument.text_of(indices)` — a verbatim slice.

Consequences you must honour:

- Never send extracted prose back through a model and use the model's output as
  content. Word-for-word preservation is enforced, not requested.
- The **only** field allowed to contain model-written words is
  `Answer.rationale` (shown in the review UI, never exported as question text).
- `Answer` with `origin=AnswerOrigin.AI` is new information — always add
  `Flag.AI_ANSWER`.
- Stripping an option's `"A. "` prefix is done by **deterministic regex on the
  source line**, never by the model.

---

## Layout

```
KrishnasSuperBot/
  main.py                  entry point
  requirements.txt
  CONTRACT.md
  app/
    models.py              DONE — do not edit
    config.py              settings persistence            [agent: core]
    ingest/
      __init__.py          read_any() dispatcher           [agent: ingest]
      docx_reader.py                                       [agent: ingest]
      pdf_reader.py                                        [agent: ingest]
      txt_reader.py                                        [agent: ingest]
      pptx_reader.py                                       [agent: ingest]
    parse/
      rules.py             rule-based parser               [agent: rules]
      providers.py         pluggable LLM providers         [agent: ai]
      ai.py                span-anchored AI parser         [agent: ai]
      answers.py           answer detection / derivation   [agent: ai]
      reconcile.py         verbatim proof + repair         [agent: reconcile]
      merge.py             multi-file merge + dedupe       [agent: reconcile]
      styles.py            source bold -> Question spans   [agent: reconcile]
    export/
      docx_format.py       XML surgery (fonts/spacing)     [agent: word]
      word_box.py          box-format .docx writer         [agent: word]
      verify.py            verbatim verification           [agent: word]
      pptx_deck.py         branded deck builder            [agent: pptx]
      preview_html.py      the box layout as HTML for the on-screen preview (no Qt)
    ui/
      theme.py             QSS + palette + wallpaper       [agent: ui-shell]
      home.py              drag-drop home screen           [agent: ui-shell]
      actions.py           action menu after drop          [agent: ui-shell]
      review.py            review/edit table               [agent: ui-review]
      word_doc_model.py    edit semantics + the Word document model (no Qt)
      word_preview.py      Word preview & edit screen (list / preview / editor)
      settings_dialog.py   API key + branding              [agent: ui-review]
      worker.py            QThread job runner              [agent: ui-shell]
  tests/
    test_rules_chaotic.py        one reduced case per chaotic source shape
    test_word_preview_model.py   document model, edit functions, HTML renderer
```

### Additions since the first contract

- `Question.source_number` -- the number the source printed (0 for none).
  `rules.assign_numbers()` counts questions 1..N in document order and is run
  once per file by `main.parse_file`, so the AI and rules paths agree.
  `WordExportConfig.renumber` (default True) decides which number an export
  prints; the preview screen offers the switch.
- **Every hand edit goes through `app.ui.word_doc_model`** (`edit_stem`,
  `edit_option_text`, `edit_explanation`, `set_answer_label`, `clear_answer`,
  `add_option`, `remove_option`, `move_option`). `review.py` delegates to them;
  no screen may mutate a `Question` any other way.
- `main.run_export(..., rebuild=True)` forces the box writer for a source that
  already has boxes. The preview screen's `needs_rebuild()` is the only caller
  that may pass False, and only when nothing changed.
- `rules.strip_answer_marker` is the sanctioned stripper for a tick on an
  option; `reconcile` and `ai` offer the marker-less form as verbatim.
  `rules.IMAGE_REFERENCE_RE` is the one image-reference pattern; `reconcile`
  and `ai` import it.
- `docx_format.KEYWORD_BASES` gained "Correct Option Explanation", "Other
  Options Explanation" and "Correct Answer Explanation"; `match_keyword` now
  returns the longest matching base; `EXPLANATION_BASES` lists the bases that
  stop the exporter adding an "Explanation:" heading of its own.
- The rule parser decides where a question starts by **structure** (answer
  choices, an options header or an answer line following the stem) weighed
  with numbering, never numbering alone inside an explanation. See the module
  docstring of `_Scanner` in `rules.py` and `tests/test_rules_chaotic.py`.

---

## `app/config.py` — [core]

```python
APP_NAME = "Krishna's Super Bot"
APP_VERSION = "1.0.0"

@dataclass
class Settings:
    api_key: str = ""
    provider: str = "gemini"          # gemini | openai | anthropic | groq | none
    model: str = "gemini-2.5-flash"
    use_ai: bool = True
    max_concurrency: int = 4
    brand: BrandConfig
    word: WordExportConfig
    pptx: PptxExportConfig
    output_dir: str = ""
    recent_files: list[str]

def settings_path() -> Path        # %APPDATA%/KrishnasSuperBot/settings.json
def load_settings() -> Settings    # tolerant of missing/corrupt file
def save_settings(s: Settings) -> None
def resolve_api_key(s: Settings) -> str
    """s.api_key, else env GEMINI_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY /
    ANTHROPIC_API_KEY / GROQ_API_KEY as appropriate. '' if none."""
```

Store the key in the settings JSON with `0o600`-equivalent permissions where the
OS allows; never log it. `load_settings()` must never raise.

---

## `app/ingest/` — [ingest]

```python
# ingest/__init__.py
SUPPORTED_EXTENSIONS: frozenset[str]   # {".docx", ".pdf", ".txt", ".pptx"}

def read_any(path: str | Path) -> SourceDocument
    """Dispatch on suffix. Raise UnsupportedFormat for anything else."""

class UnsupportedFormat(Exception): ...
class ReadError(Exception): ...
```

Each reader returns a `SourceDocument` with:

- `lines` — **document order**, `idx` contiguous from 0. Split paragraphs on
  `\n` (Word soft breaks) so each visual line is one `SourceLine`. Keep blank
  lines: they are structural signal for the parsers. Preserve text verbatim.
- `images` — with `anchor_line` = index of the nearest **preceding** non-blank
  line (or the following one if the image leads the document). Drop exact
  duplicates by `sha1`.
- `tables` — verbatim cell text. For **docx**, a table that looks like a
  question box (single column, ≥3 rows, first row matching a question header)
  must ALSO be flattened into `lines` with `LineKind.CELL` so the parsers can
  read it as text; genuine data grids (≥2 columns) go to `tables` only.
- `ref` — for docx use `"p:<n>"` for body paragraphs and
  `"tbl:<t>/r:<r>/c:<c>/p:<p>"` for cell paragraphs. Needed by the Word export.

Reader specifics:

- **docx_reader** — port the image extraction and nested-table walk from
  `../PPT_FROM_BOX_FILE_WITH_ANSWERS.py` (`_extract_images_from_element`,
  `_docx_table_to_data`). Iterate `doc.element.body` children in true document
  order, not `doc.paragraphs` then `doc.tables`. Deduplicate merged cells by
  `<w:tc>` element identity (see `iter_unique_cells` in
  `../FORMATTING_BOX_FILE.py` — and keep the element, not `id()`).
- **pdf_reader** — PyMuPDF (`import fitz`). Use `page.get_text("dict")` and emit
  one line per text line, sorted top-to-bottom then left-to-right. Extract
  images via `page.get_images`. If a page yields no text, append a warning
  `"page N has no text layer (scanned?) — OCR not available"` and continue; do
  not raise.
- **txt_reader** — decode utf-8 with `errors="replace"`, fall back to cp1252.
- **pptx_reader** — walk shapes in each slide; text frames → paragraphs,
  pictures → images, tables → tables. `page` = 1-based slide number.

---

## `app/parse/rules.py` — [rules]

The deterministic parser: the fast path, the offline mode, and the AI's
cross-check. It must handle every shape found in the real corpus:

| Shape | Example | Source |
|---|---|---|
| Box table, 5 rows | `Q.1` / stem / `A. …⏎B. …` / `Ans. A) …` / dup | `D:\Anthony Sir\Question_1_boxes.docx` |
| Box table, labelled rows | `Question 1` / `Question: …` / `Options: (A) …` / `Answer: (X)` / `Explanation:` | `../PPT_FROM_BOX_FILE.py` |
| Flat, bare number | `1. stem` then `A. opt` … | `D:\Bhanu Sir\ENT Recall Question.docx` |
| Flat, `Q` prefix | `Q1. stem` / `Q.1` / `Question 1.` | `D:\Bhanu Sir\OBG Recall File.docx` |
| Options on one line | `(A) x (B) y (C) z` | `parse_options_line` in `../PPT_FROM_BOX_FILE.py` |
| Bullet options | `- text` (auto-label A, B, C…) | `../PPT_FROM_NORMAL_FILE.py` |

```python
QUESTION_HEADER_RE   # matches: "Question 1.", "Q1.", "Q.1", "Q 1)", "1.", "1)"
OPTION_RE            # "A." "A)" "(A)" "a." — A-H only
INLINE_OPTIONS_RE    # "(A) … (B) …"
ANSWER_RE            # "Ans. B)", "Answer: (B)", "Ans: B", "Correct answer - B"
SECTION_RE           # Explanation | Incorrect Options | Other Options |
                     # Take Home Message | Tags

def parse(doc: SourceDocument) -> ParseResult
def detect_question_starts(doc: SourceDocument) -> list[int]
def strip_option_label(text: str) -> tuple[str | None, str]
    """('A', 'body') or (None, text) — the ONLY sanctioned label stripper."""
def parse_answer_line(text: str) -> tuple[str | None, str]
```

**Two hard requirements, both real failures in the current scripts:**

1. `Q.1` / `Q1.` headers must be detected. `^Question\s+(\d+)` alone silently
   skips an entire 60-question file.
2. A bare `N.` line must NOT start a question when it is a **numbered sub-item
   of the current stem**. Real case (`Anesthesia Recall Question.docx`):

   ```
   3. What is the order of structures pierced during lumbar puncture?
   1. Ligamentum Flavum
   2. Supraspinatus ligament
   …
   6. Arachnoid
   ```

   Six phantom questions today. Rules: a bare `N.` only opens a question when
   `N == expected_next` (or within +2 of it) **and** the current question
   already has ≥2 options, or no question is open. Otherwise treat it as stem
   continuation and set `Flag.NUMBERED_SUBITEMS`. Prefer explicit
   `Question N` / `QN` headers over bare numbers, as
   `PPT_FROM_NORMAL_FILE.parse_questions` does in its two-pass scan.

Also: filter `"Top of Form"` / `"Bottom of Form"` artifacts; treat an
`Options:` header line as a separator, not content; a line that is not a label
match while options are open is a **continuation of the last option**.

Every `Question` you emit must carry correct `stem_lines` / `source_lines`.
Set `parser=Parser.RULES`, and apply the flags in `Flag` that you can determine
locally (`FEW_OPTIONS`, `NON_SEQUENTIAL_LABELS`, `DUPLICATE_OPTIONS`,
`EMPTY_STEM`, `NO_ANSWER`, `REFERENCES_MISSING_IMAGE` when the stem matches
`/\b(image|figure|photograph|shown below|marked|arrow)\b/i` and no image is
attached).

---

## `app/parse/providers.py` + `ai.py` + `answers.py` — [ai]

### providers.py

```python
class ProviderError(Exception): ...
class RateLimited(ProviderError): ...
class NoApiKey(ProviderError): ...

@dataclass
class Provider(ABC):
    api_key: str
    model: str
    def complete_json(self, system: str, user: str, schema: dict,
                      *, images: list[ImageAsset] | None = None,
                      timeout: float = 120.0) -> dict: ...
    @property
    def name(self) -> str: ...

class GeminiProvider(Provider)     # google.genai, response_schema, response_mime_type="application/json"
class OpenAIProvider(Provider)     # HTTP via requests; json_schema response_format
class AnthropicProvider(Provider)  # HTTP via requests; tool-based structured output
class GroqProvider(Provider)       # OpenAI-compatible endpoint

def make_provider(settings) -> Provider | None   # None when no key / provider="none"
```

Gemini is the default (free tier). Use `google.genai`:
`client = genai.Client(api_key=...)`, then
`client.models.generate_content(model=..., contents=[...], config={"response_mime_type": "application/json", "response_schema": schema, "temperature": 0})`.
Default model `gemini-2.5-flash`. Retry on 429/5xx with exponential backoff
(3 attempts, 2s/4s/8s + jitter) and raise `RateLimited` after that. Never log
the key. `temperature=0` always — this is extraction, not creativity.

### ai.py — span-anchored extraction

```python
EXTRACTION_SCHEMA: dict    # JSON schema, indices only — no content strings
                           # except answer.rationale

def parse(doc, provider, *, settings=None,
          progress: Callable[[int, int], None] | None = None) -> ParseResult

def build_chunks(doc: SourceDocument, *, max_lines: int = 400,
                 overlap: int = 12) -> list[tuple[int, int]]
    """Split long docs on blank-line/question-header boundaries so a question is
    never cut in half. Overlap lets a straddling question be recovered; dedupe
    by first stem line."""

def spans_to_questions(doc, payload: dict, offset: int) -> list[Question]
```

The model returns, per question:

```json
{"questions": [{
  "number": 3,
  "stem_lines": [12, 13],
  "options": [{"label": "A", "lines": [15]}, {"label": "B", "lines": [16]}],
  "answer": {"label": "B", "stated_in_source": true, "source_lines": [20],
             "confidence": 0.95, "rationale": ""},
  "explanation_lines": [22, 23],
  "notes": "stem contains a numbered sub-list"
}]}
```

`spans_to_questions` builds text **only** via `doc.text_of(...)` and strips
option label prefixes with `rules.strip_option_label`. Run chunks concurrently
(`ThreadPoolExecutor`, `settings.max_concurrency`, default 4) — that is where
the speed comes from. Set `parser=Parser.AI`. On any `ProviderError`, fall back
to `rules.parse` for that chunk and record a warning; the app must never
hard-fail because the API is down.

### answers.py

```python
def detect_answers(questions, doc, provider, *, settings=None,
                   progress=None) -> list[Question]
```

For questions where `answer.origin is AnswerOrigin.NONE`: batch them
(~10 per call), send stem + options **as extracted text** (this is a reasoning
task, so text is correct here), and ask for the correct label + confidence +
one-sentence rationale. Set `origin=AnswerOrigin.AI`, add `Flag.AI_ANSWER`, and
`Answer.text = question.option(label).text` — copied, never rewritten.

Refuse to guess when honesty demands it: if the question carries
`Flag.REFERENCES_MISSING_IMAGE`, or the model reports confidence < 0.5, leave
`origin=NONE`, add `Flag.NO_ANSWER` / `Flag.LOW_CONFIDENCE`. A wrong answer key
is worse than a blank one. When the question *does* have images, pass them to
the provider so a genuine visual question can be answered.

---

## `app/parse/reconcile.py` + `merge.py` — [reconcile]

```python
# reconcile.py
def verify_question(q: Question, doc: SourceDocument) -> tuple[bool, str]
    """Re-derive stem/options from source line indices and compare.
    Equal after whitespace collapsing (and after removing the option label
    prefix) -> (True, ''). Otherwise (False, first-divergence detail)."""

def repair(q: Question, doc: SourceDocument) -> Question
    """Snap text back to the verbatim source slice; re-derive labels; recompute
    local flags. Idempotent."""

def reconcile(ai: ParseResult, rules: ParseResult,
              doc: SourceDocument) -> ParseResult
    """Merge the two parses. AI wins on structure; rules fill in questions the
    AI dropped and supply an answer the AI missed. Where they disagree on
    option count or on a source-stated answer label, lower `confidence` and set
    Flag.LOW_CONFIDENCE — a disagreement is exactly what a human should see.
    Every returned question passes through `repair`, so
    `verbatim_ok` is always accurate. parser=Parser.AI_RULES."""

# merge.py
def merge(results: list[ParseResult], *, renumber: bool = True,
          drop_duplicates: bool = True,
          similarity: float = 0.92) -> ParseResult
    """Concatenate in input order, optionally renumber 1..N. Exact duplicates by
    Question.dedupe_key(); near-duplicates by difflib.SequenceMatcher on the
    normalised stem >= `similarity`. Keep the copy with the better answer
    provenance (SOURCE > AI > NONE) and flag it MERGED_DUPLICATE; never drop
    silently — return the count in ParseResult.warnings."""

# styles.py
def annotate_bold(questions, doc: SourceDocument) -> int
    """Fill Question.stem_bold / Option.bold / Question.explanation_bold from
    SourceLine.bold. Runs ONCE, after parsing, from main.parse_file — so no
    parser has to think about formatting and a question that has been through
    any combination of rules/ai/reconcile still ends up styled.

    Each stored line is located inside its source line by search, never by a
    computed offset: the stem has had its question prefix stripped, an option its
    label, and the rules parser calls .strip() on the join. A line that cannot be
    found contributes nothing — wrong bold mid-word is worse than none."""
```

---

## `app/export/` — [word] and [pptx]

### docx_format.py + word_box.py + verify.py — [word]

Port from `../FORMATTING_BOX_FILE.py`, which is already correct and battle-tested.
**Keep its XML approach**: schema-ordered element insertion
(`get_or_add_ordered` with `RPR_ORDER` / `TCPR_ORDER` / `TBLPR_ORDER`),
`set_font_on_rpr`, `set_spacing_zero`, `set_document_defaults`,
`color_nested_table`, and the `run_text` / `is_blank_p` / `split_simple_run`
helpers. Do not rewrite these from scratch — they handle real Word edge cases.

```python
# docx_format.py
def apply_house_style(doc, cfg: WordExportConfig) -> None
    """Calibri 14 + zero spacing everywhere, incl. paragraph marks and
    docDefaults."""
def match_keyword(text) -> str | None
    """CASE-INSENSITIVE, and SEPARATOR-INSENSITIVE. Sources write "EXPLANATION:"
    and "Other options:" as freely as the title-case form; treating those as
    different headings is what made the exporter add a second "Explanation:"
    above the source's own. Any run of spaces/tabs/hyphens matches the single
    space in the canonical base, so "Take-Home Message" is "Take Home Message" —
    a real file spelled it with a hyphen and the block lost its formatting and,
    in the AI path, its place in the explanation. The canonical spelling is
    returned for comparison only — never written back."""
def apply_keyword_rules(cell) -> None
    """For Explanation:, Incorrect Options:, Other Options:, Take Home Message:
    and Educational Objective: — split an inline keyword onto its own line, bold
    the label, and ENSURE a blank line above and below it (skipped when one is
    already there, which is what keeps it idempotent). Rules 3-5 of
    ../format_docs.py, which is the house style of record."""
def bold_prefix(pel, length) -> None
    """Bold the first `length` characters of a paragraph, splitting a run at the
    boundary when it can."""
def label_incorrect_options(cell) -> None
    """Bold 'Option ' before A./B./C. inside the Incorrect/Other Options block
    only — never the answer choices under the stem. The source's own letter is
    bolded as well, so the label reads **Option A.**

    ALSO a blank line above every entry (rule 6 of ../format_docs.py). The blank
    BELOW the last one is not added here — it falls out of the blank
    apply_keyword_rules puts above the heading that closes the block.

    An entry already reading "Option A. …" (an earlier run, or a source that
    writes it that way) counts as an entry, so the blank lines are stable
    across runs instead of appearing and vanishing."""
def color_nested_tables(doc, cfg) -> None

# word_box.py
def has_question_boxes(path: str) -> bool
    """True when the .docx is ALREADY one table per question, using the reader's
    own definition (single column, >=3 rows, first cell a question header).

    THE EXTRACT ACTION MUST NOT REBUILD SUCH A DOCUMENT. Restyle it in place via
    reformat_existing — that is format_docs.py's own mode and the house style of
    record. Rebuilding drops every row the box model has no slot for (the
    trailing "Tags: #..." row), and it puts a grid and a page of prose in the
    same table row, which Word cannot split across pages and silently CLIPS.
    write_box_docx is for a FLAT document, which has no rows to keep."""

def write_box_docx(questions: list[Question], out_path: str,
                   cfg: WordExportConfig, brand: BrandConfig) -> str
    """One bordered single-column table per question. Nothing outside the boxes:
       cfg.title_block is False, so there is no subject/faculty/date/logo page.
       Every border of every row is black (cfg.border_color = "000000").
         Question <n>              (white fill, black bold — NOT a blue band)
         <stem>                    (+ images, + the grid between stem & options)
         A. …  B. …  C. …  D. …    (one option per paragraph, aligned)
         Ans. <L>) <text>          (bold; + marker when AI-derived)
         Ans. <L>) <text>          (the same line again, bold, then:)
         Explanation: …            (only when the source has no heading of its own)
       FIVE ROWS, ALWAYS. Row 5 is written whether or not the question has an
       explanation — it is part of the box's shape, not something the
       explanation brings with it. Only a config with the answer AND the
       explanation both off drops it, because it would then be blank.
                                   (+ the images and grids anchored inside it,
                                    each after the line it was anchored to)
       Uniform: identical row structure, spacing and option alignment for every
       question. Then run apply_house_style + color_nested_tables.

       A picture or grid goes in the row it came from, decided by anchor_line
       against the explanation's own line span. Everything else — including an
       unanchored asset (anchor_line == -1) — stays with the stem, so a missing
       anchor can never lose one. Dumping them all into the stem row is what
       used to lift every explanation figure out of its explanation.

       EVERY EMBEDDED GRID GETS A ROW OF ITS OWN. Word cannot break a table row
       across pages when the row holds a nested table — it lays out what fits
       and silently CLIPS the rest. A grid sharing a row with a page of prose
       therefore loses everything after it, usually the whole Incorrect Options
       block. So no row is ever both tall and unsplittable: the grid rows are
       short, the prose rows have no nested table in them. fmt.weld_rows then
       erases the rules between the pieces (cell-level w:tcBorders val="nil",
       which beats the table's insideH) so the reader still sees one box.

       python-docx rejects images by sniffing literal signature bytes, so an
       Adobe JPEG (opens on APP14, not APP0/APP1) and a WMF are dropped even
       though Word embeds them. _repair_image grafts an 18-byte JFIF header on
       the former (LOSSLESS — never re-encode a radiograph) and rasterises the
       latter through Pillow.

       Source bold is reproduced from Question.stem_bold / Option.bold /
       Question.explanation_bold (see app/parse/styles.py) when
       cfg.preserve_bold."""

def reformat_existing(src: str, dst: str, cfg) -> tuple[str, bool, str]
    """The original script's pass-through mode: restyle a docx in place-ish
    without re-parsing. Returns (dst, verified_ok, detail)."""

# verify.py
def concat_text(doc, skip_runs=frozenset()) -> str
def inserted_label_runs(doc) -> set        # the 'Option ' runs we added
def verify_verbatim(src_path: str, dst_path: str) -> tuple[bool, str]
    """Generalised from the original verify_verbatim: character-exact, or equal
    after whitespace collapsing, ignoring runs we deliberately inserted."""
def verify_questions_against_source(questions, doc) -> list[tuple[Question, str]]
    """Return the questions whose text is not a verbatim source slice."""
```

### pptx_deck.py — [pptx]

Merge the three PPT builders into one. Keep `_estimate_text_height_cm`,
`_compute_font_sizes` (the auto-shrink loop), `_add_table_to_slide`, and the
image stacking/side-placement logic from
`../PPT_FROM_BOX_FILE_WITH_ANSWERS.py` — use the tighter `0.045` line-height
coefficient from the box variants, not `0.055`.

```python
def build_deck(questions, out_path, cfg: PptxExportConfig,
               brand: BrandConfig) -> str
    """Title slide (logo, subject, blue rule, faculty/date) → per question a
    slide (bordered red stem box, blue rule, black options, images beside
    options, nested tables as real PPTX tables) → optional answer slide or
    in-slide banner per cfg.answer_on_separate_slide → thank-you slide."""
```

Mark an AI-derived answer visibly on the answer slide (e.g. a grey
`AI-derived — please verify` line beneath it). Never write an answer slide for
`AnswerOrigin.NONE`; show `Answer not determined` instead.

---

## `app/ui/` — PySide6 — [ui-shell] and [ui-review]

Hard rule: **all document work happens off the GUI thread.** The UI must never
block; every long job goes through `worker.py`.

```python
# worker.py
class Job(QObject):                       # QRunnable-style payload
    progress = Signal(int, int, str)      # done, total, message
    finished = Signal(object)             # result payload
    failed = Signal(str)
def run_job(fn, *args, on_progress=None, on_done=None, on_error=None) -> QThread

# theme.py
def stylesheet(dark: bool = True) -> str
COLORS: dict            # brand: deep blue #141E52, accent #1AB7EA, red #E23A3A
TINTS: dict             # one cool, low-saturation hue per feature icon square;
                        # never used for text, borders or whole cards, and never
                        # warm -- amber and red stay semantic
def tile_colors(name, dark=False, surface=None) -> tuple[str, str]
def apply_theme(app, dark: bool = True) -> None

# sidebar.py
class Sidebar(QWidget)      # the left nav rail: logo, name, tagline, the four
                            # destinations, the quote in the bottom-left corner,
                            # version. Signal: navigate(str)
                            #   # "home"|"history"|"settings"|"help"
                            # MainWindow hides it on the lock screen and owns
                            # which entry is lit (set_active).

# dashboard.py
class DashboardScreen(QWidget)  # greeting, six feature tiles, and a status strip
                            # along the bottom saying what will actually run.
                            # Signal: featureChosen(str)
                            #   # the four actions, plus "bulk"|"settings"|"lock"

# history.py
class HistoryScreen(QWidget)  # Settings.recent_files as rows; a missing file is
                            # shown greyed, not dropped. Picking one loads it
                            # into the drop screen and never starts a job.
                            # Signals: filesChosen(list[str]), cleared()

# home.py
class DropZone(QFrame)      # accepts dragEnterEvent/dropEvent, highlights on hover,
                            # filters to SUPPORTED_EXTENSIONS, also click-to-browse
class HomeScreen(QWidget)   # title "Krishna's Super Bot", drop zone, file chips
                            # with remove buttons, brand fields (subject/faculty/
                            # date), settings gear. Signal: filesReady(list[str])

# actions.py
class ActionMenu(QWidget)   # shown after files are dropped; four large cards:
                            #   Extract & Format MCQs
                            #   Detect Correct Answer
                            #   Build PowerPoint Deck
                            #   Merge Multiple Files   (disabled for < 2 files)
                            # each: icon, title, one-line description.
                            # Signal: actionChosen(str)  # "extract"|"answers"|"pptx"|"merge"

# review.py
class ReviewTable(QWidget)  # QTableView over the parsed questions:
                            #   #, Stem (elided), Options, Answer, Origin,
                            #   Confidence, Flags, Source
                            # - rows needing attention tinted amber; verbatim
                            #   mismatch tinted red
                            # - inline edit of stem/options/answer (sets
                            #   Parser.MANUAL, clears the flag it fixes)
                            # - detail pane below: full stem, options, answer,
                            #   rationale, thumbnails of attached images
                            # - filter: All / Needs attention / AI answers
                            # - footer: "N questions · M need attention"
                            # - Export buttons: Word, PowerPoint
                            # Signal: exportRequested(str)  # "word"|"pptx"

# settings_dialog.py
class SettingsDialog(QDialog)  # provider combo, API key (masked, with
                               # "Test connection"), model, use_ai toggle,
                               # concurrency, fonts/sizes, logo picker,
                               # output folder. Links to
                               # https://aistudio.google.com/apikey for the
                               # free Gemini key.
```

`main.py`:

```python
def main() -> int
    """QApplication, apply theme, Sidebar beside a QStackedWidget of
    LoginScreen → DashboardScreen → HomeScreen → ActionMenu → ReviewTable →
    BulkEntryScreen → WordPreviewScreen → HistoryScreen, wire signals, window
    titled "Krishna's Super Bot", 1280x860 min, remember geometry. Also support a
    headless CLI:  python main.py --cli <files...> --action extract --out DIR
    so the pipeline is testable without a display."""
```

The lock screen must be reachable only forwards: while it is showing, the nav
rail is **hidden** and the Back / Home / Lock / Settings `QAction`s are
**disabled**. Hiding the rail is not enough on its own — the actions hang off the
window, so their shortcuts (`Ctrl+,`, `Alt+←`) stay live unless they are switched
off explicitly.

If no API key is configured, the app must still work end-to-end using
`rules.parse` — show a non-blocking amber banner: *"Running without AI. Add a
free Gemini key in Settings for best accuracy."* Never show a modal on launch.

---

## Style rules

- Python 3.12+, `from __future__ import annotations`, full type hints.
- 4-space indent, ~88 col soft limit, double quotes.
- Comments explain *why*, not *what*. Match the density of the original scripts:
  a short header docstring per module, section banners for long files.
- No `print()` in `app/` — use `logging.getLogger(__name__)`. `main.py` and the
  CLI path may print.
- Catch narrow exceptions. A per-question failure must never abort a whole run:
  log it, flag the question, carry on.
- No TODOs, stubs, `pass`-bodies or placeholder returns. Ship working code.
