# OpenNotebook, study notes specification

Study notes of a collection's sources, on its collection page. The person adds sources, picks the Study notes tile, presses Generate, and gets a written study guide: an overview, the key ideas, a short-answer quiz with its answers, essay questions and a glossary. Every claim carries a numbered chip that shows the passage it rests on.

It is modelled on NotebookLM's Study guide and Briefing doc reports. Where this spec copies NotebookLM or an open implementation it says so; where it departs, it says why. The labels are the ones the [mind map spec](mindmap-spec.md) uses: Measured, Decided, Observed.

## 1. What the others do

Google has published no prompt or design for NotebookLM's reports. What follows is read from user write-ups, typical outputs and the source of open implementations.

- **NotebookLM's study guide (observed).** A title, then a short-answer quiz of about 10 questions answerable in two or three sentences, its answer key as a separate numbered section, about 5 essay questions with no answers, and a glossary of key terms. The briefing doc adds an executive summary and the main themes. Reports keep their citations, shown as numbered chips: hover shows the passage, click opens the source at it.
- **NotebookLM's pipeline (observed).** Gemini's long context: the whole of the selected sources in one call when they fit.
- **[SurfSense](https://github.com/MODSetter/SurfSense).** Each passage gets a small integer label `[n]` and the model is told to put the label right after the claim, stack labels as `[1][2]`, copy them exactly, never invent one, and leave a claim uncited when nothing backs it. Unknown numbers are dropped after generation so a bad citation disappears rather than misleads.
- **[Kotaemon](https://github.com/Cinnamon/kotaemon).** Checks that a cited phrase really occurs in its passage, accepting a fuzzy match of at least 35% of the phrase.
- **[open-notebook](https://github.com/lfnovo/open-notebook).** No study guide. Cites whole documents by id rather than passages, which is too coarse for a chip that shows a passage.
- **[notex](https://github.com/smallnest/notex).** Cuts each source at 100k characters and appends "content truncated", which silently loses coverage. The thing to avoid.

## 2. Decisions

| Decision | Choice | Basis |
|---|---|---|
| Sections | Overview, Key ideas, Quiz, Answer key, Essay questions, Glossary | Decided. NotebookLM's study guide, with its briefing doc's overview and key ideas in front, because the tile promises "the key ideas". |
| Sizes | 4 to 7 ideas, 10 questions, 10 answers, 5 essays, 15 to 20 terms | Observed sizes of NotebookLM's guide. |
| What the model reads | Every source whole, cut into numbered passages of about 900 characters | Decided. A citation can then point at any passage, not only the 8 a question would retrieve. The 190 KB Moshi paper is about 211 passages. |
| How it cites | `[n]` after the claim, SurfSense's five rules in the prompt | Observed, SurfSense. |
| What is checked | A number naming no passage is removed. A citation whose claim shares fewer than two words with its passage is removed. Both are counted and shown. | Decided, and a departure: NotebookLM shows nothing about this. |
| Numbering | Renumbered in reading order, so the reader meets `[1]` first | Decided, the same as `source_ask`. |
| Format | Markdown under fixed headings, read into sections on the server | Decided. A heading the model renames ("Key concepts", "Short-answer questions") is still found. |
| Model | `OPENNOTEBOOK_NOTES_MODEL`, default `google/gemini-2.5-flash-lite` | Decided. The script model's window is too small for a whole collection. |
| One call or several | One call; a second only when the first has fewer than 2 ideas | Decided. Above 600,000 characters the sources are cut to excerpts, and the notes say so. |
| Storage | `<data dir>/notes/<cid>/<id>.json` | Forced, as for maps: the build ingests everything in staging. |
| Quiz answers | Hidden until asked for, one at a time or all | Decided. A study guide is for testing yourself. |
| Export | Markdown, every section, the cited passages listed at the end | Decided. Built on the server and stored with the notes, so an agent reads the same document. |

## 3. Where it lives

- `crates/opennotebook_script/src/notes.rs`: the prompt, the citation check, the section reader and the Markdown export, with their tests.
- `crates/opennotebook_script/src/grounding.rs`: `passages()`, every prose passage of every source with its source index.
- `crates/opennotebook_api/oschema/notes/notes.oschema`: the `notes` domain at `/api/notes/rpc`: `notes_create`, `notes_estimate`, `notes_list`, `notes_list_all`, `notes_get`, `notes_delete`.
- `crates/opennotebook_server/src/notes_impl.rs`: the domain and the store.
- `crates/opennotebook_ui/src/notes.rs`: the list under the sources, the home card and the viewer. Citation chips are the chat's own (`mindmap::with_chips`).
- `crates/opennotebook_ui/src/main.rs`: Study Notes as the third output, with its own addresses `/ui/new-study-notes` and `/ui/study-notes/<sid>/<id>`, its home filter and section.
- `crates/opennotebook_server/src/agent.rs`: on a notes page the agent has no `start_build` and points at Create study notes.

## 4. Measured

On 2026-10-03, on this box, through OpenRouter.

| Draft | Time | Result | Citations removed | Estimate |
|---|---|---|---|---|
| Moshi paper, 188,709 characters, run 1 | 9.7 s | 6 ideas, 10 questions all answered, 5 essays, 19 terms, 34 passages cited | 1 | $0.0066 |
| Moshi paper, run 2 | 9.6 s | 6 ideas, 10 questions all answered, 5 essays, 20 terms, 21 passages cited | 7 | $0.0066 |
| A 1,151 character note, through the browser | 6 s | 5 ideas, 10 questions, 21 terms | 0 | $0.0017 |

Checked in a real browser: the New dialog offers Study Notes, a typed note becomes a source, Create study notes writes and opens the notes at their own address, Show answer and Show all answers work, a chip's popover shows the passage, the bar then says the notes are up to date, the home page lists them under Study notes, there is no overflow at 390 px, and the console stayed empty.

## 5. Known limits

- **Pronoun claims.** "It ensures high-quality audio output" names its subject in the sentence before, so the check finds too few shared words and removes a citation that was right. Most of the 7 removals of run 2 are of this kind. Reading the previous sentence into the claim would fix most of them.
- **Hover in a headless browser** does not hold, so the popover was checked with keyboard focus.
- **Not in the player.** Notes of a built session are kept under its sid and could open beside the player later without a migration.

## Amendments

### 2026-10-03, built and measured

The first citation check kept a citation when about three in ten of the claim's words were in its passage. On the Moshi paper it removed 22 citations, about half of them right: a long paraphrase shares few words in proportion. It now keeps a citation when the claim and the passage share two words, or one word of a claim of three words or fewer. The wrong-passage case it was built for, "Mimi is a neural audio codec" citing a passage about audio tokens, is still removed, and a test holds it.

Building it found the same fault in the mind map: the bar decided "up to date" by comparing the page's source rows, which name a source by its title until a reload, against the stored file names. Both now compare against the server's own list.

A citation chip near the top of a scrolling pane had its popover cut off by the pane's edge. Chips now open their popover below when there is no room above, in the notes and in the chat.
