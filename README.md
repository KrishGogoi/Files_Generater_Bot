# Krishna's Super Bot

A Windows desktop app (PySide6) that takes documents containing multiple-choice
questions — `.docx`, `.pdf`, `.txt`, `.pptx` — and produces uniformly formatted
Word documents and branded PowerPoint decks.

It reads whatever layout the file happens to use (box tables, `1.` lists,
`Q.1` headers, options on one line, bullet options) and rewrites only the
*formatting*. The question wording is never rephrased, summarised or
regenerated.

---

## Install

```
pip install -r requirements.txt
```

Python 3.12+ (tested on 3.14). Everything else — python-docx, python-pptx,
PyMuPDF, Pillow, google-genai, PySide6 — comes from `requirements.txt`.

Run it:

```
python main.py
```

---

## Free Gemini API key

The AI pass uses Google's free tier.

1. Open <https://aistudio.google.com/apikey>.
2. Sign in with a Google account and click **Create API key**.
3. Copy the key.
4. In the app: **Settings** (nav rail on the left, or `Ctrl+,`) → paste it into **API key** →
   keep provider `gemini`, model `gemini-3.1-flash-lite` → **Test connection** → OK.

> **Model choice.** `gemini-3.1-flash-lite` is the default because it was the
> fastest model that scored full marks on real extraction — about 4× quicker
> than `gemini-3.5-flash` (23 s vs 92 s on a 60-question file) with identical
> accuracy. Do **not** set a `gemini-2.5-*` model: that family returns
> `404 "no longer available to new users"` for keys issued after mid-2026.
> Free-tier `*-pro` models return `429 RESOURCE_EXHAUSTED`, so avoid those too.

The key is stored in `%APPDATA%\KrishnasSuperBot\settings.json` and is never
logged or written into any report.

