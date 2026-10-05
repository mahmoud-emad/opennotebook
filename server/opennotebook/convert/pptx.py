"""PowerPoint (`.pptx`) to Markdown, one section per slide.

Slides are taken in the order the presentation lists them, which is the order a
person sees, not the order of the part names: a deck whose slides were
reordered still has `slide1.xml` wherever it was first created.

Each slide becomes `## Slide N: <title>`, followed by the text of its shapes in
the order they sit in the slide's shape tree, its tables, the text of any
SmartArt diagram, and finally the speaker notes under a `Notes:` line. Text on
the slide layout and master is template text ("Click to add title") and is not
read.

The parts are read with the same element tree as Word rather than through
python-pptx: only the raw XML says which placeholder a shape is, whether a
paragraph has a bullet, and where a diagram keeps its text.
"""

from __future__ import annotations

from . import markdown
from .kinds import ConvertError, InputKind, Problem
from .markdown import LINE_BREAK, Block, Item, Paragraph, Span, Table
from .ooxml import Package, Rels
from .xmltree import El, uint

KIND = InputKind.POWERPOINT

# A shape that is not a placeholder, as `placeholder_type` reports it.
NOT_PLACEHOLDER = "-"


def convert(data: bytes) -> str:
    pkg = Package(data, KIND)
    main = pkg.main_part("ppt/presentation.xml")
    presentation = pkg.read_xml(main)
    if presentation is None:
        raise ConvertError(KIND, Problem.NOT_VALID)
    rels = pkg.rels(main)

    slide_parts: list[str] = []
    listed = presentation.child("p:sldIdLst")
    if listed is not None:
        for s in listed.children_named("p:sldId"):
            rel = rels.get(s.attr("r:id") or "")
            if rel is not None and not rel.external:
                slide_parts.append(rel.target)

    sections: list[str] = []
    for index, part in enumerate(slide_parts, start=1):
        # A slide listed but missing from the package is skipped rather than
        # failing the deck: the rest of the text is still there to read.
        slide = pkg.read_xml(part)
        if slide is None:
            continue
        reader = SlideReader(pkg.rels(part))
        tree = slide.path("p:cSld", "p:spTree")
        if tree is not None:
            reader.shapes(tree)
        for data_part in reader.diagrams:
            data_xml = pkg.read_xml(data_part)
            if data_xml is not None:
                reader.blocks.extend(diagram_text(data_xml))

        section = f"## Slide {index}: {reader.title}" if reader.title else f"## Slide {index}"
        body = markdown.render(reader.blocks)
        if body:
            section += "\n\n" + body
        speaker = notes(pkg, reader.rels)
        if speaker is not None:
            section += "\n\nNotes:\n\n" + speaker
        sections.append(section)
    return "\n\n".join(sections)


def notes(pkg: Package, slide_rels: Rels) -> str | None:
    """The speaker notes of a slide, from the body placeholder of its notes
    page. The notes page also carries a slide image and a slide number, which
    are not notes."""
    rel = next((r for r in slide_rels.values() if r.is_("notesSlide") and not r.external), None)
    if rel is None:
        return None
    page = pkg.read_xml(rel.target)
    if page is None:
        return None
    rels = pkg.rels(rel.target)
    blocks: list[Block] = []
    for sp in page.find_all("p:sp"):
        if placeholder_type(sp) != "body":
            continue
        body = sp.child("p:txBody")
        if body is not None:
            text_body(body, rels, False, blocks)
    text = markdown.render(blocks)
    return text if text.strip() else None


class SlideReader:
    def __init__(self, rels: Rels) -> None:
        self.rels = rels
        self.title: str | None = None
        self.blocks: list[Block] = []
        # SmartArt data parts, read once the shape walk is done.
        self.diagrams: list[str] = []

    def shapes(self, tree: El) -> None:
        for el in tree.elements():
            match el.name:
                case "p:sp":
                    self.shape(el)
                case "p:grpSp":
                    self.shapes(el)
                case "p:graphicFrame":
                    tbl = el.find("a:tbl")
                    if tbl is not None:
                        rows = table(tbl, self.rels)
                        if rows:
                            self.blocks.append(Table(rows))
                        continue
                    ids = el.find("dgm:relIds")
                    rel = self.rels.get((ids.attr("r:dm") if ids else None) or "")
                    if rel is not None and not rel.external:
                        self.diagrams.append(rel.target)
                case _:
                    pass

    def shape(self, sp: El) -> None:
        body = sp.child("p:txBody")
        if body is None:
            return
        placeholder = placeholder_type(sp)
        # Date and slide number are filled in by PowerPoint, not written by the
        # author.
        if placeholder in ("dt", "sldNum"):
            return
        if placeholder in ("title", "ctrTitle") and self.title is None:
            lines = (
                markdown.one_line(markdown.plain(paragraph_spans(p, self.rels)))
                for p in body.children_named("a:p")
            )
            self.title = " ".join(t for t in lines if t)
            return
        # Body and content placeholders take their bullets from the slide
        # master, so their paragraphs are list items unless they say otherwise.
        # A free text box has no bullets by default.
        bulleted = placeholder in (None, "body", "obj")
        text_body(body, self.rels, bulleted, self.blocks)


