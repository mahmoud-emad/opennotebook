"""Word (`.docx`) to Markdown, read from the WordprocessingML parts directly.

The main document is walked in order. Paragraphs become headings, list items or
plain paragraphs depending on their style and numbering; tables become GFM
tables; text boxes, which Word anchors inside a run, are written as their own
blocks right after the paragraph that holds them. Footnotes and endnotes are
appended at the end as Markdown footnotes, referenced from where they were in
the text.

Tracked changes are read the way the document currently reads: deleted text is
left out and inserted text is kept, as if every change had been accepted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from . import markdown
from .kinds import ConvertError, InputKind, Problem
from .markdown import LINE_BREAK, Block, Heading, Item, Paragraph, Span, Table
from .ooxml import Package, Rels
from .xmltree import El, uint

KIND = InputKind.WORD

# How far a chain of `basedOn` styles is followed. Real chains are a few deep;
# the limit only stops a style that is based on itself.
MAX_STYLE_CHAIN = 16


def convert(data: bytes) -> str:
    pkg = Package(data, KIND)
    main = pkg.main_part("word/document.xml")
    document = pkg.read_xml(main)
    if document is None:
        raise ConvertError(KIND, Problem.NOT_VALID)
    rels = pkg.rels(main)

    def part_of(kind: str) -> str | None:
        return next((r.target for r in rels.values() if r.is_(kind) and not r.external), None)

    styles = Styles()
    if (part := part_of("styles")) and (root := pkg.read_xml(part)) is not None:
        styles = Styles.read(root)
    numbering = Numbering()
    if (part := part_of("numbering")) and (root := pkg.read_xml(part)) is not None:
        numbering = Numbering.read(root)
    notes: dict[NoteKind, tuple[El, Rels]] = {}
    for kind, part in ((NoteKind.FOOT, part_of("footnotes")), (NoteKind.END, part_of("endnotes"))):
        if part is None or (root := pkg.read_xml(part)) is None:
            continue
        # Notes have relationships of their own, for links inside them.
        notes[kind] = (root, pkg.rels(part))

    body = document.child("w:body")
    if body is None:
        raise ConvertError(KIND, Problem.NOT_VALID)
    reader = Reader(styles, numbering, rels)
    blocks: list[Block] = []
    reader.blocks(body, blocks)
    out = markdown.render(blocks)

    notes_md = reader.notes(notes)
    if notes_md:
        out += "\n\n" + notes_md
    return out


class NoteKind(Enum):
    FOOT = "foot"
    END = "end"


@dataclass
class Style:
    """What the converter needs from one entry of `styles.xml`."""

    name: str = ""
    based_on: str | None = None
    outline: int | None = None
    numbering: tuple[str, int] | None = None
    bold: bool | None = None
    italic: bool | None = None


def _heading_by_name(name: str) -> int | None:
    if name == "title":
        return 1
    if not name.startswith("heading"):
        return None
    n = uint(name.removeprefix("heading").strip())
    return n if n is not None and 1 <= n <= 9 else None


@dataclass
class Styles:
    by_id: dict[str, Style] = field(default_factory=lambda: {})

    @classmethod
    def read(cls, root: El) -> Styles:
        by_id: dict[str, Style] = {}
        for s in root.children_named("w:style"):
            sid = s.attr("w:styleId")
            if sid is None:
                continue
            ppr, rpr = s.child("w:pPr"), s.child("w:rPr")
            name = s.child("w:name")
            based_on = s.child("w:basedOn")
            outline = ppr.child("w:outlineLvl") if ppr else None
            by_id[sid] = Style(
                name=((name.attr("w:val") if name else None) or "").lower(),
                based_on=based_on.attr("w:val") if based_on else None,
                outline=uint(outline.attr("w:val")) if outline else None,
                numbering=num_pr(ppr) if ppr else None,
                bold=toggle(rpr, "w:b") if rpr else None,
                italic=toggle(rpr, "w:i") if rpr else None,
            )
        return cls(by_id)

    def chain(self, sid: str) -> list[Style]:
        """The style and the styles it is based on, nearest first."""
        out: list[Style] = []
        nxt: str | None = sid
        while nxt is not None and len(out) < MAX_STYLE_CHAIN:
            style = self.by_id.get(nxt)
            if style is None:
                break
            out.append(style)
            nxt = style.based_on
        return out

    def heading_level(self, sid: str) -> int | None:
        """The Markdown heading level a paragraph style stands for, if any.
        Built-in heading styles are recognised by name, which survives
        localisation of the id; anything else counts if it, or a style it is
        based on, carries an outline level, which is what Word itself uses to
        build a table of contents."""
        level = _heading_by_name(sid.lower())
        if level is not None:
            return min(level, 6)
        for style in self.chain(sid):
            level = _heading_by_name(style.name)
            if level is not None:
                return min(level, 6)
            if style.outline is not None and style.outline < 6:
                return style.outline + 1
        return None

    def numbering(self, sid: str) -> tuple[str, int] | None:
        return next((s.numbering for s in self.chain(sid) if s.numbering), None)

    def emphasis(self, sid: str) -> tuple[bool | None, bool | None]:
        chain = self.chain(sid)
        return (
            next((s.bold for s in chain if s.bold is not None), None),
            next((s.italic for s in chain if s.italic is not None), None),
        )


def num_pr(ppr: El) -> tuple[str, int] | None:
    """A list paragraph's numbering reference: `(numId, ilvl)`."""
    np = ppr.child("w:numPr")
    if np is None:
        return None
    num_id = np.child("w:numId")
    nid = num_id.attr("w:val") if num_id else None
    if nid is None:
        return None
    ilvl = np.child("w:ilvl")
    level = uint(ilvl.attr("w:val")) if ilvl else None
    return nid, level or 0


