"""Markdown output shared by the format readers: inline runs with emphasis and
links, block layout, and GFM tables.

Text is written as it is, without backslash-escaping Markdown characters. An
escaped `af\\_bella` renders the same but no longer matches the source byte for
byte, and the stored text is what grounding searches. The one exception is a
`|` inside a table cell, which would otherwise split the cell and lose the
table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# A hard line break inside a paragraph. Kept as a plain newline while spans are
# collected and turned into Markdown's two-space break when rendered, or into
# `<br>` inside a table cell.
LINE_BREAK = "\n"


@dataclass
class Span:
    """A stretch of text with one formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    link: str | None = None


def _lead(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _trail(s: str) -> str:
    return s[len(s.rstrip()) :]


def inline(spans: Sequence[Span]) -> str:
    """Render spans as inline Markdown."""
    out: list[str] = []
    i = 0
    while i < len(spans):
        url = spans[i].link
        j = i
        if url is not None:
            # A link spans every consecutive run that points at the same
            # target, so a link whose words are partly bold stays one link.
            while j < len(spans) and spans[j].link == url:
                j += 1
            label = emphasised(spans[i:j])
            core = label.strip()
            if not core:
                out.append(label)
            else:
                out.append(f"{_lead(label)}[{core}]({link_target(url)}){_trail(label)}")
        else:
            while j < len(spans) and spans[j].link is None:
                j += 1
            out.append(emphasised(spans[i:j]))
        i = j
    return "".join(out)


def link_target(url: str) -> str:
    """A URL written so it cannot end the link early: spaces and parentheses are
    percent-encoded, which every Markdown reader turns back into the same
    address."""
    return url.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def emphasised(spans: Sequence[Span]) -> str:
    """Spans with bold and italic markers, merging neighbours of the same style
    first so `**a****b**` comes out as `**ab**`."""
    merged: list[Span] = []
    for s in spans:
        # Whitespace carries no visible emphasis; treating it as unstyled keeps
        # markers from landing next to a space, where they stop working.
        if not s.text.strip():
            bold, italic = (merged[-1].bold, merged[-1].italic) if merged else (False, False)
        else:
            bold, italic = s.bold, s.italic
        if merged and merged[-1].bold == bold and merged[-1].italic == italic:
            merged[-1].text += s.text
        else:
            merged.append(Span(s.text, bold, italic))
    out: list[str] = []
    for run in merged:
        text, bold, italic = run.text, run.bold, run.italic
        marker = ("**" if bold else "") + ("*" if italic else "")
        if not marker:
            out.append(text)
            continue
        # Emphasis applies line by line, since a marker cannot reach across a
        # line break, and only to the words: the surrounding spaces go outside,
        # where they do not stop the marker from closing.
        lines: list[str] = []
        for line in text.split(LINE_BREAK):
            core = line.strip()
            lines.append(f"{_lead(line)}{marker}{core}{marker}{_trail(line)}" if core else line)
        out.append(LINE_BREAK.join(lines))
    return "".join(out)


def plain(spans: Sequence[Span]) -> str:
    """Plain text out of spans, ignoring formatting and links, for places where
    Markdown markup does not belong (headings, slide titles)."""
    return "".join(s.text for s in spans)


def tidy_line(line: str) -> str:
    """Collapse whitespace the way a reader sees it: tabs and runs of spaces to
    one space, kept line by line."""
    return " ".join(line.split())


def paragraph_text(text: str) -> str:
    """Rendered inline text made ready to stand as a paragraph: whitespace tidied
    within each line, empty lines dropped, and the remaining lines joined with
    Markdown hard breaks."""
    return "  \n".join(t for t in (tidy_line(line) for line in text.split(LINE_BREAK)) if t)


def one_line(text: str) -> str:
    """A heading or title on one line: breaks become spaces."""
    return tidy_line(text.replace(LINE_BREAK, " "))


# The output's building blocks. Separating the kinds lets list items sit on
# consecutive lines while everything else is set off by a blank line.


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class Item:
    depth: int
    # The number an ordered item shows, or `None` for a bullet.
    ordered: int | None
    text: str


@dataclass
class Table:
    rows: list[list[str]]


type Block = Heading | Paragraph | Item | Table


def render(blocks: Sequence[Block]) -> str:
    out: list[str] = []
    prev_item = False
    # The list depth actually written for the previous item. A list that jumps
    # from level 0 to level 3 is written one level deeper at a time, because
    # indentation past the parent's content would turn into a code block.
    last_depth: int | None = None
    for block in blocks:
        is_item = isinstance(block, Item)
        if out:
            out.append("\n" if is_item and prev_item else "\n\n")
        if not is_item:
            last_depth = None
        match block:
            case Heading(level, text):
                out.append("#" * min(max(level, 1), 6) + " " + text)
            case Paragraph(text):
                out.append(text)
            case Item(depth, ordered, text):
                depth = 0 if last_depth is None else min(depth, last_depth + 1)
                last_depth = depth
                # Four spaces per level clears the content column of both `- `
                # and `10. ` parents.
                out.append("    " * depth)
                out.append("- " if ordered is None else f"{ordered}. ")
                # Continuation lines of an item are indented to stay in it.
                out.append(text.replace("\n", "\n" + "    " * depth + "  "))
            case Table(rows):
                out.append(table(rows))
        prev_item = is_item
    return "".join(out)


def cell(text: str) -> str:
    """One table cell's text: breaks become `<br>`, whitespace is tidied, and
    pipes are escaped so they stay inside the cell."""
    lines = (tidy_line(line) for line in text.split(LINE_BREAK))
    return "<br>".join(t for t in lines if t).replace("|", "\\|")


def table(rows: Sequence[Sequence[str]]) -> str:
    """A GFM table, first row as the header. Rows are padded to the widest row,
    since GFM needs every row to have the header's columns, and trailing
    columns that are empty in every row are dropped."""

    def used(row: Sequence[str]) -> int:
        return max((i + 1 for i, c in enumerate(row) if c), default=0)

    width = max(max((used(r) for r in rows), default=0), 1)

    def line(row: Sequence[str]) -> str:
        cells = [row[i] if i < len(row) else "" for i in range(width)]
        return "|" + "".join(f" {c} |" for c in cells)

    out = [line(rows[0] if rows else []), "|" + " --- |" * width]
    out.extend(line(r) for r in rows[1:])
    return "\n".join(out)


def finish(md: str) -> str:
    """The last pass over a converter's output: at most one blank line in a row,
    no trailing spaces except a hard break's two, and one final newline."""
    out: list[str] = []
    blank = 0
    # Split on newlines only: `str.splitlines` would also break at form feeds
    # and other separators that are part of the text.
    for raw in md.split("\n"):
        line = raw.removesuffix("\r")
        trimmed = line.rstrip()
        if not trimmed:
            blank += 1
            continue
        if out:
            out.append("\n\n" if blank > 0 else "\n")
        blank = 0
        out.append(trimmed)
        if line.endswith("  "):
            out.append("  ")
    if out:
        out.append("\n")
    return "".join(out)