Prefer environment variables? Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and
leave the field blank. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GROQ_API_KEY`
work the same way if you switch provider.

**No key is required.** Without one the app runs the rule-based parser and shows
an amber banner. It never blocks you with a dialog on launch.

---

## The app password

The app opens on a lock screen. The first time you run it, it asks you to choose
a password; after that it asks for it each time it opens, and **Lock** (or
`Ctrl+L`) returns you to it. Change or remove it under
**Settings → Branding & output → App password**.

The password is stored as a salted **scrypt** hash in
`%APPDATA%\KrishnasSuperBot\auth.json`, never in plain text and never in a log.
Three wrong attempts trigger a short, escalating wait.

> **Be clear about what this protects.** It stops someone sitting at this PC from
> opening the app and using your API key or your question banks. It is **not**
> encryption: your `.docx` files are untouched on disk, the command line is not
> gated, and anyone with file access can delete `auth.json` to clear the
> password. Treat it as a lock on the front door, not a safe.
>
> That also means you cannot be locked out: if you forget the password, delete
> `auth.json` and the app will ask you to set a new one.

---

## The two logos

They are separate on purpose and never share a setting:

| Logo | Where it appears | Where it lives |
|---|---|---|
| **App logo** | Lock screen, dashboard header, window and taskbar icon | `app/ui/assets/app_logo.jpg` — built in, no setting |
| **Academy logo** | PowerPoint **title and thank-you** slides | `app/ui/assets/Cerebellum_Logo.png` — overridable in **Settings → Branding & output → Logo** (`BrandConfig.logo_path`) |
| **Wallpaper** | Behind the window, dark theme only | `app/ui/assets/wallpaper_dark.jpg` — built in, no setting |

All three now sit in `app/ui/assets`. `app_logo.jpg` and `Cerebellum_Logo.png`
are **different logos with different jobs** — same folder, so read the filename.

The Word export carries no logo at all — it opens on question 1.

The Academy logo is found **relative to `app/config.py`**, so nothing stores an
absolute path to it and renaming or moving the project cannot break it. If your
saved setting still points at an older location, it is re-pointed on the next
start and logged. A logo of *your own* that has gone missing is left alone and
warned about instead, since it is not ours to replace.

To change the app logo, replace `app/ui/assets/app_logo.jpg` (any size; it is
centre-cropped for the square badge and the icon, and scaled to fit on the lock
screen). If the file is missing the lock screen falls back to a padlock glyph and
the dashboard simply shows no badge — nothing breaks.

## The window

A nav rail runs down the left edge on every screen except the lock screen, where
it is hidden so nothing on it can be reached from behind the password:

| Rail | Goes to |
|---|---|
| **Home** | the dashboard of feature tiles |
| **History** | the files you worked on recently, from `recent_files` |
| **Settings** | the settings dialog (`Ctrl+,`) |
| **Help** | a short summary of the tiles, the shortcuts and the file types |

`Alt+←` still steps back and `Ctrl+L` still locks, as they did when these were
toolbar buttons.

## The home page

After unlocking you land on a dashboard with every feature on it:

| Tile | Goes to |
|---|---|
| Extract & Format MCQs | the drop zone, with this action already chosen |
| Detect Correct Answer | the drop zone |
| Build PowerPoint Deck | the drop zone (then the slide-contents panel) |
| Merge Multiple Files | the drop zone |
| **Bulk Entry Automation** | its own screen — no files needed |
| Settings | the settings dialog |

Choosing a document feature carries that choice through, so dropping files runs it
straight away. **Back** (or **Home** on the rail) returns you to the tiles to pick
a different one.

Along the bottom of the dashboard a status strip says what will actually run —
which model, or that the run is rule-based only — and offers **Open Settings**
when there is something to fix.

Picking a row in **History** loads that file into the drop zone; it does not
re-run anything on its own. A file that has been moved or deleted since is shown
greyed rather than quietly dropped from the list.

---

## Bulk Entry Automation

Ported from `SPLIT_SCREEN_AUTOMATION.py`, with the spreadsheet half removed.

The original clicked into Excel, copied a cell, pasted it into the backend and
walked down the sheet. Here you paste the values into the app instead, and the run
only ever touches the **backend** coordinates:

```
click the paste field → Ctrl+V → wait for the lookup → click submit
```

That drops the two most fragile parts of the original: it no longer depends on
Excel being open at a particular position, and it can no longer copy the wrong
cell and submit it.

**How to use it**

1. Copy the column of values from your spreadsheet.
2. Click the first row of the grid and press `Ctrl+V` — the whole block lands at
   once. Only the first column of a paste is read; extra columns are ignored, and
   blank rows are skipped, so you do not need all 25.
3. Press **Dry run**. Nothing is clicked; it reports the exact coordinate it
   would use for every box. Do this whenever the backend window may have moved.
4. Press **Run**, confirm, and bring the backend window to the front during the
   5-second countdown.

**Safety**

- **Stop** is polled between every step, so it takes effect within one step.
- pyautogui's failsafe stays on: slam the pointer into a screen corner to abort.
- Coordinates are checked against your actual screen size before the first click,
  and the run refuses to start if any of them fall outside it.
- The coordinates are the original script's, unchanged, and are shown on screen.
  They live in one place — `BulkEntryLayout` in `app/automation/bulk_entry.py` —
  if your window layout ever changes.

---

## The four document actions

Drop files on the drop zone, then pick one:

| Action | What it does |
|---|---|
| **Extract & Format MCQs** | Parses the questions and writes the box-format Word file: one bordered table per question, Calibri 14, zero spacing, aligned options. See below for the exact layout. |
| **Detect Correct Answer** | For questions whose source states no answer key, asks the model for the correct option. Every derived answer is flagged `ai_answer` and marked in the export. |
| **Build PowerPoint Deck** | Title slide, one slide per question (red stem box, blue rule, black options, images beside the options), answer slides, thank-you slide. Opens the contents panel below first. Whatever height the question does not use is split evenly above and below the options, so a short question does not leave a band of empty slide at the foot. |
| **Merge Multiple Files** | Concatenates several files, drops exact and near-duplicate questions, renumbers 1..N. Enabled once two or more files are loaded. |

### The Word box layout

The document is **questions only** — no logo, subject, faculty or date page in
front of the first box. Every question is one table of **five rows**, with black
borders on every row:

| Row | Contents |
|---|---|
| 1 | `Question n` — plain white background, black bold text. |
| 2 | Stem, plus any images and any grid that sits between the stem and the options. |
| 3 | Options: `A.` + tab + text, hanging indent, so the letters line up down the page. |
| 4 | `Ans. B) …`, bold. |
| 5 | The **same answer line again, bold** — then the explanation underneath it, when the question has one. |

**Row 5 is always there**, explanation or not. It is part of the shape of a
question box rather than something an explanation brings with it, so a question
with no explanation still ends on its answer repeated. (The one exception is a
configuration with *both* the answer and the explanation switched off, where the
row would be genuinely blank and is dropped instead.)

Inside the explanation row, `Explanation:`, `Other Options:`, `Incorrect
Options:`, `Educational Objective:` and `Take Home Message:` are each put on
their own line in bold, with **no blank line above or below**. Under an
Other/Incorrect Options heading, each entry gets a bold `Option ` prefix and its
letter is bolded too — `**Option A.** …` — and every entry gets a blank line
above it:

```
Incorrect Options:

    Option A. … is holocrine.

    Option C. … is apocrine.

    Option D. … is apocrine.