def toggle(rpr: El, name: str) -> bool | None:
    """A boolean run property such as `<w:b/>`, which is on when present unless
    its value says otherwise."""
    el = rpr.child(name)
    if el is None:
        return None
    return el.attr("w:val") not in ("0", "false", "off")


@dataclass(frozen=True)
class Level:
    ordered: bool = False
    start: int = 1


def read_levels(levels: Iterable[El]) -> dict[int, Level]:
    out: dict[int, Level] = {}
    for lvl in levels:
        i = uint(lvl.attr("w:ilvl"))
        if i is None:
            continue
        fmt_el, start_el = lvl.child("w:numFmt"), lvl.child("w:start")
        fmt = (fmt_el.attr("w:val") if fmt_el else None) or "bullet"
        start = uint(start_el.attr("w:val")) if start_el else None
        out[i] = Level(ordered=fmt not in ("bullet", "none"), start=1 if start is None else start)
    return out


@dataclass
class Numbering:
    """What the converter needs from `numbering.xml`: whether each list level is
    bulleted or numbered, and where its numbering starts."""

    # `numId` to its abstract definition and per-level overrides.
    nums: dict[str, tuple[str, dict[int, Level]]] = field(default_factory=lambda: {})
    abstracts: dict[str, dict[int, Level]] = field(default_factory=lambda: {})

    @classmethod
    def read(cls, root: El) -> Numbering:
        abstracts: dict[str, dict[int, Level]] = {}
        for a in root.children_named("w:abstractNum"):
            aid = a.attr("w:abstractNumId")
            if aid is not None:
                abstracts[aid] = read_levels(a.children_named("w:lvl"))
        nums: dict[str, tuple[str, dict[int, Level]]] = {}
        for n in root.children_named("w:num"):
            nid = n.attr("w:numId")
            abs_el = n.child("w:abstractNumId")
            abs_id = abs_el.attr("w:val") if abs_el else None
            if nid is None or abs_id is None:
                continue
            overrides: dict[int, Level] = {}
            for o in n.children_named("w:lvlOverride"):
                i = uint(o.attr("w:ilvl"))
                if i is None:
                    continue
                level = read_levels(o.children_named("w:lvl")).get(i)
                start_el = o.child("w:startOverride")
                start = uint(start_el.attr("w:val")) if start_el else None
                if level is not None:
                    overrides[i] = level
                elif start is not None:
                    base = abstracts.get(abs_id, {}).get(i, Level())
                    overrides[i] = Level(ordered=base.ordered, start=start)
            nums[nid] = (abs_id, overrides)
        return cls(nums, abstracts)

    def level(self, num: str, ilvl: int) -> Level:
        found = self.nums.get(num)
        if found is None:
            return Level()
        abs_id, overrides = found
        return overrides.get(ilvl) or self.abstracts.get(abs_id, {}).get(ilvl, Level())


@dataclass
class Field:
    """A complex field (`fldChar begin … separate … end`) that is open at the
    current position. Only its result is document text; the instruction is
    collected to recognise `HYPERLINK` fields, whose result becomes a link."""

    instruction: str = ""
    in_result: bool = False
    # Index in the current paragraph's spans where the result began.
    start: int = 0


