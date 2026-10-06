# OpenNotebook, mind map specification

A mind map of a collection's sources, on its collection page. The person clicks the Mind map tile, gets a tree of the topics their sources cover, and clicks any topic to ask the chat what the sources say about it, with citations back to the sources.

It is modelled on NotebookLM's mind map, which Google renamed Gemini Notebook in July 2026. Where this spec copies NotebookLM it says so and names the evidence; where it departs, it says why.

## How to read this one

The labels are the same as in the [phase 2 spec](phase2-spec.md):

- **Measured.** A number produced on this box by something that ran.
- **Decided.** A judgement, with the alternatives named so it is cheap to reopen.

A third label appears in section 1 only:

- **Observed.** What NotebookLM itself does, read from its shipped code or its help pages. Google has published nothing on how the feature was built, so nothing here is "Google says".

All six steps of section 11 are built, and section 8 holds what was measured. The amendments at the end say what changed from this plan while building it, and why.

## 0. Decisions locked

| Decision | Choice | Basis |
|---|---|---|
| Where it lives first | The Create page's Studio panel, on a draft. The player gets it later. | Decided by the owner, 2026-10-02. The Create page is the notebook: sources on the left, chat in the middle, outputs on the right. |
| What the model writes | An indented `- ` outline, parsed into a tree | Decided by the owner. NotebookLM asks for JSON and repairs trailing commas on the way in, which is a sign its model breaks JSON. The small models this studio runs break it more often, and `parse_plan` already shows a decorated outline parses reliably. |
| What is stored | NotebookLM's shape: `{name, children}` | Observed. Keeping its shape costs nothing and makes an import or export of one trivially compatible. |
| How it is generated | One model call over the selected sources' full text | Decided. The largest real draft on this box is 189,870 bytes (measured), about 50k tokens, which fits whole in the agent model's context. A map-then-merge pass is only needed for drafts far bigger than any seen. |
| The model | A new setting, `OPENNOTEBOOK_MINDMAP_MODEL`, defaulting to the chat agent's model, `google/gemini-2.5-flash-lite` | Decided. The script model's default, `amazon/nova-micro-v1`, has too small a window for a whole draft. |
| Are nodes grounded | Yes, checked: a node whose words appear nowhere in the sources is dropped and counted | Decided, and a departure. NotebookLM enforces nothing; its nodes carry no citations and grounding rests on the prompt alone. |
| What a click asks | NotebookLM's exact sentence (section 1) | Observed. |
| How the answer is grounded | Excerpts from the staged files, numbered, cited as `[n]` | Decided by the owner: citations are in v1. A draft has no retrieval index until it is built, so `session_ask` cannot serve it. |
| Where maps are stored | `<data dir>/mindmaps/<cid>/<id>.json` | Decided, forced. The Rust server's `read_resources` ingested every file in the staging directory, so a map stored there would have become a source of the build. The new server keeps maps in the `mindmaps` table (`MindMap` in `server/opennotebook/db/models.py`). |
| Rendering | Plain SVG from React, laid out by a pure TypeScript function (`layout` in `web/src/ui/mindmapLayout.ts`). No D3, no markmap. | Decided. The UI is offline first and takes no drawing library. The layout NotebookLM uses is small enough to write and test in TypeScript. |
| Editing | Not in v1 | Decided. NotebookLM has none either. See section 9. |

## 1. What NotebookLM does

