# OpenNotebook design

How the studio looks, and why. The tokens are `crates/opennotebook_sdk/assets/theme.css`, the icons are `crates/opennotebook_sdk/src/icons.rs`, the app's rules are `crates/opennotebook_ui/src/style.css`, and the player's are in its own page. This file is the reasoning those files follow.

## Which world this is

The usual rule for an admin UI is the constrained world: Bootstrap through a shared component library, no app CSS. The studio is an end-user creative product, not an admin pane, and the owner decided on 2026-10-03 that it keeps its own look under the rules that matter:

- **Bootstrap's names and mechanism.** Tokens are Bootstrap 5.3's own (`--bs-body-bg`, `--bs-secondary-bg`, `--bs-primary`, `--bs-border-radius-lg`). The theme switch is Bootstrap's `data-bs-theme` on `<html>`. Moving to a Bootstrap component library later would be close to a no-op.
- **Studio-only extras are marked as such.** What Bootstrap has no token for carries an `--st-` prefix and is marked `KEEP product:opennotebook`: a backdrop under media, a hover and a pressed layer, a colour per voice, the mind map's depth colours, motion.
- **Offline first.** Icons are Bootstrap Icons 1.11.3, vendored as inline SVG. Nothing is fetched at runtime; the player used to load the icon font from a CDN and drew blank controls without internet.
- **The accessibility floor.** Every text-on-surface pair is tested to WCAG AA in both themes, focus is always visible, and motion stops under `prefers-reduced-motion`.

## What the research said

Studied on 2026-10-03: NotebookLM's Studio panel, Linear's 2024 redesign, Figma UI3, Vercel Geist, Radix Colors, Adobe Spectrum 2, Apple's dark mode guidance, Material's dark theme, ElevenLabs Studio, Descript, Raycast, Runway and Pitch, plus NN/g on progress indicators. Recurring patterns:

- **Elevation is lightness, not shadow.** Raised surfaces are lighter (Spectrum 2's base and layers, Apple's base and elevated). Shadows are only for things that float: menus, dialogs, popovers.
- **One accent, used sparingly.** It marks the primary action, the selection and focus. Media supplies every other colour on screen (Runway, Linear).
- **Chrome is quieter than content.** Linear dimmed its sidebar "a few notches" so the content leads; Figma's UI3 motto is "Center your ideas, not Figma's UI."
- **Fewer, softer separators.** Hairlines inside a panel, a real edge only where one surface meets another.
- **Off-white text on dark.** Pure white on dark glares (Material); body text aims well above 4.5:1.
- **Long work shows real progress.** NN/g: anything over 10 seconds needs an indicator, and an AI step should say what it is doing.
- **Speakers are told apart by colour.** ElevenLabs Studio gives each voice its own colour beside its text.
- **Labelled controls beat mystery icons.** Figma reverted icon-only controls that "forced users to wait for a tooltip".

## The system

**Surfaces, darkest to lightest in dark mode:**

| Token | Use |
|---|---|
| `--st-backdrop` | under media: the player stage |
| `--bs-tertiary-bg` | chrome: the top bar, side panels, the bar under the chat |
| `--bs-body-bg` | content: the page, the conversation, the map canvas |
| `--bs-secondary-bg` | cards, inputs, chips |
| `--st-hover-bg` | a hovered row or button |
| `--st-overlay-bg` | menus, popovers, dialogs |
| `--st-pressed-bg` | pressed, and selected in a neutral list |

Light mode keeps the roles and inverts the values: white cards on a near-white page, chrome a step greyer.

**The theme is chosen, not followed.** Dark is the studio's own default. Light is the viewer's choice, in Settings, Appearance; it is kept in their browser and applies at once, there and in the player. The device's light or dark setting is deliberately not followed: a studio is a place you set up once, and it should not change under you.

**Text** is four levels: `--bs-emphasis-color` for titles, `--bs-body-color` for reading, `--bs-secondary-color` for supporting text, `--bs-tertiary-color` for metadata only, and never on a hover or overlay layer.

**The accent** is one blue, `--bs-primary`. It fills the primary button, and appears as `--bs-primary-bg-subtle` on a selection and as `--st-focus` on a focus ring. Status colours (success, danger, warning) mean exactly that and are never decoration.

**Radii:** 4 for tags, 6 for buttons and inputs, 10 for cards, 12 for dialogs and menus, 16 for the player frame.

**Motion:** 80 ms for hover and press, 150 ms for small state, 220 ms for menus, 320 ms for dialogs. Easing is `cubic-bezier(.2,0,0,1)`, entrances `(.05,.7,.1,1)`. Only opacity, transform and colour animate. Under reduced motion every duration is 1 ms.

**Type** is the system stack at 14 px for reading. Titles are 600 weight, never heavier. Section labels are 11 px uppercase with letter spacing. Numbers that are compared (cost, time, counts) use tabular figures. The agent's work log is monospace.

## Components