Take Home Message:

Caudate gives chorea, …
```

This is `format_docs.py`'s house style, rule for rule, and the output is
paragraph-for-paragraph what running that script yourself produces.

**A document that already has question boxes is restyled in place, not rebuilt
— as long as you change nothing.** The boxes exist; there is nothing to build.
Every row your file had survives, including the ones the box model has no slot
for — the trailing `Tags: #Hyperrevision#Factual` row is the one that matters —
and every picture and grid stays exactly where you put it. The moment you edit
a question on the preview screen, leave one out, reorder them, switch a layout
tick off, or the file's own numbering had to be corrected, the document is
written from the questions on screen instead, because a restyle would silently
throw those changes away. The footer of the preview says which of the two will
happen.

Two things it will not do:

* **It never writes a second `Explanation:`.** If your file already has one, that
  one is used. The match ignores case, so `EXPLANATION:` and `Explanation:` count
  as the same heading — treating them as different is what used to produce two.
* **It never adds a heading to a block that has another one.** An explanation
  that opens on `Other Options:` is left as it is.

**Bold is carried over.** Any word the source document shows in bold comes out
bold, in the stem, the options and the explanation. The ranges are recorded
against the source's own characters and re-applied to the exact text that was
sliced out, so it cannot drift onto a neighbouring word; editing a question in
the review table drops its recorded bold rather than applying it to new wording.

### Choosing what goes in the deck

Before any PowerPoint is written — whether you started from **Build PowerPoint
Deck** or from **Export PowerPoint** in the review table — a panel asks what
each slide should show:

| Tick box | Effect |
|---|---|
| Question number or "Q" | Adds a label in front of the stem. Pick **numbered** (`Q1.`, `Q2.` …) or **just `Q.`** on every slide. Untick for no prefix at all. |
| Question text | The stem itself. |
| Options | The A/B/C/D list. |
| Correct answer | Include it, either **on the next slide** or **as a banner** on the question slide. |
| Title slide / "Thank you" slide | The opening and closing slides. |

A live preview shows the effect before you commit, and **Build deck** is disabled
if you untick everything (60 blank slides is never what anyone meant). Your
choices are saved as the defaults for next time; Cancel changes nothing.

The review table appears next: every question with its options, answer, origin,
confidence and flags. Rows needing attention are tinted amber, a verbatim
mismatch red. Edit anything inline, then export to PowerPoint — or press
**Word preview & export** to see the document first.

### The Word preview screen

**Extract & Format MCQs**, **Detect Correct Answer** and **Merge Multiple
Files** all land here after parsing, and the review table's Word button opens
it too. It is the Word counterpart of the PowerPoint contents panel, with the
document itself on screen:

| Pane | What you can do |
|---|---|
| **Questions** (left) | The list, in the order the file will have. Untick a question to leave it out of the export; **▲ Up** / **▼ Down** reorder; **Include all** / **Exclude all**. Amber rows need a human. |
| **Preview** (centre) | Every question drawn exactly as its Word box will be written: the same five rows, the source's bold, its pictures and grids, and the `Explanation:` / `Other Options:` house rules already applied. Click a box to edit it. A box the file will not contain is greyed. |
| **Edit** (right) | The selected question: the question text, every option (reword it, **✕** removes it, **+ Add option** appends the next letter, tick the radio button of the correct one), **Clear answer**, the explanation, and the plain-English warnings for it. |
| **Layout of every box** (below) | Tick boxes for the answer line, the repeated answer above the explanation, the explanation, AI-answer marking, keeping the source's bold, **numbering 1, 2, 3 … in this order**, plus font and size. The preview follows every tick; the choices are saved as the defaults for next time. |

The footer counts the questions, the excluded ones and the ones needing
attention, and says whether the file will be written from these questions or —
for a box document you have left untouched — restyled in place. **Export Word**
writes exactly what the preview shows. **Review table** switches to the table
view of the same questions; an edit on either screen is visible on both.

**Numbering.** Source files restart at 1 for a second section, print a "2."
after a "5.", repeat a number or print none at all. Questions are therefore
counted 1..N in document order, and the number the file printed is shown beside
each one ("was 7 in the source"). Untick *Number questions 1, 2, 3 …* to write
the file's own numbers instead.

### Appearance

The app opens light. **Settings → Branding & output → Appearance → Theme** offers
`Light` or `Dark`; the change takes effect when you restart the app.

**Dark is the Batman theme.** Switching to it turns on three things at once:

* the wallpaper (`app/ui/assets/wallpaper_dark.jpg`) behind the whole window,
  cover-fitted to whatever shape the window is and dimmed so text stays readable
  wherever it lands;
* gunmetal blue-black panels that go slightly translucent, so the picture reads
  through them without competing with the content;
* a palette **sampled from the picture itself** — the surfaces are the suit and
  the cape, the lines are the cape's lit edge, and the accent is the moonlight on
  the cowl.

The accent is moonlight blue rather than bat-signal gold on purpose: amber already
means *"this question needs a human"* everywhere in the review table, and an
accent that looks like a warning would be a worse app however good it looked.
Amber and red stay purely semantic in both themes.

**Light stays plain.** A photograph behind a white working surface fights every
panel on screen, so the wallpaper is dark-only. To change it, replace
`wallpaper_dark.jpg`; if the file is absent the dark theme simply renders on flat
gunmetal.

**Colour on the tiles is deliberately quiet.** Each feature tile carries its own
hue on the icon square only — never on text, a border or a whole card — and every
one of them is cool and low-saturation, mixed down to about a sixth of its
strength against the card beneath it. Warm hues are not used there at all,
because amber and red are already spoken for. The hues live in `theme.TINTS`; the
mix that lands on screen is `theme.tile_colors`.

---

## Command line

The whole pipeline runs headless, which is also how it is tested:

```
python main.py --cli FILE [FILE ...] --action extract|answers|pptx|merge
               [--out DIR] [--no-ai] [--json REPORT.json] [--no-export] [-v]
```

| Option | Meaning |
|---|---|
| `--action` | Which of the four actions to run (default `extract`). |
| `--out DIR` | Where exports go. Default: the folder set in Settings, else beside the input file. |
| `--no-ai` | Rule-based parsing only, even when a key is configured. |
| `--json REPORT.json` | Write a machine-readable report: per question the stem, options, answer, origin, confidence, flags and `verbatim_ok`. |
| `--no-export` | Parse and report only; write no `.docx`/`.pptx`. |
| `-v` / `-vv` | Progress on stderr / debug logging. |

Deck contents — the same choices the GUI panel offers, for headless runs. Each is
a two-way pair, so a flag you omit keeps its saved setting and a flag you pass
wins either way (including turning something back **on** that the panel switched
off):