@dataclass
class Inline:
    """Inline content gathered from one paragraph."""

    spans: list[Span] = field(default_factory=lambda: [])
    # Blocks found inside the paragraph's runs (text boxes), written after it.
    anchored: list[Block] = field(default_factory=lambda: [])
    # Set where a field result begins, so the next text starts a span of its
    # own and the result can later be turned into a link on its own.
    boundary: bool = False


@dataclass(frozen=True)
class Format:
    bold: bool = False
    italic: bool = False


class Reader:
    def __init__(self, styles: Styles, numbering: Numbering, rels: Rels) -> None:
        self.styles = styles
        self.numbering = numbering
        self.rels = rels
        # Running numbers for ordered lists, by `(numId, level)`, so a list that
        # continues after an interruption keeps counting as Word does.
        self.counters: dict[tuple[str, int], int] = {}
        self.fields: list[Field] = []
        self.note_refs: list[tuple[NoteKind, str]] = []
        self.note_labels: dict[tuple[NoteKind, str], str] = {}

    def blocks(self, parent: El, out: list[Block]) -> None:
        """Block-level content: the body, a table cell, a text box or a note."""
        for el in parent.elements():
            match el.name:
                case "w:p":
                    self.paragraph(el, out)
                case "w:tbl":
                    rows = self.table(el)
                    if rows:
                        out.append(Table(rows))
                # Deleted and moved-away content is not part of the document as
                # it now reads.
                case "w:del" | "w:moveFrom" | "w:sectPr":
                    pass
                # Content controls, custom XML, and tracked insertions all wrap
                # ordinary blocks; anything else holds none and costs nothing
                # to look through.
                case "w:sdtPr" | "w:sdtEndPr":
                    pass
                case _:
                    self.blocks(el, out)

    def paragraph(self, p: El, out: list[Block]) -> None:
        for f in self.fields:
            f.start = 0
        inline = Inline()
        self.inline(p, inline, Format(), None)

        ppr = p.child("w:pPr")
        style_el = ppr.child("w:pStyle") if ppr else None
        style = style_el.attr("w:val") if style_el else None
        outline_el = ppr.child("w:outlineLvl") if ppr else None
        outline = uint(outline_el.attr("w:val")) if outline_el else None
        direct_outline = outline + 1 if outline is not None and outline < 6 else None
        heading = self.styles.heading_level(style) if style is not None else None
        if heading is None:
            heading = direct_outline
        numbering = num_pr(ppr) if ppr else None
        if numbering is None and style is not None:
            numbering = self.styles.numbering(style)
        if numbering is not None and numbering[0] == "0":
            numbering = None

        if heading is not None:
            text = markdown.one_line(markdown.plain(inline.spans))
            if text:
                out.append(Heading(heading, text))
        else:
            text = markdown.paragraph_text(markdown.inline(inline.spans))
            if text:
                if numbering is not None:
                    num, ilvl = numbering
                    out.append(Item(ilvl, self.list_number(num, ilvl), text))
                else:
                    out.append(Paragraph(text))
        out.extend(inline.anchored)

    def list_number(self, num: str, ilvl: int) -> int | None:
        """The number an ordered list item shows, or `None` for a bullet."""
        # A shallower item restarts the levels below it, as in 1. a. b. 2. a.
        self.counters = {k: v for k, v in self.counters.items() if k[0] != num or k[1] <= ilvl}
        level = self.numbering.level(num, ilvl)
        if not level.ordered:
            return None
        key = (num, ilvl)
        self.counters[key] = self.counters.get(key, max(level.start - 1, 0)) + 1
        return self.counters[key]

    def inline(self, parent: El, out: Inline, fmt: Format, link: str | None) -> None:
        """Inline content of a paragraph or of anything wrapping runs inside it."""
        for el in parent.elements():
            match el.name:
                case "w:r":
                    self.run(el, out, fmt, link)
                case "w:hyperlink":
                    rel = self.rels.get(el.attr("r:id") or "")
                    url = rel.target if rel is not None and rel.external else None
                    self.inline(el, out, fmt, url or link)
                case "w:fldSimple":
                    instr = el.attr("w:instr")
                    url = hyperlink_target(instr) if instr is not None else None
                    self.inline(el, out, fmt, url or link)
                case "w:del" | "w:moveFrom" | "w:pPr" | "w:rPr" | "w:sdtPr" | "w:sdtEndPr":
                    pass
                # Office Math keeps its characters in `m:t`.
                case "m:t":
                    push(out, el.own_text(), fmt, link)
                case _:
                    self.inline(el, out, fmt, link)

    def run(self, r: El, out: Inline, inherited: Format, link: str | None) -> None:
        rpr = r.child("w:rPr")
        style_bold: bool | None = None
        style_italic: bool | None = None
        rstyle = rpr.child("w:rStyle") if rpr else None
        if rstyle is not None and (sid := rstyle.attr("w:val")) is not None:
            style_bold, style_italic = self.styles.emphasis(sid)

        def pick(name: str, from_style: bool | None, base: bool) -> bool:
            direct = toggle(rpr, name) if rpr else None
            if direct is not None:
                return direct
            return from_style if from_style is not None else base

        fmt = Format(
            bold=pick("w:b", style_bold, inherited.bold),
            italic=pick("w:i", style_italic, inherited.italic),
        )

        for el in r.elements():
            # Text between a field's `begin` and `separate` is its instruction,
            # not something the reader sees.
            hidden = bool(self.fields) and not self.fields[-1].in_result
            match el.name:
                case "w:t" if not hidden:
                    push(out, el.own_text(), fmt, link)
                case "w:tab" | "w:ptab" if not hidden:
                    push(out, "\t", fmt, link)
                case "w:br" | "w:cr" if not hidden:
                    push(out, LINE_BREAK, fmt, link)
                case "w:noBreakHyphen" if not hidden:
                    push(out, "-", fmt, link)
                case "w:sym" if not hidden:
                    # Symbol-font characters live in the private use area and
                    # mean nothing outside that font; anything else is a real
                    # character.
                    code = _hex(el.attr("w:char"))
                    if code is not None and not 0xE000 <= code <= 0xF8FF:
                        push(out, chr(code), fmt, link)
                case "w:fldChar":
                    self.field_char(el.attr("w:fldCharType"), out)
                case "w:instrText":
                    if self.fields and not self.fields[-1].in_result:
                        self.fields[-1].instruction += el.own_text()
                case "w:footnoteReference" | "w:endnoteReference":
                    kind = NoteKind.FOOT if el.name == "w:footnoteReference" else NoteKind.END
                    nid = el.attr("w:id")
                    if nid is not None:
                        label = self.note_label(kind, nid)
                        push(out, f"[^{label}]", Format(), None)
                case "w:rPr" | "w:delText" | "w:delInstrText":
                    pass
                case "w:t" | "w:tab" | "w:ptab" | "w:br" | "w:cr" | "w:noBreakHyphen" | "w:sym":
                    pass
                # Drawings, VML shapes and embedded objects: their only text is
                # in text boxes, which are whole blocks of their own.
                case _:
                    for box in el.find_all("w:txbxContent"):
                        self.blocks(box, out.anchored)

    def field_char(self, kind: str | None, out: Inline) -> None:
        match kind:
            case "begin":
                self.fields.append(Field(start=len(out.spans)))
            case "separate":
                if self.fields:
                    f = self.fields[-1]
                    f.in_result = True
                    f.start = len(out.spans)
                    out.boundary = True
            case "end":
                if self.fields:
                    f = self.fields.pop()
                    url = hyperlink_target(f.instruction)
                    if url is not None:
                        for span in out.spans[f.start :]:
                            if span.link is None:
                                span.link = url
            case _:
                pass

    def note_label(self, kind: NoteKind, nid: str) -> str:
        key = (kind, nid)
        label = self.note_labels.get(key)
        if label is None:
            label = str(len(self.note_labels) + 1)
            self.note_labels[key] = label
            self.note_refs.append(key)
        return label

    def notes(self, notes: dict[NoteKind, tuple[El, Rels]]) -> str:
        """The footnote and endnote definitions: those referenced from the text
        in the order they were referenced, then any that nothing references, so
        no note's text is lost either way."""
        order = list(self.note_refs)
        referenced = set(order)
        for kind in (NoteKind.FOOT, NoteKind.END):
            if kind not in notes:
                continue
            for note in notes[kind][0].elements():
                # Separator "notes" hold the line Word draws above the notes.
                note_type = note.attr("w:type")
                if note_type is not None and note_type != "normal":
                    continue
                nid = note.attr("w:id")
                if nid is not None and (kind, nid) not in referenced:
                    self.note_label(kind, nid)
                    order.append((kind, nid))

        out: list[str] = []
        for kind, nid in order:
            if kind not in notes:
                continue
            root, rels = notes[kind]
            note = next((n for n in root.elements() if n.attr("w:id") == nid), None)
            if note is None:
                continue
            reader = Reader(self.styles, self.numbering, rels)
            blocks: list[Block] = []
            reader.blocks(note, blocks)
            text = markdown.render(blocks).strip()
            if not text:
                continue
            label = self.note_labels[(kind, nid)]
            # Later lines of a note are indented so they stay in it.
            out.append(f"[^{label}]: " + text.replace("\n", "\n    "))
        return "\n\n".join(out)

    def table(self, tbl: El) -> list[list[str]]:
        """A table as rows of rendered cells. Horizontally merged cells are
        written as the merged cell followed by empty ones, and a vertically
        merged cell's continuation is empty, which keeps every row aligned with
        the grid."""
        rows: list[list[str]] = []
        for tr in descend(tbl, "w:tr", "w:tbl"):
            trpr = tr.child("w:trPr")

            def grid(name: str, trpr: El | None = trpr) -> int:
                g = trpr.child(name) if trpr else None
                return min((uint(g.attr("w:val")) if g else None) or 0, 64)

            row = [""] * grid("w:gridBefore")
            for tc in descend(tr, "w:tc", "w:tbl"):
                tcpr = tc.child("w:tcPr")
                span_el = tcpr.child("w:gridSpan") if tcpr else None
                span = uint(span_el.attr("w:val")) if span_el else None
                span = min(max(1 if span is None else span, 1), 64)
                merge = tcpr.child("w:hMerge") if tcpr else None
                continued = merge is not None and merge.attr("w:val") != "restart"
                row.append("" if continued else self.cell_text(tc))
                row.extend([""] * (span - 1))
            row.extend([""] * grid("w:gridAfter"))
            rows.append(row)
        return rows

    def cell_text(self, tc: El) -> str:
        blocks: list[Block] = []
        self.blocks(tc, blocks)
        lines: list[str] = []
        for block in blocks:
            match block:
                case Heading(_, text) | Paragraph(text):
                    lines.append(text)
                case Item(_, ordered, text):
                    lines.append(f"- {text}" if ordered is None else f"{ordered}. {text}")
                # A table inside a cell is flattened: one line per row, cells
                # separated by slashes.
                case Table(rows):
                    for r in rows:
                        cells = [c for c in r if c]
                        if cells:
                            lines.append(" / ".join(cells).replace("\\|", "|"))
        return markdown.cell(LINE_BREAK.join(lines))