The best evidence is not a Google post. The open source [notebooklm-py](https://github.com/teng-lin/notebooklm-py) client records NotebookLM's network traffic for its tests, and those recordings contain the mind map's own minified code. A copy was read for this spec.

**The data.** A recursive tree, `{"name": "...", "children": [...]}`, with no `children` on a leaf. No node carries a citation or a source offset. The viewer strips trailing commas before `JSON.parse` (`c.replace(/,\s*([}\]])/g,"$1")`), which says the model's output is used close to raw.

**The size.** A real map in the recordings has 37 nodes, a root and three levels below it, labels of 4.4 words on average and 9 at most, and 2 to 5 children per node. A live Android capture in the same repo has 67 nodes at depth 3.

**The generation.** One request, action `"interactive_mindmap"`, carrying the selected source ids, an optional focus prompt (added May 2026, the pencil on the tile asks "What should the topic be?") and a language code. It goes to the same handler as grounded chat. Whether it is one model call or several is not public; one long-context call is the likely reading.

**The layout.** A horizontal tree drawn with D3 (`d3.hierarchy`, `d3.tree().nodeSize([80,30])`, `d3.zoom`):

- Nodes are rounded rectangles, radius 8, around 20px text.
- A small circle on the right of a node shows `>` when collapsed and `<` when expanded.
- Links are curves from the right edge of a parent to the left edge of a child.
- Each depth column is as wide as its widest label plus 60px.
- Fill colours come from `--mindmap-depth-0` to `--mindmap-depth-4`, capped at depth 4.
- Only the root and its children are open at first: `depth >= 1` starts collapsed.

**The controls.** Collapse all, expand all, zoom in (x1.2) and out (x0.8), wheel and drag zoom clamped to 0.1x to 50x, and download as PNG. Clicking a node re-fits the view to it and its children. The tree is keyboard accessible: arrows move, Right expands, Left goes to the parent, Home and End jump, Enter asks. A node reads as "Name, N children".

**The click.** This is the feature. From the shipped code:

```js
var c = b.parent
  ? new sx("Discuss what these sources say about {TOPIC_NAME}, in the larger context of {PARENT_TOPIC_NAME}.")
      .format({TOPIC_NAME: b.data.name, PARENT_TOPIC_NAME: b.parent.data.name})
  : new sx("Discuss what these sources say about {TOPIC_NAME}.").format({TOPIC_NAME: b.data.name});
a.notebookAppApi.askQuestion(c);
```

Only the direct parent is named, not the path. Clicking the label always asks, and opens the node if it was closed. Clicking the circle on a branch only opens or closes it; on a leaf it asks. The question lands in the ordinary chat, which answers with numbered citations.

**What people complain about.** It cannot be edited. It sprawls with many sources. It sits in a narrow column. The only export is a PNG.

## 2. What the studio already has

- **The tile.** The Studio panel already lists Mind Map as "soon". It is now a working output in `web/src/ui/collection/Studio.tsx`.
- **The sources.** One Markdown file per source in `<data dir>/staging/<cid>/`, written by the `sources` domain.
- **The id.** The draft's sid is the session's sid after the build, so a map stored under it still belongs to the session later.
- **Retrieval without a build.** `grounding.excerpts()` cuts documents into paragraph pieces, drops navigation, and keeps the pieces sharing the most words with a query. It returns text only; section 5 needs it to say which document a piece came from.
- **Word matching.** `grounding.terms()` lowercases a query, keeps words of three letters or more and drops common ones. Section 3's grounding check reuses it.
- **A tolerant outline parser.** `parse_plan` in `server/opennotebook/script/generate.py` already reads a model's decorated outline. Section 3's parser follows its approach.
- **The language rule.** `settings.language_rule()` is what every other prompt appends.

What is missing: nothing on the Create page answers a question from the sources. The chat agent has `web_search`, `deep_research`, `add_sources` and `start_build`, and none of them reads what was gathered.

## 3. Generation

A new module, `server/opennotebook/script/mindmap.py`, with one public function:

```python
async def generate_map(
    sources: list[NamedDoc],
    root_hint: str,
    focus: str | None,
    *,
    model: str,
    language_rule: str,
) -> MindMap
```

**The prompt.** System, in the studio's voice:

- Return a mind map of the material as an indented outline, two spaces per level, every line starting with `- `.
- The first line is the root: the subject of the material, in at most 8 words.
- Under it, 3 to 6 main topics. Under each, 2 to 5 subtopics. At most 4 levels below the root.
- Every label is a short noun phrase of at most 6 words. No sentences, no numbering, no trailing punctuation.
- No two siblings say the same thing.
- Cover only what the material says. Do not add topics from your own knowledge.
- When a focus is given, build the map around it and leave out what does not bear on it.
- The language rule.

User: each source as `SOURCE <n>: <title>` followed by its text, then the focus if there is one.

**The parser.** Indent depth from leading spaces, normalised to the smallest step seen, so a model that indents by 4 or by tabs still parses. Bullets `- `, `* `, `• `, `– ` and numbered `1.` are all accepted and stripped, as are `**`, `#` and a trailing colon or full stop. A line that is not a bullet is a root candidate only if no root is set yet; otherwise it is skipped.

**The clean up, in this order.**

1. Drop empty labels and fit each label to 48 characters with `budget.fit`.
2. Merge siblings whose labels are equal ignoring case and punctuation, keeping the first and appending the second's children.
3. Cut everything below depth 4.
4. The grounding check: a node is kept when at least one of its `terms()`, or that term's stem (`costs` also matches `cost`), appears in the lowercased source text. A node with no terms at all (every word short or common) is kept. A node is dropped only when it and everything under it is unmentioned: "Key ideas" over two real topics is an organising heading, not an invention. The root is never dropped. The count of dropped nodes is stored.
5. Each node keeps at most 8 children, so no column grows taller than a screen.
6. If fewer than 2 branches remain under the root, ask once more with a note that the last answer was too thin, and keep whichever answer had more branches. A map with one branch is still returned; a map with none is an error.

The check is skipped when the studio language is not English, because translated labels cannot be matched word for word against the sources and every node would be dropped. The map records that it was not checked.

A reply cut off at the model's token ceiling is kept up to the cut, because a map missing its last branch is better than no map. This is the opposite of the outline, which refuses a cut reply because there a cut loses whole slides.

**Size.** Above 600,000 characters of source text, each source is cut to its best excerpts against the draft's title with `excerpts()`, so that the call stays under about 150k tokens, and the stored map says it was built from excerpts. Decided: no draft on this box comes near that, and a map-then-merge pass is real work for a case nobody has hit.

## 4. Storage and API

A new JSON-RPC domain, the `mindmap` methods in `server/opennotebook/api/rpc_methods.py`, served at `/api/mindmap/rpc`. The chat agent and any JSON-RPC client call the same methods, so the comments are written for an agent.

```
MindNode = {
  name:      str
  children:  [MindNode]   # empty on a leaf
}

MindMap = {
  id:          str        # unique within its sid
  sid:         str        # the draft, and later the session
  title:       str        # the root's name
  focus:       str        # what the person asked it to centre on; empty when nothing
  sources:     [str]      # the source names it was built from
  excerpted:   bool       # true when the sources were cut to excerpts to fit
  model:       str
  created_ms:  u64
  node_count:  u32
  dropped:     u32        # nodes removed because the sources never mention them
  unchecked:   bool       # the grounding check did not run: the map is not in English
  root:        MindNode
}

MindMapSummary = { id: str  title: str  focus: str  node_count: u32  created_ms: u64 }

MindMapCreateReq = {
  sid:       str
  focus?:    str          # a topic to centre the map on, in the person's words
  sources?:  [str]        # source names from source_list; every source when absent
}

MindMapRef = { sid: str  id: str }

service MindMapService {
  mindmap_create(req: MindMapCreateReq) -> MindMap               # Build a mind map of a draft's sources. Takes 10 to 30 seconds.
  mindmap_list(sid: str)                -> [MindMapSummary]      # The maps of a draft, newest first.
  mindmap_get(req: MindMapRef)          -> MindMap               # One map in full.
  mindmap_delete(req: MindMapRef)       -> bool                  # Delete a map. False when there was no such map.
}
```

`MindNode` is recursive. Whether the oschema macro accepts a self-referencing type has not been tried. If it refuses, the wire carries the tree as a JSON string in a `tree: str` field and the UI parses it; that is the fallback, and the first build step finds out which one applies.

**On disk.** One JSON file per map, `<data dir>/mindmaps/<cid>/<id>.json`, the id being `m<epoch ms>`. Writes go to a temporary file and are renamed into place, so a crash never leaves half a map. `session_delete` does not remove maps, for the same reason it leaves the deck and the WAVs: each cleanup is its own job.

**In the request.** `mindmap_create` runs in the request, the way a chat turn does. It is one model call and its output is small; a prep job would add a poll loop for a wait of seconds.

## 5. Asking from a node

### The question

The UI builds NotebookLM's sentence, unchanged:

- Root: `Discuss what these sources say about {name}.`
- Any other node: `Discuss what these sources say about {name}, in the larger context of {parent}.`

It appears in the chat as the person's own message, so the conversation reads the same as if they had typed it.

### The answer path

A new method on the `sources` domain, because it reads a draft's sources and an agent wants it as a tool:

```
Citation = {
  n:       u32   # the number used in the answer, [1], [2]
  name:    str   # the source's file name, as in source_list
  title:   str
  url:     str   # empty for a note or a research report
  excerpt: str   # the passage the answer drew on
}

SourceAskReq = {
  sid:       str
  question:  str
  sources?:  [str]   # source names; every source when absent
}

SourceAskResult = {
  answer:     str          # Markdown, with [n] markers
  citations:  [Citation]   # only the ones the answer actually cites, in order of first use
}

source_ask(req: SourceAskReq) -> SourceAskResult   # Answer a question from a draft's sources, citing them. Use it whenever the person asks what their material says.
```

How it works:

1. Read the selected staged files.
2. Retrieve with a new `excerpts_from()` beside `excerpts()`, returning each piece with the index of its document. Keep 8 pieces.
3. Number the pieces 1 to 8 and give each to the model as `[n] (title) text`.
4. System prompt: answer from the numbered passages only; after each claim put the number of the passage it rests on, `[2]` or `[2][5]`; when the passages do not cover the question, say so in one sentence; a few short paragraphs or a short list; the language rule.
5. Parse the reply for `[n]`. A number outside 1 to 8 is removed from the text. The citations returned are the cited pieces only, renumbered in order of first use so the person sees `[1]` before `[2]`.

The chat agent gets the same function as a fifth tool, `ask_sources`, so "what do my sources say about the cost" works by typing as well as by clicking.

### Citations in the chat

A message gets an optional `citations` list. The Markdown renderer turns each `[n]` into a small numbered chip. Hovering or focusing a chip shows the source title and the excerpt; clicking it opens the url in a new tab when there is one. Under the message, the cited sources are listed once each by title. Decided: the excerpt in a popover rather than a side panel, because the Studio panel is already holding the map.

## 6. The viewer

### The tile

The Mind Map tile stops being "soon". Clicking it opens a small form with one optional field, "Focus on a topic", and a Create button, mirroring NotebookLM's pencil. Made maps are listed under the tile, newest first, each with its title, node count and a delete button. While one is being made, its row shows a spinner and "Reading your sources".

### Opening a map

The Studio panel widens to take the space of the sources panel, which folds to a narrow strip that reopens it. The chat stays where it is, so an answer arrives beside the map that asked for it. Closing the map restores the three columns.

### Layout

A pure function in the UI crate, so it is testable without a browser:

```rust
fn layout(root: &MindNode, open: &HashSet<Path>, measure: &dyn Fn(&str) -> f32) -> Layout
```

- `Path` is the list of child indexes from the root, which survives a re-render where a pointer would not.
- Column x: the sum, over the shallower depths, of each depth's widest visible label plus 60px.
- Row y: visible leaves take consecutive rows 40px apart, in order; a parent sits at the midpoint of its first and last visible child.
- Links: a cubic curve from the parent's right edge to the child's left edge, with control points halfway across the gap.
- Node box: label width plus 24px, 32px tall, radius 8.
- Text width comes from a canvas `measureText` with the page's font, cached per label. The fallback is 0.55 of the font size per character.

### Interaction

- The root and its children start open, as in NotebookLM.
- Clicking a label asks (section 5) and opens the node if it was closed.
- Clicking the circle opens or closes a branch, or asks on a leaf.
- Drag pans. Wheel and pinch zoom around the pointer, clamped to 0.1x to 50x.
- Clicking a node animates the view to fit it and its children, over 300 ms.
- Toolbar: expand all, collapse all, zoom in, zoom out, fit, and export.
- Colours: `--mm-depth-0` to `--mm-depth-4` and `--mm-link`, defined for light and dark, readable at WCAG AA, held there by a test the way `test_every_kit_is_readable` in `server/tests/builds/test_slides.py` holds the kits' palettes.

### Keyboard and screen readers

`role="tree"` on the SVG group, `role="treeitem"` with `aria-expanded` and `aria-level` on each node, one roving tab stop. Up and Down move between visible nodes, Right opens or moves to the first child, Left closes or moves to the parent, Home and End jump, Enter asks. Each node is labelled "Name, N subtopics".

### Export

- **PNG.** The SVG is serialised with its computed styles inlined, drawn on a canvas at 2x, and saved as `<title>.png`.
- **Markdown.** The tree as an indented `- ` outline, the same format the model writes.
- **OPML.** For mind map and outliner apps.

Markdown and OPML are a departure: NotebookLM offers a PNG only, and its users ask for more.

## 7. Failure modes

| What happens | What the person sees |
|---|---|
| The draft has no sources | The tile is disabled with "Add a source first", the same rule as the build button |
| The model call fails or times out (60 s) | The row says why, in the provider's words, with a Retry button. Nothing is stored. |
| The reply parses to a root and nothing else, twice | It is not stored; the row says "The sources did not give enough to map. Try a focus, or add a source." |
| Every node is dropped by the grounding check | Same as above. A map whose nodes are all unsupported is worse than no map. |
| `source_ask` finds no passage sharing a word with the question | The first pieces of the first source stand in, which is what `excerpts()` already does, and the model is told it may say the sources do not cover it |
| The model cites nothing | The answer is shown without chips and a note under it says it has no citations |
| A source is removed after a map was made | The map stays. Its `sources` list still names the removed one, and the list under the tile marks that map "built from a source no longer here". |

## 8. Cost and speed

Measured on 2026-10-02, on this box, against `google/gemini-2.5-flash-lite` through OpenRouter. The price is OpenRouter's catalog: $0.10 per million input tokens, $0.40 per million output, 1,048,576 token window. Token counts are estimated from characters at about four to a token; nothing on the call path reports usage yet.

| Call | Draft | Time | Result | Cost |
|---|---|---|---|---|
| `mindmap_create` | 5,495 bytes | 1.36 s | 23 nodes, 3 levels, 0 dropped | about $0.0002 |
| `mindmap_create` | 189,870 bytes, run 1 | 2.68 s | 59 nodes, 3 levels, 7 branches, 0 dropped | about $0.005 |
| `mindmap_create` | 189,870 bytes, run 2 | 3.08 s | 75 nodes, 4 levels, 5 branches, 0 dropped | about $0.005 |
| `source_ask` | 5,495 bytes | 1.65 s | every claim cited | about $0.0003 |
| `source_ask`, one question asked four times | 189,870 bytes | 15.4, 10.4, 12.0 and 2.1 s | 5 passages cited | about $0.0004 |
| `source_ask`, another question | 189,870 bytes | 1.55 s | 6 passages cited | about $0.0004 |

A map of the whole 190 KB paper takes about 3 seconds, so the excerpt path in section 3 is not needed for any real draft. The maps are NotebookLM's size: its recorded maps have 37 to 67 nodes.

The answer time varies from 2 to 15 seconds for the same question with the same 8 passages, so the spread is the provider's and not the studio's. It matters for the click: a person can wait over ten seconds with only the "Reading your sources" line moving. Streaming the answer is the fix, and it is the first follow-up.

Neither call is in the session cost estimate, because neither is part of a build.

## 9. Non-goals, explicitly out

- **Editing nodes.** Renaming, moving and deleting nodes is the top complaint about NotebookLM's map. It is still out of v1: it needs an undo story and a decision about what a regenerate does to edits. It is the first candidate for v2.
- **The player.** A Map tab in the player rail, asking through `session_ask`, comes after v1. The storage key is the sid, so it needs no migration.
- **Expanding a node by asking the model for more children.** Some open clones do this. It makes the map a different object every time it is opened.
- **Map then merge for very large drafts.** See section 3.
- **Building a session from one branch.** A natural next step, and out.

## 10. Known traps, as assertions

Each of these is a test, written before the code it guards.

- An outline indented by 4 spaces, by tabs, or by a mix of 2 and 4 parses to the same tree as the 2 space version.
- `**Bold label**:`, `1. Label`, `* Label` and `- Label.` all parse to `Label`.
- Two siblings `Cost` and `cost.` merge into one, and the second's children survive under it.
- A node at depth 5 is cut; its depth 4 parent stays.
- A node named `Quantum Entanglement` is dropped from a map whose sources never mention either word, and its children go with it. `dropped` counts all of them.
- An unmentioned node with a mentioned child is kept, and only its unmentioned children go.
- A reply that is several top-level bullets with no root gets its root from a `# Title` line above them, and from the hint when the line above is "Here is a mind map".
- A map is never written into the staging directory. The test builds a map for a draft and checks `read_resources` on that draft returns only its sources.
- The click sentence for a root has no "in the larger context" clause, and for a child names the parent only, never the grandparent.
- A reply citing `[9]` against 8 passages has the `[9]` removed, and no citation 9 is returned.
- Citations come back renumbered in order of first use: a reply citing `[5]` then `[2]` returns them as 1 and 2, and the text agrees.
- Layout: no two visible node boxes overlap, every child is to the right of its parent, and a parent's y is between its first and last child's.
- Layout: closing a node removes exactly its descendants from the visible set and moves no node above it.
- Every depth colour is at least 4.5:1 against its text in both themes.

## 11. Sequence, for building

1. `server/opennotebook/script/mindmap.py`: prompt, parser, clean up, grounding check, and the section 10 parser tests.
2. Find out whether the oschema macro takes a recursive `MindNode`. Then the `mindmap` domain, the on-disk store, and the staging directory test.
3. `excerpts_from()`, `source_ask`, citation parsing and renumbering, their tests, and the `ask_sources` agent tool.
4. The layout function and its tests.
5. The UI: the tile and list, the viewer, pan and zoom, keyboard, the click into chat, citation chips, and export.
6. Measure, on the 189,870 byte draft and on the 5,495 byte one: time to a map, tokens in and out, cost, node count, how many nodes the grounding check dropped, and time to a `source_ask` answer. Replace section 8 with what came back, and add the numbers here as an amendment.

## 12. Open questions for the owner

- **Should a build use the map?** The outline step could take the map's main topics as its parts, so the session covers what the person saw in the map. It would tie the two features together and change how sessions are planned, so it is not assumed.
- **One map per draft or many?** This spec allows many, as NotebookLM does. One, regenerated in place, would be simpler to show.

## Amendments

### 2026-10-02, step 1 built

`server/opennotebook/script/mindmap.py`: the prompt, the parser, the clean up and the grounding check. Its tests are in `server/tests/test_mindmap.py`. The model call goes through `generate.send()` in `server/opennotebook/script/generate.py`, so the map can use its own model setting, `OPENNOTEBOOK_MINDMAP_MODEL`, which is on the Models tab. Building it settled three things section 3 had not: an organising label over real topics is kept, the check is skipped for a map that is not in English, and a reply cut at the token ceiling is kept. Section 3 now says so. Nothing has called a real model yet; that is step 6.


### 2026-10-02, step 2 built

The `mindmap` domain is served at `/api/mindmap/rpc` with all four methods, and maps are stored in `<data dir>/mindmaps/<cid>/`. The recursive `MindNode` works on the wire, so the JSON string fallback in section 4 was not needed. The macro flattens a returned struct into the output type, so `mindmap_create` and `mindmap_get` convert through JSON (`flat()`), with a test that fails if the two drift. One live call on the 5,495 byte draft returned in 1.36 s: 23 nodes, 3 levels, 0 dropped. The model wrote the main topics at the top level without a root, so the root fell back to the source's title, "[2410.00037] Moshi". That fallback works but reads poorly, and the prompt should be tightened in step 6.

### 2026-10-02, step 3 built

`source_ask` is on the sources domain and `ask_sources` is a tool of the chat agent. The prompt and the citation parser are `server/opennotebook/script/cite.py`, tested in `server/tests/test_ask.py`. Retrieval is `grounding.excerpts_from()`, which is `excerpts()` plus the index of the source each passage came from. Reading a collection's sources is `read_docs()` in `server/opennotebook/domain/reading.py`, shared by ask, mind maps and study notes.

The answer uses the agent's model, the `CHAT_MODEL_KEY` setting in `server/opennotebook/domain/settings.py`, whose default is `AGENT_MODEL_DEFAULT`, rather than the script model, too small to follow the citation rule.

`ask_sources` ends the agent's turn. Its answer goes to the person as the reply, with a `citations` field on the `reply` event, instead of going back to the model. Handed back, it would come out paraphrased, and a paraphrase drops the markers.

Measured on the 5,495 byte draft: NotebookLM's click sentence for "Inner Monologue method" under "Real-time dialogue framework" was answered in 1.65 s, with every claim cited to the arXiv abstract. The same question in plain words through the chat agent called `ask_sources` on its own. One side effect to fix in step 5: the draft's working title, which the agent's `state` event derives from the conversation, came out as the question itself.

### 2026-10-02, step 4 built

The layout is `layout()` in `web/src/ui/mindmapLayout.ts`, kept apart from the React view so it is tested on its own, in `web/src/ui/mindmapLayout.test.ts`. It takes the wire `MindNode` directly. Beside it, because they are pure too: `questionFor()`, NotebookLM's click sentence, `boundsOf()` for fitting the view to a clicked node, and the Markdown and OPML exports (`toMarkdown()`, `toOpml()`). The tests cover every layout and click assertion in section 10.

Sizes differ slightly from section 6 and are constants at the top of the file: rows 44px apart, boxes 32px tall with 14px either side of the label, 22px more on a branch for its open and close circle, 15px labels.


### 2026-10-02, step 5 built

The viewer, the click into the chat and the citation chips are `MindMapView` in `web/src/ui/mindmap.tsx`; the tile is in `web/src/ui/collection/Studio.tsx`. Checked in a real browser, not only compiled:

- The tile makes a map on a fresh draft, and the map opens on its root and children, 5 nodes.
- Expand all shows all 24 nodes.
- Clicking "Core approach" sent `Discuss what these sources say about Core approach, in the larger context of Moshi speech-text foundation model.` and got an answer with 4 citation chips and a source list.
- Focusing a chip shows the source's title and the passage, and a chip of a page links to it.
- Arrow keys move the tree's focus.
- At 390 px there is no horizontal overflow, and the browser console stayed empty.
- The three exports produce real files: a 945 by 536 PNG with a valid header, the Markdown outline and well-formed OPML.

Three departures from section 6, decided while building:

- A click calls `source_ask` directly, not the chat agent. It is NotebookLM's behaviour, and it means a click can never become a search or a build the model chose instead.
- The depth colours are written onto the SVG as attributes, not set in the stylesheet. The app's palette is dark only, so the five fills are picked for a dark page and held to WCAG AA by `every_depth_reads_at_aa` in the SDK. Attributes also make the PNG export come out the same as the screen.
- With a map open, the sources panel folds to a strip labelled Sources. Pressing it closes the map, rather than opening the sources beside it.

Not done: pinch zoom on a phone, re-fitting the view when the pane changes size, and keeping a chip's popover inside the chat when the chip is at the top of it.

### 2026-10-02, step 6 measured

Section 8 now holds the measurements. The prompt's root rule was tightened, because the first live call came back without a root and fell back to the source's title: the root is now "the only line with no indentation; every other line is indented under it". It helped but did not fix it. The two 189,870 byte runs wrote their own root, but on the 5,495 byte draft, 1 of 3 runs after the change still wrote no root, fell back to the source's title, and came back thin, with 6 nodes against 20 for the other two. A thin map passes the two-branch check today. Next: retry when a map has fewer than about 12 nodes as well as fewer than 2 branches, and keep the larger map.


### 2026-10-02, a mind map is an output, not part of the build

The Studio panel is now a choice between outputs: Narrated Session or Mind Map, one chosen at a time, the chosen one highlighted with its options under it (the style picker, or the map's optional focus). The bar under the chat acts on the choice. For a session it is unchanged: the paid-model notice, the breakdown and Start building. For a map it says what one map costs and has one button, Create mind map, because a map is one cheap call and not a build: no breakdown, no spending limit, no paid-build notice.

The price comes from a new `mindmap_estimate(sid)`, which makes no model call: the sources' characters at four to a token plus about 500 for the prompt, capped where the generator switches to excerpts, and 1,000 tokens out, priced from the same cached model price catalog the session estimate reads. It gives one call as the usual cost and two as the high end, for the retry a thin map triggers. On the 5,520 character draft it says about $0.00059.

The bar wraps by design now, because it shares the chat column, which narrows when a map is open: the text takes the room and wraps, and when even its minimum does not fit the buttons move under it, right-aligned, labels never squeezed. Checked in a browser at 1600, 1280 and 1100 px with a map open: no overflow, every button whole.

### 2026-10-02, one id for every output, a New dialog, and the home page

This replaces the Studio panel choice of the amendment above. Researched first against NotebookLM, Gamma, Canva, Figma, Google Drive and Microsoft 365 Copilot; what was taken from them is named here.

**"+ New" opens a dialog of what can be made, and the create page that follows makes only that.** Each kind is one row: icon, name, one line, and its time and cost (a few minutes and a few cents, against a few seconds and under a cent: the two differ about a hundredfold, and none of the products studied says so before the click). Kinds not built yet are listed after, dimmed, labelled Soon, and out of the keyboard's way. The rows are a radio group in a modal after the WAI-ARIA pattern: arrow keys move, Enter opens, Esc closes. Each kind has its own link, `/ui/new-session` and `/ui/new-mind-map`, the way figma.new opens one kind of file.

**The create page asks for that kind's options once, at its top, and has no Studio column.** A strip over the chat names the kind and holds its one option: the slide style for a session, the focus for a map. The options were moved out of the dialog because asking them in the dialog and again on the page is the double-ask every studied product avoids. The right column now only ever holds an open map. A map page's maps are listed under its sources. The chat agent is told the kind: on a map page it has no `start_build`, and says to press Create mind map.

**Every output of one set of sources shares its sid, and the home page shows that.** NotebookLM keeps one notebook of many outputs; Figma and Drive filter by type. Home now has type chips, All, Narrated sessions, Mind maps and Drafts, with counts; a Mind maps section of map cards, whose cover is a small tree so the kind reads from the shape and whose label says which session or draft it came from; and on every session card that has maps, a "◉ N maps" link that opens them on that sid's page with its sources. A built session's sources are read back from the server for that, so a map opens the same from any browser. New server call: `mindmap_list_all()`, and `MindMapSummary` carries its `sid`.

**Thin maps, found and fixed.** Logging the raw reply of a thin map showed the model writing the right topics with no indentation at all, so the hierarchy was lost before parsing. The prompt now shows the exact nested shape in three lines, and a map under 12 nodes is asked for once more, keeping the larger. Measured on the 5,495 byte draft: before, about 1 in 3 first attempts came back flat; after, 15 of 15 came back nested, 13 to 27 nodes.

Checked in a real browser: the chips and their counts, the filters, the dialog by keyboard, a map made on a new map page, the new draft and its map on home, a session's maps opened from its card, and the session page's style picker.

### 2026-10-03, every map has its own id and address

A map's id is now a UUID, made when the map is made, the way each session has its own sid and player address. Maps made before keep their `m<digits>` id, and `safe_id` accepts both, but only the canonical lowercase UUID spelling, so one map has exactly one file name. An open map has its own address in the app, `/ui/mind-map/<sid>/<id>`: reloading it, or opening it in another browser, lands on that map with the sources of its sid. Closing the map puts the page's own address back. The address is replaced, not pushed, so opening a map is not a page the Back button has to walk through.

`MindMapSummary` now carries `shape`, the number of subtopics under each main topic, so a map's cover on home is its real outline rather than a picture repeated.

With a map open, the sources fold to a strip. Pressing it opens the sources beside the map, which stays where it is; a fold button puts them back. It used to close the map, which read as the map disappearing. Neither way animates.

When a map already covers exactly the current sources and no focus is typed, the bar under the chat no longer offers Create mind map: it says the map is up to date, with Open map when that map is not the one showing. Adding or removing a source, by hand or by the agent while chatting, or typing a focus, brings the button back. A focused map does not count as covering the sources as a whole. The check is `covering_map`, tested.