- **Top bar.** 52 px on chrome, a hairline under it. The brand mark, a breadcrumb ("Studio / All collections", or the open collection's name), then ghost controls. On home and All collections the one primary button is New collection; on a collection it steps back to a ghost, because that page's primary is its own Generate.
- **Buttons.** Secondary by default (a surface with an edge). One primary per view. Ghost for chrome. Icon-only buttons are 32 px and always carry a name.
- **Cards.** A surface with an edge. Hover strengthens the edge and lifts the card 2 px; no shadow. "Ready" is not shown, because it is the normal state; preparing and failed are.
- **Collections.** Home is one row: the three most recent collections and a New card of the same shape on a dashed edge. A collection's card shows its cover; under the name, a glyph and a count per kind it holds, and Preparing or Failed when something is. In the list view the cover is a small thumbnail at the start of the row, and gives way on a phone.
- **Covers.** Every collection has one, designed from what it holds: its title, its sources and the titles of what was made from them. A small model call picks a subject, a few key terms, a glyph, one of eight accents and one of three layouts; the server draws it as a static 1600 by 900 page, and a free cover drawn from the collection's id and title stands in until then (or always, with covers off in Settings). A cover is built from the app's own parts, not a look of its own: the page with the map canvas's dot grid, the glyph in a rounded tile like a Create tile's, the terms as chips, a small uppercase label, the subject as a 600 title, and in one layout a card with a hairline. Every colour is a theme token from `theme.css`, embedded in the page and switched by `data-bs-theme`, so a cover follows the viewer's theme and is fetched again when it changes. The one colour of its own is the accent, the studio's blue or one of seven hues beside it, on the glyph's tile and a short rule; white reads on every accent at 4.5:1 and every accent stands at 3:1 on the page and card surfaces of both themes. Sizes and radii are set for the card, where the canvas shows at about a sixth: the subject's cap height stays at 9 px or more, a chip's radius reads as 4 and a card's as 10. The app shows a cover as a lazy, sandboxed frame scaled to its box, hidden from assistive technology and out of the Tab order, because the card around it is the link and carries the name; the box shimmers until the frame paints, then stops. A card's ⋯ has Regenerate cover, which veils the cover with a spinner while it works. The collection page shows the cover small beside its name, and drops it when the column is too narrow.
- **A collection.** Sources on the left as chrome, the Studio in the centre as content, the open map or notes on the right. Sources come in as links, text, a researched topic or files; files dragged over the panel get a dashed primary edge on the primary subtle tint, and each one is a row with a spinner until it is read or says why not. The Studio is four equal tiles; a tile's options open in place under them, never in a dialog, with one primary Generate. Under them, everything made, newest first and every kind mixed, one row each: preparing rows show their live step and a thin bar, failed rows their reason and Retry; every row's ⋯ renames or deletes it. Nothing waits on a build.
- **The Ask tab.** Beside the Studio. The conversation is content. The studio's own words are plain text on the page; the person's are a bubble. The agent's steps are mono lines: a status glyph, the action, its result under a corner mark, and the running line shimmers. The composer is one field with its send button inside.
- **The map viewer.** The canvas has a faint dot grid like a design canvas, and node colours come from the theme. A PNG export resolves the theme's colours to real ones, so a map exported in light mode is a light map.
- **Citations.** A numbered chip in the answer, with the passage on hover or focus. Under the answer, each source is listed once with the numbers of its passages.
- **Dialogs.** Overlay surface, 12 px radius, a scrim with a light blur, entering from 8 px below over 320 ms.
- **Settings.** A dialog with vertical tabs, one glyph each: Appearance, Generation defaults, Voices, Language, Live conversation, Costs & limits, and Models under a small Advanced heading. Tabs, groups, labels, hints and choices come from the server's catalogue; the page holds no copy. A tab's note sits at its top; rows sit under small uppercase group headings (Host, Second voice, Writing, Chat & answers). Each row is the label, its hint always visible in the secondary colour, the control with its unit beside a number, and, once changed, "Default: X" and a Reset. Every change saves at once and says Saving…, Saved, a caveat, or why it was refused. A model is a choice of the tested ones with their prices, or Custom… for any id the catalogue lists; the price of the one in use is shown. The second voice's rows dim, still editable, while decks use one voice. The slide style is picked from its sample slides, as in the Create panel.
- **Hints that point at Settings.** Where a setting decides what happens, the place it happens says so in one short secondary line, with a link that opens Settings on that tab: "5 slides · about 5 min · Host and Expert · Change defaults in Settings" under a deck's style, the voices under an audio format, "Writing in French · voices are English" as a chip, "Answers in English with Gemini 2.5 Flash Lite. Settings › Models" under the Ask composer, the research time under a typed topic, and the spending limit in the cost line. Hints stay quiet until the settings are read, so they never show a value that is not the one in force. The settings are read once per page and again when the dialog closes. Extras go in a tooltip, never something needed to choose.
- **The player.** The stage is a backdrop. Captions, the poster's veil and the speaker tag stay dark in both themes, as a video player's do. The poster's veil blurs the slide under it so its title reads over any slide. Each transcript line carries a rule in its speaker's colour.

## How it is held

- `theme.rs` tests read `theme.css` and check every pairing the app uses, in both themes. Text pairs must reach 4.5:1; focus rings, voice rules and map links must reach 3:1. They also check that light is only ever chosen, never taken from the device, and that both themes set the same tokens.
- `look_tests` in the app check four things: every icon it names exists (each Settings tab's glyph included), the index page applies the theme before it paints, `style.css` has no hex colours of its own, and none of its functional colours but the listed veils over media.
- `look_tests` in the server check that every icon the player names is in the sprite it is served with, and that the page fills its theme placeholders.
- `every_map_colour_is_a_theme_token` checks that the mind map draws only with theme tokens.
