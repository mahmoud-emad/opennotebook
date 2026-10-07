# Plan: themes for video overviews

Status: phases 0 to 4 built (2026-10-07). Phase 4 is tested against the test model only, not yet a real image model (the account's balance was too low), and has no Custom style yet. See the spec's amendments "themes" and "illustrated themes".
Read first: [video-overview-spec.md](../video-overview-spec.md) (how a video is made today), sections 4.4 to 4.9 and the amendments.

## 1. What and why

A whiteboard video has one look: dark marker on off-white paper, the Caveat hand. People want to choose how their video looks, as NotebookLM lets them (Classic, Whiteboard, Watercolor, Retro Print, Heritage, Paper-craft, Anime, Kawaii, or a style typed in).

NotebookLM gets its styles by painting every slide with an image model (Nano Banana, Gemini's image model) in the chosen style. That gives range, but a painted slide can be wrong or carry garbled text, and nothing is drawn as it is said. Our whiteboard is the other way round: the model writes a scene description, the studio draws it, every label is checked against the sources, and each piece appears on the word that names it. That accuracy is the product, so themes come in two families:

- **Drawn themes** (phases 1 to 3): the same scenes, drawn with another paper, ink, pen and hand. They cost nothing per video, keep every check, and still draw as they are spoken.
- **Illustrated themes** (phase 4): a picture per scene from an image model, in a chosen art style, under the studio's own checked labels. They cost about 4 cents a scene and are offered as a paid option.

| Theme | Family | Phase | Looks like |
|---|---|---|---|
| Whiteboard | drawn | 0 (today's, the default) | Off-white board, dark marker, coloured markers, yellow highlighter |
| Notebook | drawn | 1 | Lined paper with a red margin, blue ballpoint, yellow highlighter |
| Chalkboard | drawn | 1 | Dark green slate, chalk strokes with grain, pastel chalks, a chalk smudge for highlights |
| Blueprint | drawn | 1 | Prussian blue sheet, white grid, thin white technical pen, drafting hand |
| Retro Print | drawn | 2 | Cream newsprint, two or three spot inks, halftone fills, slight misregistration |
| Paper-craft | drawn | 2 | Flat cut-paper shapes with soft drop shadows on a card background |
| Watercolor | illustrated | 4 | Soft washes, painted scene, ink labels |
| Anime | illustrated | 4 | Cel-shaded illustration, clean labels |
| Heritage | illustrated | 4 | Engraving or vintage textbook plate |
| Kawaii | illustrated | 4 | Cute rounded characters, pastel |
| Custom | illustrated | 4 | The person's own words, e.g. "children's storybook" |

Previews of each drawn theme, rendered by the real renderer, are in [plans/img](img/): `video-themes-<id>.png`.

## 2. Decisions

1. **A theme is an option of the whiteboard style, not a style of its own.** A video's state stays keyed by style (`slides`, `whiteboard`, `api/video.py`). The whiteboard state gains `theme` (the theme's id), and making the video again in another theme replaces it, as making it again does today. Illustrated themes are whiteboard-style videos too: same plan, same scenes, same checks, same timing; only how a scene is shown changes.
2. **One `Theme` object carries the whole look**, and nothing about the look stays hard-coded elsewhere. Today it is spread over:
   - `build/whiteboard/draw.py`: `BOARD`, the pen tip in `_marker` (three colours), the wash blend in `_paint` (`kMultiply`).
   - `build/whiteboard/compile.py`: `INK` (five tones), `WASH`, `STROKE`, `LABEL_SIZE`, the title size.
   - `build/whiteboard/geometry.py`: `_font()` loads `assets/Caveat-Bold.ttf`, and `wobble`'s amplitude.
   - `build/whiteboard/check.py`: `still()` clears with `BOARD`.
   - `build/whiteboard/frame.py`: the opening and closing slides' CSS (paper, ink, accent, the hand font).
3. **The theme travels with the segment.** Scenes are drawn in child processes (`python -m opennotebook.build.whiteboard.draw <segment.json>`), so `draw.Segment` gains `theme: str` and the child looks the theme up by id. A theme never travels as a pickled object.
4. **The model's prompts do not change with a drawn theme.** Tones stay `ink | blue | red | amber | green`; the theme maps each tone to its own colour. A scene written once can be drawn in any theme, and the checks (grounding, lint, the vision check) run as they do now. The vision check should be shown the scene in the theme it will be drawn in (`check.still` takes the theme).
5. **Readable first.** Every theme's five inks and its highlighted ink must reach a contrast of at least 4.5:1 against its paper (WCAG AA for text). A test enforces it (section 7).
6. **Fonts are bundled, open-licensed TTFs**, drawn as outlines like Caveat today, with the same fallback for missing glyphs (`geometry._needs_shaping` / fallback fonts). Candidates, each to be checked for its licence (OFL or Apache 2.0) and its Latin and extended coverage before it is added: Kalam or Patrick Hand (Notebook), Gochi Hand or Caveat (Chalkboard), Architects Daughter (Blueprint), Special Elite or a slab serif (Retro Print), Caveat or Patrick Hand (Paper-craft). Add each to `assets/LICENSES.md`.
7. **Defaults.** Whiteboard is the default and stays exactly as it is: phase 0 changes no pixel of it (enforced by a golden test). A Settings default (`OPENNOTEBOOK_VIDEO_THEME`) can come later; it is not needed.

## 3. Phase 0: the plumbing (no visible change)

Server:
- `build/whiteboard/theme.py` (new): `@dataclass(frozen=True) class Theme` with:
  - `id`, `label`, `family` (`drawn` | `illustrated`).
  - Paper: `paper: RGB`, plus `background: Callable[[skia.Canvas], None] | None` for rules, a grid, grain or a margin line, drawn once per board. Implement as named kinds (`"plain"`, `"lined"`, `"grid"`, `"slate"`), not as callables, so the theme stays plain data.
  - `ink: dict[Tone, RGB]`, `highlight: RGB`, `highlight_blend` (`multiply` on light paper, `screen` or plain alpha on dark).
  - `stroke: float`, `wobble: float`, `pen: "marker" | "chalk" | "ballpoint" | "technical"` (how a line is painted, section 4), `tip: (RGB, RGB, RGB)` for the pen tip that follows the drawing.
  - `font: str` (asset file name), `title_size`, `label_size`.
  - `slide_css: str` (the opening and closing slides' variables: paper, ink, accent, font face).
- `THEMES: dict[str, Theme]` with `whiteboard` holding today's values exactly. Add `theme_of(id) -> Theme`, which falls back to whiteboard for an unknown id (an old video's state has none).
- Thread the theme through:
  - `compile_scene(sc, when, start, end, theme=WHITEBOARD)`: inks, sizes and wash come from the theme.
  - `draw.Segment.theme`, used by `frames()`, `_still_frames()`, `_marker()` and `_paint()`.
  - `check.still(..., theme)`.
  - `frame.opening_html` / `closing_html(..., theme)` and `frame.board_png(..., theme)`.
  - `geometry.text(s, size, font=...)`, with `_font` cached per file.
- `build/video.py`: `render(..., theme: str = "whiteboard")` passes it to `_whiteboard` and into each `Segment`, and writes `"theme"` into the video state. `jobs/tasks.py`'s `render_video` task takes `theme` and passes it on.
- `api/video.py`:
  - `VideoReq` gains `theme: ThemeId = "whiteboard"`, with `ThemeId = Literal[...]` of the theme ids that exist, so the OpenAPI client lists them.
  - `OverviewReq` gains it too. A collection overview stores it with its `waiting` state, so `video.after_build` queues the render with it.
  - `VideoState` gains `theme: str | None`.
  - `GET /api/video/themes` lists `{id, label, family, cost_note, preview_url}` for the picker.
- `video.queue(s, o, style, theme)` keeps the theme in the waiting/rendering state so a retry uses it.

Web (`web/src/ui/video.tsx`, `web/src/styles/video.css`): no picker yet. `makeVideo`/`makeOverview` send `theme` when given. Regenerate the client (`DATABASE_URL=<placeholder> pnpm run api` in `web/`).

Tests:
- `tests/builds/test_whiteboard.py`: a golden test that today's whiteboard frames are unchanged. Render `_scene()` with the default theme and compare against the same scene rendered before the change. Store a small PNG or a hash of the raw RGBA in `tests/builds/golden/`; skia output is deterministic with bundled fonts.
- The API accepts `theme`, rejects an unknown one with 422, and the state reports it.

## 4. Phase 1: Notebook, Chalkboard, Blueprint

Drawing work in `draw.py` / `compile.py`:
- **Backgrounds**, drawn once on the board surface before any piece (`frames()` clears the board with paper, then the background):
  - `lined`: horizontal rules every 40 px, with a margin line at x=150.
  - `grid`: a grid every 48 px.
  - `slate`: a dark board with a faint low-frequency smudge texture (a few large, very low-alpha blurred ellipses, from a fixed seed).
  - The background must also show under the wipe between scenes: the wipe fades to paper plus background, not to a flat colour.
- **Highlight blend**: `multiply` for light paper, as today. For dark paper, a light translucent wash (`kScreen`, or `kSrcOver` at about 35% alpha) behind the element, so the label stays readable. Fixes the faded "Antibodies" in the previews.
- **Pens** (`pen` in the theme), all applied in `_paint` / `draw_piece`, never changing the geometry:
  - `marker`: today's round 5 px stroke.
  - `ballpoint`: thinner (3 px) with very slight width variation along the stroke.
  - `technical`: thin (2.5 px), perfectly even, `wobble` near 0.3 so lines look ruled.
  - `chalk`: the stroke painted with a grain shader. Use a `skia.Shader` built from a small tiling noise image (64×64, fixed seed, generated at import), set as the paint's shader with the ink as colour, plus a second pass at lower alpha and +1.5 px width for the dusty edge. Text gets the same shader. Grain must be the same on every frame (stable, no shimmer), so the shader is in board coordinates, not stroke coordinates.
- **Pen tip**: a theme colour set. Chalk shows a chalk stick and Blueprint a pen nib, both drawn as simple shapes in `_marker`.
- **Slides** (`frame.py`): the opening and closing slides use the theme's CSS variables and font: dark slides for Chalkboard and Blueprint, lined paper behind the cards for Notebook.

| Theme | paper | ink (ink / blue / red / amber / green) | highlight | font | pen |
|---|---|---|---|---|---|
| Notebook | #FFFEF8, lined #C7DBF5, margin #F4A6A6 | #1E3A8A / #1D4ED8 / #C02626 / #B45309 / #15803D | #FEF08A multiply | Kalam or Patrick Hand | ballpoint |
| Chalkboard | #243B33, slate | #F1F1EA / #9CD3F5 / #F59E9E / #F7D37A / #B5E8A8 | #F1F1EA at 18% | Gochi Hand or Caveat | chalk |
| Blueprint | #143D7A, grid #2B5A99 | #F0F6FF / #A8D8FF / #FFB4A8 / #FFE08A / #B8F0C8 | #FFFFFF at 16% | Architects Daughter | technical |

The values are starting points from the previews. The contrast test (section 7) has the final word.

## 5. Phase 2: Retro Print and Paper-craft

Both need filled shapes, which the compiler does not make today: it emits `line`, `text` and `wash` pieces. Add a `fill` piece kind: a closed path with a paint, drawn in `draw_piece` and revealed as the drawing progresses. The reveal is a wipe across its bounding box in the drawing direction, so it still appears on its word.

- **Retro Print**:
  - Cream newsprint paper (#F4EBD6) with fine noise.
  - Two spot inks (a deep red #B3261E and a navy #1F2A44) plus black. The five tones map onto them: blue and green become navy, red and amber become red.
  - Boxes and circles get a halftone fill: a dot-pattern shader at 20% coverage, in the element's ink.
  - Lines get a 1.5 px offset second impression in the other ink at 30% alpha (misregistration).
  - A slab or typewriter face (Special Elite, Apache 2.0, or similar).
  - The slides use the same newsprint, with a masthead rule under the title.
- **Paper-craft**:
  - Card background (#EFE7DA) with a faint fibre texture.
  - Boxes and circles become solid pastel cut shapes in the element's tone, with a 6 px soft drop shadow. Their outline is a slightly irregular cut edge: the existing rough rectangle with a larger wobble, filled.
  - Icons sit on a white paper disc with a shadow.
  - Arrows are thick paper strips with a shadow, not lines.
  - Labels in dark ink on the shapes. The contrast test runs on the shape colour, not the paper.
  - Reveal: a shape "drops in", scaling from 1.08 with the shadow growing, over 250 ms on its word, instead of being drawn.

Lint (`lint.py`) must treat a filled shape as covering its box, as it treats a box today.

## 6. Phase 3: the theme picker and polish

- **The Video overview tool** (`VideoOptions` in `video.tsx`) gets a **Theme** row: a grid of thumbnails, each the same sample scene rendered in that theme. Generate the thumbnails at build time with a script (`server/scripts/theme_previews.py`, writing into `web/public/themes/<id>.png`) and commit them; do not render them per request.
  - Illustrated themes show a "+ about 30 cents" badge.
  - The choice is remembered in localStorage per collection, wrapped in try/catch like the other per-browser preferences.
- **The row on an output** ("Make a video of this") makes the video in the last theme used, or Whiteboard.
- **The watch page** shows the theme in its subtitle ("Chalkboard video · 3:22").
- `docs/video-overview-spec.md`: an amendment describing the themes.

## 7. Tests for phases 1 to 3

- **Contrast**: for every drawn theme, each of the five inks, and the ink over a highlighted box, against the paper (and against the fill for Paper-craft) is at least 4.5:1. Compute WCAG relative luminance in the test, no dependency.
- **Golden stills**: one still per theme of the fixed test scene, kept in `tests/builds/golden/`, compared with a tolerance (mean absolute difference under 1%) so a skia patch release does not break them. Regenerate on purpose with `UPDATE_GOLDEN=1`.
- **Determinism**: every theme's frames are the same on two runs (the existing `test_frames_start_blank_fill_in_and_are_the_same_every_time`, parametrised over themes).
- **The pieces join**: `test_a_segment_is_drawn_in_pieces_that_join_to_every_frame`, parametrised over at least Chalkboard (shader) and Paper-craft (fills).
- **End to end**: `test_a_deck_becomes_a_whiteboard_video` with `theme="chalkboard"`. The state says chalkboard, and a probed frame's mean colour is dark (the paper is the slate).
- **Web**: the picker posts the chosen theme, and the illustrated badge shows.

## 8. Phase 4: illustrated themes (Watercolor, Anime, Heritage, Kawaii, Custom)

How a scene is shown changes. What it says does not: the plan, the scene JSON, the grounding and the claims check stay as they are.

**The picture**: one image per scene, made after the scene is written and checked.
- **Model**: an image model through OpenRouter. The likely candidate is Gemini 2.5 Flash Image ("Nano Banana", `google/gemini-2.5-flash-image`), at about $0.039 an image, 1024×1024 or 16:9. Check the model id, the price and the 16:9 support on OpenRouter before writing code.
  - Add `VIDEO_IMAGE_MODEL` to `domain/settings.py` with the other `VIDEO_*` keys, and update the settings counts in `tests/test_settings.py` and `tests/test_rpc.py`.
- **Client**: `ai/client.py` sends chat completions. Add `modalities: list[str] | None` to `complete()` and `_body()`, and read the image from the response: OpenRouter returns it in `choices[0].message.images[].image_url.url` as a data URL. `Completion.raw` already keeps the body. Charge it to the ledger like any call: the client's usage and cost recording applies, so check that `cost_usd` comes back for image calls.
- **Prompt** (`build/whiteboard/illustrate.py`, new): `STYLE_PROMPTS[theme]` (one paragraph per style, e.g. Watercolor: "soft watercolour washes on textured paper, loose ink outlines, muted natural palette"). Then the scene's planned brief and its concepts, never its labels. The rules:
  - **No text, letters, numbers or symbols of any kind.**
  - Leave the band where the labels go clear: a calm, low-detail area.
  - Show only what the brief says.
  - Custom: the person's own words replace the style paragraph, length-limited (200 characters) and passed as data in the prompt, never as instructions.
- **Check**: run the vision checker (`check.py`) on the image with a picture rule: "Is there any text or lettering? Does the picture show something the scene's brief or narration contradicts?" Text or a contradiction means one retry with the fault named, then the scene falls back to being drawn on the theme's drawn twin (Watercolor falls back to Notebook, the others to Whiteboard). Record `illustrated` / `fallback` counts in the state, as `plain` is now.
- **Showing it** (`draw.py`):
  - **The picture**: it fills the frame and moves with a slow push-in (Ken Burns), 1.00 to 1.06 over the scene, its origin varied per scene from a fixed seed. Scenes cross-fade into each other (400 ms).
  - **The labels**: the scene's grounded labels, numbers and title appear on their words as clean caption chips. These are rounded cards in the theme's ink on a translucent paper, anchored in the clear band the prompt asked for (the bottom third by default). They are laid out by a small chip layout in `compile.py` (left to right, wrapping), not on the 6×6 grid.
  - Arrows and icons are not drawn over a picture. A relation the scene asserts with an arrow becomes a chip ("Vaccine → Harmless copy"), so the claim stays on screen and stays checked.
  - The pen tip is not shown.
- **Cost and limits**:
  - A 7 to 9 scene video adds about $0.30 to $0.40. The tool says so, and the spending limit's estimate for a render counts it (scenes × image price); find where builds are priced before they start and add it there.
  - Images are made in parallel (at most 4 at once), overlapping the scene writing as drawing does now.
  - The opening and closing slides use the theme's CSS. For Watercolor, an illustrated header image on the opening slide may come later; it is not needed.
- **Storage**: keep each picture under `video/<sid>/art/scene-NNN.png` beside the voice files, so a re-render in the same theme with an unchanged scene could reuse it. That reuse is optional and comes later.
- **Tests** (with the fake Studio in `tests/builds/fake.py` answering image requests with a fixed PNG):
  - An illustrated video renders, and its state says so.
  - A picture the fake checker says has text is retried once, then falls back to drawn.
  - The image prompt never contains a label.
  - The custom style is length-limited.
  - The cost of a fake image is in `spent_usd`.

## 9. Order, size, and what is done when

| Phase | Size | Done when |
|---|---|---|
| 0 plumbing | about half a day | All tests pass, the whiteboard golden test is unchanged, the API takes and reports `theme` |
| 1 three drawn themes | about a day | A real video renders in each on the lab; the contrast and golden tests pass; the four previews in `img/` are regenerated from the real renderer |
| 2 Retro Print, Paper-craft | one to two days | Fills and reveals work, lint understands fills, a real video in each |
| 3 picker | half a day | Thumbnails, the remembered choice, the subtitle, the spec amendment |
| 4 illustrated | two to three days | A real Watercolor video on the lab, with its cost measured and no text in any picture over five videos (eyeballed and checked) |

Phases 0 to 3 ship without any new model call; phase 4 can ship later on its own.

## 10. Working notes for the agent doing this

- **The machine**: the code runs on the lab machine (`ssh remote-lab01`, repo `~/code/research/opennotebook`, branch `development`).
  - The dev stack runs from `~/start-dev.sh`, which reads the AI key from `~/.config/opennotebook/ai.env`. Never print or copy keys.
  - Stop the stack by PID, not with `pkill -f`: that pattern matches your own ssh command line.
  - The web app is at `http://localhost:5173` on the lab. Reach it with `ssh -f -N -L 15173:localhost:5173 remote-lab01`.
- **Checks**:
  - Server, from `server/`: `uv run ruff check`, `uv run ruff format`, `uv run pyright`, and `uv run pytest -q`. Tests need `TEST_DATABASE_URL` pointing at an empty Postgres 18 with pgvector. Run one suite at a time on one database; two at once deadlock.
  - Web, from `web/`, with `PATH=$HOME/.local/bin:$HOME/.local/node/bin:$PATH` on the lab: `pnpm exec tsc -b`, `pnpm exec eslint .`, `pnpm exec vitest run`, and `pnpm run build` (first screen under 150 KB).
  - The client is regenerated with `DATABASE_URL=postgresql://gen@localhost/gen pnpm run api`.
- **Rendering slides**: the browser for the slides is Playwright's Chromium. If a video's opening and closing slides come out as plain boards, the browser is missing: run `uv run playwright install chromium` in `server/`.
- **Commits**:
  - Author `Mahmoud-Emad <mahmmoud.hassanein@gmail.com>` (`git -c user.name=Mahmoud-Emad -c user.email=mahmmoud.hassanein@gmail.com commit`).
  - No Co-Authored-By or Signed-off-by, and no mention of AI tools in messages.
  - Short conventional messages, one feature per commit.
  - The lab has no GitHub credentials: commit from a clone on the Mac and push from there, then move the lab onto the new HEAD with a git bundle and `git reset --mixed`.
- **Coordination**: another agent may be working in the same tree. Do not overwrite files you did not change; tell it which shared files you touched (`routes.ts`, `App.tsx`, `pipeline.py`, `settings.py`, `web/openapi.json`, `web/src/client/*`).
