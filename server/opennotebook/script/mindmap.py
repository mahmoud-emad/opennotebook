"""A collection's sources as a mind map: one model call, an outline parsed
into a tree, and every node checked against the sources before it is kept.
A port of `opennotebook_script/src/mindmap.rs`.

The design and the evidence behind it are in `docs/mindmap-spec.md`. In
short: NotebookLM asks its model for a `{name, children}` JSON tree and
repairs trailing commas on the way in, which says the model breaks JSON. The
small models this studio runs break it more often, so the model writes an
indented `- ` outline instead, and the tree is built here. The stored shape
is still NotebookLM's.

NotebookLM enforces nothing about where a node came from. This module does:
a node whose words appear nowhere in the sources, and none of whose
descendants appear either, is dropped and counted.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from opennotebook.script import budget, generate, grounding
from opennotebook.script.errors import Empty, Truncated

log = logging.getLogger(__name__)

# Levels below the root. NotebookLM's real maps go three or four deep.
MAX_DEPTH = 4

# Children kept per node. The prompt asks for 2 to 6; a model that writes a
# list of fifteen would make a column taller than any screen.
MAX_CHILDREN = 8

# A map with fewer nodes than this is asked for once more, and the larger of
# the two is kept. Measured: on a 5,495 byte source, 1 run in 3 came back with
# 6 or 7 nodes where the others had 20. A source too small for 12 still gets
# whichever map was larger, so this never refuses a map.
MIN_NODES = 12

# The longest label kept, in characters. Six words is about 40.
LABEL_CHARS = 48

# Above this much source text the sources are cut to excerpts. About 150k
# tokens; the largest real collection is 189,870 bytes.
WHOLE_TEXT_CHARS = 600_000

STAGE = "mind map"


@dataclass
class NamedDoc:
    """One source, as the readers here read it."""

    # The source's name, as the sources list reports it.
    name: str
    title: str
    text: str
    # Where it was read from; empty for a note or a file.
    url: str = ""


@dataclass
class MindNode:
    """One node of the tree. A leaf has no children."""

    name: str
    children: list[MindNode] = field(default_factory=lambda: list[MindNode]())

    def count(self) -> int:
        """Every node, this one included."""
        return 1 + sum(c.count() for c in self.children)

    def depth(self) -> int:
        """Levels below this node: 0 for a leaf."""
        return max((1 + c.depth() for c in self.children), default=0)

    def to_outline(self) -> str:
        """The tree as the outline the model writes: two spaces a level, `- `
        before every label. Also the Markdown export."""
        out: list[str] = []
        self._write_outline(0, out)
        return "".join(out)

    def _write_outline(self, level: int, out: list[str]) -> None:
        out.append(f"{'  ' * level}- {self.name}\n")
        for c in self.children:
            c._write_outline(level + 1, out)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "children": [c.to_json() for c in self.children]}


@dataclass
class MindMap:
    """What a generation produced, with what it had to do to get there."""

    root: MindNode
    # Nodes removed because the sources never mention them.
    dropped: int
    # The sources were cut to excerpts to fit one call.
    excerpted: bool
    # The grounding check did not run, because the map is in another language
    # than English and its labels cannot be matched word for word against the
    # sources.
    unchecked: bool
    model: str


async def generate_map(
    sources: list[NamedDoc],
    root_hint: str,
    focus: str | None,
    *,
    model: str,
    language_rule: str,
) -> MindMap:
    """Build a map of `sources` on `model`.

    `root_hint` names the root when the model leaves it out, which happens: a
    model asked for one root often writes the main topics at the top level
    instead. `focus` is the person's own words, or nothing.
    """
    text, excerpted = material(sources, root_hint)
    system = system_prompt(focus, language_rule)
    user = user_prompt(text, focus)
    haystack = "\n".join(s.text.lower() for s in sources)
    # Labels in another language cannot be matched against the sources'
    # words, and every one of them would be dropped. The rule is empty for
    # English.
    check = not language_rule

    best: tuple[MindNode, int] | None = None
    for attempt in range(2):
        asked = (
            user
            if attempt == 0
            else f"{user}\n\nYour last map was too thin: its lines were not indented under each "
            "other, or it had only a handful of topics. Write the root as the first line with "
            "no indentation, every main topic two spaces in, and every subtopic four spaces "
            "in, with 3 to 6 main topics and their subtopics."
        )
        # A reply cut at the token ceiling is still a tree up to the cut, and
        # a map missing its last branch beats no map.
        try:
            raw = await generate.send(model, system, asked, STAGE)
        except Truncated as e:
            if not e.text.strip():
                raise
            raw = e.text
        tree = parse_outline(raw, root_hint)
        if tree is None:
            continue
        tree, dropped = clean(tree, haystack if check else None)
        enough = not thin(tree)
        if not enough:
            # A thin map is the one failure the model has shown here; its raw
            # reply is what says whether the model or the parser lost the rest.
            log.warning(
                "mind map attempt %d came back thin (%d nodes); reply starts:\n%s",
                attempt + 1,
                tree.count(),
                raw[:800],
            )
        if best is None or tree.count() > best[0].count():
            best = (tree, dropped)
        if enough:
            break

    if best is None or not best[0].children:
        raise Empty(STAGE)
    return MindMap(best[0], best[1], excerpted, not check, model)


def thin(tree: MindNode) -> bool:
    """Too little to be worth showing without asking once more."""
    return len(tree.children) < 2 or tree.count() < MIN_NODES


def material(sources: list[NamedDoc], query: str) -> tuple[str, bool]:
    """The sources as one block of text, cut to excerpts when they are too
    long to send whole. The second value says whether they were cut."""
    cut = sum(len(s.text) for s in sources) > WHOLE_TEXT_CHARS
    # An even share each, in pieces: a long source should not crowd out a
    # short one that is just as much a part of the collection.
    keep = max(WHOLE_TEXT_CHARS // max(len(sources), 1) // grounding.EXCERPT_CHARS, 1)
    out: list[str] = []
    for i, s in enumerate(sources):
        text = "\n\n".join(grounding.excerpts([s.text], query, keep)) if cut else s.text
        out.append(f"SOURCE {i + 1}: {s.title.strip()}\n\n{text.strip()}\n\n")
    return "".join(out), cut


def system_prompt(focus: str | None, language_rule: str) -> str:
    focus_rule = (
        "\n- A focus is given below the material. Build the map around it and leave out "
        "what does not bear on it."
        if focus and focus.strip()
        else ""
    )
    s = (
        "You make a mind map of source material: the topics it covers and how they break "
        "down, so a reader sees the whole of it at a glance.\n"
        'Write it as an indented outline, two spaces per level, every line starting with "- ". '
        "The indentation is the map: a reply with every line at the same level has no "
        "structure and is useless. Exactly this shape:\n"
        "- Subject of the material\n"
        "  - Main topic\n"
        "    - Subtopic\n"
        "Nothing else: no introduction, no headings, no commentary.\n"
        "Rules:\n"
        "- The first line is the root: the subject of the material, in at most 8 words. It "
        "is the only line with no indentation; every other line is indented under it.\n"
        "- Under the root, 3 to 6 main topics. Under each, 2 to 5 subtopics. At most "
        f"{MAX_DEPTH} levels below the root.\n"
        "- Every label is a short noun phrase of at most 6 words, using the material's own "
        "terms. No sentences, no numbering, no trailing punctuation, no explanations after "
        "a colon.\n"
        "- No two labels under the same parent say the same thing.\n"
        "- Cover only what the material says. Do not add topics from your own knowledge."
        f"{focus_rule}"
    )
    if language_rule:
        s += f'\n\n{language_rule} Keep the "- " bullets and the indentation exactly as described.'
    return s


def user_prompt(material: str, focus: str | None) -> str:
    f = (focus or "").strip()
    return f"Material:\n\n{material}Focus: {f}" if f else f"Material:\n\n{material}"


# ── parsing ───────────────────────────────────────────────────────────────────


def lines(text: str) -> list[str]:
    """`text`'s lines as Rust's `str::lines` gives them: split at `\\n`, a
    `\\r` before it dropped, and no empty line after a final newline."""
    out = text.split("\n")
    if out and out[-1] == "":
        out.pop()
    return [line.removesuffix("\r") for line in out]


def parse_outline(raw: str, root_hint: str) -> MindNode | None:
    """Read a model's outline into a tree.

    Indentation is read relative, not in fixed steps: a line belongs under
    the nearest line above it that is indented less. So two spaces, four, tabs
    and a mix of them all give the same tree, which is what a model's output
    needs.

    The root is the one top-level line when there is one. When the model
    wrote several at the top (the main topics, with the root left out), they
    go under a root named by the last plain line before the outline, or else
    `root_hint`. A plain line is any line that is not a bullet: "Here is the
    map:" before the outline is one, and so is `# Title`.

    None when there is no bullet at all.
    """
    # (indent, label) for every bullet, in order.
    items: list[tuple[int, str]] = []
    heading: str | None = None
    for line in lines(raw):
        trimmed = line.strip()
        # A model that wraps its outline in a code fence: the fence lines are
        # not part of it, what is between them is.
        if trimmed.startswith("```") or not trimmed:
            continue
        indent = 0
        for c in line:
            if not c.isspace():
                break
            indent += 4 if c == "\t" else 1
        rest = strip_bullet(trimmed)
        if rest is not None:
            if label := clean_label(rest):
                items.append((indent, label))
        # A plain line names the root only while no bullet has been seen: a
        # line after the outline starts is commentary.
        elif not items and (label := clean_label(trimmed)):
            heading = label
    if not items:
        return None

    top: list[MindNode] = []
    stack: list[tuple[int, MindNode]] = []
    for indent, label in items:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        node = MindNode(label)
        (stack[-1][1].children if stack else top).append(node)
        stack.append((indent, node))

    if len(top) == 1:
        return top[0]
    name = heading if heading is not None and not looks_like_preamble(heading) else None
    name = name or clean_label(root_hint)
    return MindNode(name or "Sources", top)


def looks_like_preamble(line: str) -> bool:
    """A line such as "Here is a mind map of the material" is about the answer,
    not a title of the material."""
    low = line.lower()
    return any(p in low for p in ("here is", "here's", "mind map", "below is", "outline of"))


def strip_bullet(line: str) -> str | None:
    """The label after a bullet or a list number, or None for a line that is
    not a list item."""
    for b in ("- ", "* ", "• ", "– ", "+ "):
        if line.startswith(b):
            return line[len(b) :]
    # "1. Label" and "1) Label".
    digits = len(line) - len(line.lstrip("0123456789"))
    if digits > 0:
        rest = line[digits:]
        for sep in (". ", ") "):
            if rest.startswith(sep):
                return rest[len(sep) :]
    return None


def clean_label(raw: str) -> str:
    """A label without the decoration a model puts round it: emphasis,
    heading marks, a trailing colon or full stop, and an explanation after a
    colon."""
    s = raw.strip().lstrip("#").strip()
    for mark in ("**", "__", "`"):
        s = s.replace(mark, "")
    # "Cost: what the user pays per month" keeps "Cost". A label that is only
    # the part before the colon is never empty, because of the check.
    head, sep, tail = s.partition(": ")
    if sep and head.strip() and tail.strip():
        s = head
    s = s.strip().lstrip("*_").rstrip("*_:.;, ").strip()
    return budget.fit(s, LABEL_CHARS)


# ── cleaning ──────────────────────────────────────────────────────────────────


def clean(root: MindNode, sources: str | None) -> tuple[MindNode, int]:
    """The tree made safe to draw, and how many nodes the grounding check
    removed.

    In order: siblings that say the same thing are merged, anything below
    `MAX_DEPTH` is cut, each node keeps at most `MAX_CHILDREN`, and, when
    `sources` is given, a node is dropped when neither it nor anything under
    it is mentioned there. The root is never dropped.
    """
    _merge_siblings(root)
    _cut_depth(root, 0)
    dropped = [0]
    if sources is not None:
        root.children = [
            k for c in root.children if (k := _keep_grounded(c, sources, dropped)) is not None
        ]
    _cap_children(root)
    return root, dropped[0]


def _sibling_key(name: str) -> str:
    return "".join(c.lower() for c in name if c.isalnum())


def _merge_siblings(node: MindNode) -> None:
    kept: list[MindNode] = []
    for child in node.children:
        key = _sibling_key(child.name)
        first = next((k for k in kept if _sibling_key(k.name) == key), None)
        if first is None:
            kept.append(child)
        else:
            first.children.extend(child.children)
    node.children = kept
    for c in node.children:
        _merge_siblings(c)


def _cut_depth(node: MindNode, level: int) -> None:
    if level >= MAX_DEPTH:
        node.children = []
        return
    for c in node.children:
        _cut_depth(c, level + 1)


def _cap_children(node: MindNode) -> None:
    node.children = node.children[:MAX_CHILDREN]
    for c in node.children:
        _cap_children(c)


def _keep_grounded(node: MindNode, sources: str, dropped: list[int]) -> MindNode | None:
    """The node with its ungrounded descendants removed, or None when it and
    all of them are ungrounded. An unmentioned label that holds mentioned
    ones is kept: "Key ideas" is an organising heading, not an invention."""
    node.children = [
        k for c in node.children if (k := _keep_grounded(c, sources, dropped)) is not None
    ]
    if not node.children and not mentioned(node.name, sources):
        dropped[0] += 1
        return None
    return node


def mentioned(label: str, sources: str) -> bool:
    """Whether any word of a label is in the sources. A label with no word
    worth matching (all short or common) counts as mentioned: there is
    nothing to check it by.

    A plural or a verb form is matched by its stem as well, so "Costs" is
    found in a text that says "cost".
    """
    words = grounding.terms(label)
    return not words or any(s in sources for t in words for s in stems(t))


def stems(term: str) -> list[str]:
    out = [term]
    for suffix in ("ies", "es", "s", "ing", "ed"):
        if term.endswith(suffix) and len(stem := term[: -len(suffix)]) >= 3:
            out.append(stem)
    return out