def _hex(value: str | None) -> int | None:
    if not value:
        return None
    try:
        code = int(value, 16)
    except ValueError:
        return None
    return code if 0 <= code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else None


def descend(parent: El, want: str, stop: str) -> list[El]:
    """Elements named `want` under `parent`, looking through the wrappers that
    may sit between them (content controls, custom XML, tracked insertions) but
    not into a nested `stop` element such as an inner table."""
    out: list[El] = []
    for el in parent.elements():
        if el.name == want:
            out.append(el)
        elif el.name != stop and el.name not in ("w:del", "w:moveFrom"):
            out.extend(descend(el, want, stop))
    return out


def push(out: Inline, text: str, fmt: Format, link: str | None) -> None:
    if not text:
        return
    boundary, out.boundary = out.boundary, False
    if not boundary and out.spans:
        last = out.spans[-1]
        if last.bold == fmt.bold and last.italic == fmt.italic and last.link == link:
            last.text += text
            return
    out.spans.append(Span(text, fmt.bold, fmt.italic, link))


def hyperlink_target(instruction: str) -> str | None:
    """The URL of a `HYPERLINK "…"` field instruction. Links to a bookmark in the
    same document (`\\l`) have no URL worth writing and are left as text."""
    rest = instruction.strip()
    if not rest.startswith("HYPERLINK"):
        return None
    rest = rest.removeprefix("HYPERLINK")
    tokens: list[str] = []
    i = 0
    while i < len(rest):
        c = rest[i]
        if c.isspace():
            i += 1
        elif c == '"':
            end = rest.find('"', i + 1)
            end = len(rest) if end < 0 else end
            tokens.append(rest[i + 1 : end])
            i = end + 1
        else:
            j = i
            while j < len(rest) and not rest[j].isspace():
                j += 1
            tokens.append(rest[i:j])
            i = j
    it = iter(tokens)
    for t in it:
        if t.startswith("\\"):
            # Switches with an argument consume it; `\l` names a bookmark.
            if t in ("\\l", "\\m", "\\n", "\\o", "\\t"):
                next(it, None)
            continue
        if t:
            return t
    return None