def placeholder_type(sp: El) -> str | None:
    """The placeholder type of a shape: `NOT_PLACEHOLDER` for a shape that is
    not a placeholder, the type for one that is, and `None` for a placeholder
    that has no type (a content placeholder, which defaults to body)."""
    ph = sp.path("p:nvSpPr", "p:nvPr", "p:ph")
    if ph is None:
        return NOT_PLACEHOLDER
    return ph.attr("type")


def text_body(body: El, rels: Rels, bulleted: bool, out: list[Block]) -> None:
    """Paragraphs of a text body as blocks: list items where the paragraph has a
    bullet (explicitly, or by default in a body placeholder), plain paragraphs
    otherwise."""
    # Running numbers for auto-numbered paragraphs, by level.
    counters: dict[int, int] = {}
    for p in body.children_named("a:p"):
        ppr = p.child("a:pPr")
        level = min(uint(ppr.attr("lvl") if ppr else None) or 0, 8)
        text = markdown.paragraph_text(markdown.inline(paragraph_spans(p, rels)))
        if not text:
            continue
        auto = ppr.child("a:buAutoNum") if ppr else None
        if ppr is not None and ppr.child("a:buNone") is not None:
            has_bullet = False
        elif ppr is not None and (ppr.child("a:buChar") or ppr.child("a:buBlip")) is not None:
            has_bullet = True
        else:
            has_bullet = auto is not None or bulleted
        counters = {k: v for k, v in counters.items() if k <= level}
        if not has_bullet:
            counters.pop(level, None)
            out.append(Paragraph(text))
            continue
        ordered: int | None = None
        if auto is not None:
            start = uint(auto.attr("startAt"))
            counters[level] = counters.get(level, max((1 if start is None else start) - 1, 0)) + 1
            ordered = counters[level]
        else:
            counters.pop(level, None)
        out.append(Item(level, ordered, text))


def paragraph_spans(p: El, rels: Rels) -> list[Span]:
    spans: list[Span] = []
    for el in p.elements():
        if el.name in ("a:r", "a:fld"):
            t = el.child("a:t")
            text = t.own_text() if t else ""
            rpr = el.child("a:rPr")
        elif el.name == "a:br":
            text, rpr = LINE_BREAK, None
        else:
            continue
        if not text:
            continue

        def flag(name: str, rpr: El | None = rpr) -> bool:
            return rpr is not None and rpr.attr(name) in ("1", "true")

        click = rpr.child("a:hlinkClick") if rpr else None
        rel = rels.get((click.attr("r:id") if click else None) or "")
        link = rel.target if rel is not None and rel.external else None
        spans.append(Span(text, flag("b"), flag("i"), link))
    return spans


def table(tbl: El, rels: Rels) -> list[list[str]]:
    """A slide table. PowerPoint writes every grid cell, marking the ones
    covered by a merge with `hMerge` or `vMerge`, so covered cells are simply
    empty and the rows stay aligned."""
    rows: list[list[str]] = []
    for tr in tbl.children_named("a:tr"):
        row: list[str] = []
        for tc in tr.children_named("a:tc"):
            if any(tc.attr(a) in ("1", "true") for a in ("hMerge", "vMerge")):
                row.append("")
                continue
            body = tc.child("a:txBody")
            lines = (
                [markdown.inline(paragraph_spans(p, rels)) for p in body.children_named("a:p")]
                if body
                else []
            )
            row.append(markdown.cell(LINE_BREAK.join(lines)))
        rows.append(row)
    return rows


def diagram_text(data: El) -> list[Block]:
    """The text of a SmartArt diagram, one item per node. The data part holds
    the nodes the author typed; the drawing part next to it repeats the same
    text laid out, so only the data part is read."""
    texts = (
        markdown.paragraph_text(markdown.plain(paragraph_spans(p, {})))
        for p in data.find_all("a:p")
    )
    return [Item(0, None, t) for t in texts if t]