| Option | Meaning |
|---|---|
| `--q-prefix number\|q\|none` | Label before the stem: `Q1.`, `Q.`, or nothing. |
| `--question-text` / `--no-question-text` | Include or omit the stem. |
| `--options` / `--no-options` | Include or omit the A/B/C/D list. |
| `--answers` / `--no-answers` | Include or omit the correct answer. |
| `--answer-next-slide` / `--no-answer-next-slide` | Answer on its own slide, or as a banner on the question slide. |
| `--title-slide` / `--no-title-slide` | Include or skip the opening slide. |
| `--thankyou-slide` / `--no-thankyou-slide` | Include or skip the closing slide. |

> The panel saves your choices as the next run's defaults. If a deck comes out
> missing something you expected, check the panel — or state it explicitly, e.g.
> `--answers --answer-next-slide`.

Arguments may be files, folders or globs (globs are expanded internally, so they
work in PowerShell too). Existing output files are never overwritten — a `_2`
suffix is added.

Examples:

```
python main.py --cli "D:\Bhanu Sir\ENT Recall Question.docx" --action extract --out out
python main.py --cli bank1.docx bank2.docx --action merge --json report.json
python main.py --cli lecture.docx --action pptx --no-ai -v
```

Exit codes: `0` success, `1` at least one file failed, `2` bad usage or a missing
dependency.

---

## How accuracy works

**Span-anchored extraction.** Each input file is flattened into a numbered list
of source lines. The model is shown that list as `L0| text`, `L1| text`, … and
answers with *line indices* — which lines are the stem, which lines are option
B, which line states the answer. It never returns question text. All text is then
sliced verbatim out of the source by index.

That makes "preserve the wording exactly" structural rather than a request:

* Option labels (`A.`, `(A)`, `a)`) are stripped by regex on the source line,
  never by the model.
* Every question is re-derived from its indices and compared against the
  document. A mismatch sets `verbatim_mismatch`, colours the row red and appears
  in the JSON report — nothing is silently reworded.
* The rule-based parser runs on every file as well. The two parses are
  reconciled: the AI wins on structure, the rules recover questions it dropped,
  and any disagreement lowers the confidence and raises `low_confidence` so a
  human sees exactly the questions worth checking.

**AI-derived answers are always flagged.** A stated answer key (`Ans. B) …`) is
copied from the source and marked origin `source`. When the document states no
key, a derived answer is marked origin `ai`, carries `ai_answer`, and the answer
text is *copied from the matching option* — the model supplies only the letter, a
confidence and a one-line rationale.

The bot refuses to guess when guessing would be dishonest: a question whose stem
refers to an image that was not extracted, or an answer the model reports below
0.5 confidence, is left blank with `no_answer` / `low_confidence`. A wrong answer
key is worse than an empty one.

**Nothing is dropped silently.** Duplicates, numbered sub-lists inside a stem
(`1. Ligamentum flavum` … under a question — a classic false split), missing
options, empty stems and merge collisions all become flags on the question and
lines in the run's warnings. A file that is not a question bank at all yields
zero questions rather than garbage.

**Chaotic files are read by structure, not by numbering.** A question begins
where a stem is followed by its answer choices (or an `Options:` / `Choices:`
header, or an answer line), whatever number it carries: numbering that restarts
at 1 for a second section, a `2.` after a `5.`, a `MCQ 3:` header, a stem with
no number at all above a `Choices:` line, a stem sitting in a table beside its
picture. Inside an explanation, a number alone proves nothing — an explanation
that re-lists "1. … 2. … 3. … 4. A history of amaurosis fugax" used to open a
phantom question 4 exactly when question 4 was expected, and swallow the rest
of the file. The rule parser also reads `Correct Choice:` / `CorrectChoice:`
keys, a key given as the option's text on the next line, a tick (✅) on the
correct option, and options printed with no letters at all (`1 and 2` /
`1,2 and 3` / `Only 1` / `All of the above` straight after the statements,
then `Answer : B`), which it labels A–D in printed order and flags for a look.
Section titles between questions (`FMGE MOCK 2:`) belong to no question.
`tests/test_rules_chaotic.py` holds one reduced case per shape.
