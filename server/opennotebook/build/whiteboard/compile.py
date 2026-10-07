"""A scene as the pen draws it: every stroke and label placed on the board
and timed to the word that names it (docs/video-overview-spec.md, sections
4.4 to 4.6).

The scene JSON names grid cells; this places everything in pixels. An
element owns the box of its cells; its shape and label stay inside it, so
two elements in different cells cannot cross. Arrows join box edges. Labels
are shrunk to fit their box and wrapped onto two lines before they would
spill.

Timing follows the narration: an element starts as its word is said (a
beat of 120 ms early, as a hand starts as the word begins), draws at a
speed set by its length, and its label follows. Two elements on the same
word are drawn one after the other. When the drawing would not finish
before the scene ends, every piece is drawn proportionally faster.
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import math
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import skia

from opennotebook.build.whiteboard import geometry as g
from opennotebook.build.whiteboard import icons
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.scene import COLS, ROWS, Beat, Element, Scene, cell

W, H = 1920, 1080
# The board: a title band at the top, then the 6×6 grid.
SAFE = (110.0, 175.0, 1810.0, 940.0)
TITLE_Y = 118.0
PAD = 16.0

# The inks, the highlighter, the pen's width and the hand's sizes are the
# theme's (`theme.py`); what follows is how the board is laid out and timed.

# Drawing speed, px of line per ms, and the bounds of one element's time.
LINE_SPEED = 1.6
TEXT_SPEED = 0.9
SHAPE_MS = (300, 1400)
LABEL_MS = (250, 900)
LEAD_MS = 120
# The board is still for this long before a scene's end: the drawing is
# done before the next one starts.
SETTLE_MS = 500

MIN_LABEL = 30

type Box = tuple[float, float, float, float]


@dataclass
class Piece:
    """One thing the pen does: a line drawn along its points, a label
    written left to right, a highlight washed in behind, or a shape filled
    in (`shape`, a closed path) where a theme fills its boxes."""

    element: str
    kind: Literal["line", "text", "wash", "fill", "card"]
    color: tuple[int, int, int]
    start_ms: float = 0.0
    end_ms: float = 0.0
    points: g.Poly = field(default_factory=list[g.Point])
    text: g.Text | None = None
    # Where a label's baseline starts; a wash's box.
    at: g.Point = (0.0, 0.0)
    box: Box = (0.0, 0.0, 0.0, 0.0)
    width: float = field(default_factory=lambda: th.current().stroke)
    # A label's words, for what the lint says about it.
    said: str = ""
    shape: skia.Path | None = None


@dataclass
class Drawing:
    pieces: list[Piece]
    # Each element's box, for the lint.
    boxes: dict[str, Box]
    # What the compiler had to change or leave out, said plainly.
    notes: list[str]
    # The ids of arrows and lines.
    connectors: set[str] = field(default_factory=set[str])


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def box_of(e: Element) -> Box:
    assert e.at is not None
    col, row = cell(e.at)
    cols, rows = e.span
    cols, rows = min(cols, len(COLS) - col), min(rows, ROWS - row)
    x0, y0, x1, y1 = SAFE
    cw, ch = (x1 - x0) / len(COLS), (y1 - y0) / ROWS
    return (x0 + col * cw, y0 + row * ch, x0 + (col + cols) * cw, y0 + (row + rows) * ch)


def _inset(b: Box, d: float) -> Box:
    return (b[0] + d, b[1] + d, b[2] - d, b[3] - d)


def fit_text(s: str, max_w: float, size: float) -> list[g.Text]:
    """`s` at the largest size up to `size` that fits `max_w`, on one line,
    or on two when one would be smaller than `MIN_LABEL`."""
    while size >= MIN_LABEL:
        t = g.text(s, size)
        if t.width <= max_w:
            return [t]
        size *= 0.92
    words = s.split()
    if len(words) > 1:
        half = max(range(1, len(words)), key=lambda i: -abs(len(" ".join(words[:i])) - len(s) / 2))
        a, b = " ".join(words[:half]), " ".join(words[half:])
        size = MIN_LABEL * 1.4
        while size > MIN_LABEL * 0.8:
            ta, tb = g.text(a, size), g.text(b, size)
            if max(ta.width, tb.width) <= max_w:
                return [ta, tb]
            size *= 0.92
        return [g.text(a, size), g.text(b, size)]
    return [g.text(s, MIN_LABEL * 0.8)]


def _label_pieces(eid: str, s: str, box: Box, color: tuple[int, int, int], size: float,
                  center_y: float | None = None) -> list[Piece]:  # fmt: skip
    """A label centred in `box` (or on `center_y`), one piece per line."""
    if not s:
        return []
    x0, y0, x1, y1 = box
    lines = fit_text(s, x1 - x0, size)
    lh = max(t.ascent + t.descent for t in lines) * 1.05
    cy = center_y if center_y is not None else (y0 + y1) / 2
    top = cy - lh * len(lines) / 2
    out: list[Piece] = []
    for i, t in enumerate(lines):
        base = top + lh * i + t.ascent
        x = (x0 + x1 - t.width) / 2
        out.append(
            Piece(
                eid,
                "text",
                color,
                text=t,
                at=(x, base),
                said=s,
                box=(x, base - t.ascent, x + t.width, base + t.descent),
            )
        )
    return out


def _edge(b: Box, toward: g.Point) -> g.Point:
    """Where the line from a box's centre toward a point leaves the box,
    a little outside it."""
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return (cx, cy)
    hw, hh = (b[2] - b[0]) / 2 + 14, (b[3] - b[1]) / 2 + 14
    t = min(hw / abs(dx) if dx else math.inf, hh / abs(dy) if dy else math.inf)
    return (cx + dx * t, cy + dy * t)


def shape(e: Element, box: Box, notes: list[str]) -> tuple[list[Piece], Box | None]:
    """An element's own strokes and labels, untimed, and the box its label
    sits in when it has one."""
    look = th.current()
    color = look.ink[e.tone]
    size = look.label_size
    seed = _seed(e.id, e.kind, e.at, e.label)
    inner = _inset(box, PAD)
    x0, y0, x1, y1 = inner
    w, h = x1 - x0, y1 - y0
    eid = e.id

    def lines(polys: list[g.Poly]) -> list[Piece]:
        return [Piece(eid, "line", color, points=p) for p in polys if len(p) > 1]

    def outlined(polys: list[g.Poly]) -> list[Piece]:
        """A box's or circle's outline, and the theme's fill behind it."""
        out: list[Piece] = []
        if look.fill != "none" and polys and len(polys[0]) > 2:
            fill = look.fills.get(e.tone, color)
            out.append(Piece(eid, "fill", fill, box=inner, shape=_closed(polys[0])))
        if look.outline or not out:
            out += lines(polys)
        return out

    if e.kind == "box":
        return outlined(g.rough_rect(x0, y0, w, h, seed)) + _label_pieces(
            eid, e.label, _inset(inner, 14), color, size
        ), inner
    if e.kind == "circle":
        out = outlined(g.rough_ellipse((x0 + x1) / 2, (y0 + y1) / 2, w / 2, h / 2, seed))
        return out + _label_pieces(eid, e.label, _inset(inner, w * 0.15), color, size), inner
    if e.kind == "label":
        return _label_pieces(eid, e.label or e.text, inner, color, size * 1.2), None
    if e.kind == "number":
        label_h = 56.0 if e.label else 0.0
        fig = _label_pieces(eid, e.text or e.label, (x0, y0, x1, y1 - label_h), color, 120)
        cap = _label_pieces(eid, e.label, (x0, y1 - label_h, x1, y1), look.ink["ink"], 40)
        return fig + cap if e.text else fig, None
    if e.kind == "sketch":
        if not e.d:
            notes.append(f"sketch {eid or '?'} has no path; left out")
            return [], None
        try:
            raw = g.svg_path([e.d])
        except Exception:
            notes.append(f"sketch {eid or '?'} has a path that does not parse; left out")
            return [], None
        label_h = 60.0 if e.label else 0.0
        b = raw.getBounds()
        s = min(w / max(b.width(), 1), (h - label_h) / max(b.height(), 1))
        dx = x0 + (w - b.width() * s) / 2 - b.left() * s
        dy = y0 + (h - label_h - b.height() * s) / 2 - b.top() * s
        polys = [
            g.wobble(p, seed + i) for i, p in enumerate(g.polylines(g.transform(raw, s, dx, dy)))
        ]
        return lines(polys) + _label_pieces(
            eid, e.label, (x0, y1 - label_h, x1, y1), color, 44
        ), None
    # An icon, or what stands in for one the library does not have.
    name = e.icon or ""
    if not icons.exists(name):
        found = icons.search(f"{name} {e.label}", 1)
        if not found:
            notes.append(f"icon {name!r} is not in the library and nothing like it is; drew a box")
            return shape(e.model_copy(update={"kind": "box"}), box, notes)
        notes.append(f"icon {name!r} is not in the library; drew {found[0]!r}")
        name = found[0]
    label_h = 62.0 if e.label else 0.0
    side = min(w, h - label_h) * 0.86
    s = side / icons.SIZE
    dx = (x0 + x1) / 2 - side / 2
    dy = y0 + (h - label_h - side) / 2
    polys = [
        g.wobble(p, seed + i, 0.9)
        for i, p in enumerate(g.polylines(g.transform(icons.path(name), s, dx, dy)))
    ]
    disc: list[Piece] = []
    if look.fill == "solid":
        # Cut paper: the icon is drawn on a disc of paper of its own.
        cx, cy, r = dx + side / 2, dy + side / 2, side * 0.62
        ring = skia.Path()
        ring.addCircle(cx, cy, r)
        paper_disc = look.fills.get("ink", (0xFF, 0xFF, 0xFF))
        disc = [Piece(eid, "fill", paper_disc, box=(cx - r, cy - r, cx + r, cy + r), shape=ring)]
    label = _label_pieces(eid, e.label, (x0, y1 - label_h, x1, y1), color, 44)
    return disc + lines(polys) + label, None


def _closed(poly: g.Poly) -> skia.Path:
    path = skia.Path()
    path.moveTo(*poly[0])
    for pt in poly[1:]:
        path.lineTo(*pt)
    path.close()
    return path


# The shortest an arrow is drawn. Two joined things in neighbouring cells
# leave almost no room between them, and models put them there whatever they
# are told (2 to 25 px arrows in a real run, repaired by neither Sonnet nor
# Opus). Such an arrow is drawn this long about its midpoint, reaching a
# little into both ends, rather than the scene being refused.
MIN_JOIN = 70.0


def _at_least(p: g.Point, q: g.Point, length: float) -> tuple[g.Point, g.Point]:
    d = math.dist(p, q)
    if d >= length:
        return p, q
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    if d < 1e-6:
        # Ends on one point: no direction to keep, so left to right.
        ux, uy = 1.0, 0.0
    else:
        ux, uy = (q[0] - p[0]) / d, (q[1] - p[1]) / d
    h = length / 2
    return (mx - ux * h, my - uy * h), (mx + ux * h, my + uy * h)


def connector(e: Element, boxes: dict[str, Box], notes: list[str]) -> list[Piece]:
    a, b = boxes.get(e.source or ""), boxes.get(e.target or "")
    if a is None or b is None:
        notes.append(
            f"{e.kind} {e.id or '?'} joins {e.source!r} to {e.target!r}, which "
            "are not both in the scene; left out"
        )
        return []
    if e.source == e.target:
        notes.append(f"{e.kind} {e.id} joins {e.source!r} to itself; left out")
        return []
    ca, cb = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    p, q = _edge(a, cb), _edge(b, ca)
    p, q = _at_least(p, q, MIN_JOIN)
    polys = g.arrow(p, q, _seed(e.id, e.source, e.target), head=e.kind == "arrow")
    color = th.current().ink[e.tone]
    out = [Piece(e.id, "line", color, points=pl) for pl in polys]
    if e.label:
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        n = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
        # Opposite the side the curve bends to, so the line does not run
        # through its own label.
        ox, oy = (q[1] - p[1]) / n * 30, -(q[0] - p[0]) / n * 30
        out += _label_pieces(
            e.id, e.label, (mx + ox - 160, my + oy - 32, mx + ox + 160, my + oy + 32), color, 44
        )
    return out


def _clamp(v: float, lo_hi: tuple[int, int]) -> float:
    return min(max(v, lo_hi[0]), lo_hi[1])


def _duration(p: Piece) -> float:
    if p.kind == "line":
        return g.length_of(p.points) / LINE_SPEED
    if p.kind == "text" and p.text is not None:
        return p.text.width / TEXT_SPEED
    return 300.0


def compile_scene(
    sc: Scene,
    when: Callable[[Beat], float],
    start_ms: float,
    end_ms: float,
    theme: th.Theme | str | None = None,
) -> Drawing:
    """The scene placed and timed, in `theme` (or the one in use). `when`
    turns a beat into the ms its word is said; the scene is on screen from
    `start_ms` to `end_ms`."""
    if theme is not None:
        with th.using(theme):
            return compile_scene(sc, when, start_ms, end_ms)
    look = th.current()
    notes: list[str] = []
    elements = list(sc.elements)
    for i, e in enumerate(elements):
        if not e.id:
            elements[i] = e.model_copy(update={"id": f"_e{i}"})
    boxes: dict[str, Box] = {}
    content: dict[str, Box] = {}
    label_boxes: dict[str, Box] = {}
    groups: list[tuple[float, int, list[Piece]]] = []
    placed = [e for e in elements if e.kind not in ("arrow", "line")]
    for i, e in enumerate(placed):
        if e.at is None:
            notes.append(f"{e.kind} {e.id} has no cell; left out")
            continue
        box = box_of(e)
        boxes[e.id] = box
        pieces, lb = shape(e, box, notes)
        if pieces:
            # What an arrow joins: the drawing, which sits well inside its
            # cells, not the cells, which may touch their neighbour's.
            content[e.id] = _union([extent(p) for p in pieces])
        if lb is not None:
            label_boxes[e.id] = lb
        groups.append((when(e.beat), i, pieces))
    for i, e in enumerate(elements):
        if e.kind in ("arrow", "line"):
            groups.append((when(e.beat), len(placed) + i, connector(e, content, notes)))
    for h in sc.highlight:
        b = boxes.get(h.target)
        if b is None:
            notes.append(f"highlight of {h.target!r}, which is not in the scene; left out")
            continue
        wash = Piece(h.target, "wash", look.highlight, box=_inset(b, 6))
        groups.append((when(h.beat), 10_000, [wash]))
    title: list[Piece] = []
    if sc.title:
        t = fit_text(sc.title, SAFE[2] - SAFE[0], look.title_size)[0]
        title = [
            Piece(
                "title",
                "text",
                look.ink["ink"],
                text=t,
                at=(SAFE[0], TITLE_Y),
                said=sc.title,
                box=(SAFE[0], TITLE_Y - t.ascent, SAFE[0] + t.width, TITLE_Y + t.descent),
            )
        ]
    groups.sort(key=lambda x: (x[0], x[1]))
    timed = _schedule(title, groups, start_ms, end_ms)
    for p in timed:
        if p.text is not None and p.text.missing:
            notes.append(
                f"the label {p.said!r} has letters the board cannot write yet "
                f"({''.join(p.text.missing)}); drawn as ?"
            )
    joins = {e.id for e in elements if e.kind in ("arrow", "line")}
    return Drawing(timed, boxes, notes, joins)


# ── an illustrated scene: its labels on cards over its picture ───────────────

# The band the cards sit in, where the picture was asked to stay calm.
CARD_BAND = (760.0, 1010.0)
CARD_PAD = (20.0, 12.0)
CARD_GAP = 14.0
CARD_SIZES = (40.0, 34.0, 28.0)


def _card_texts(sc: Scene) -> list[tuple[Element, str]]:
    """What an illustrated scene writes: each element's words, and each arrow
    as the relation it asserts, in the order they are said."""
    names = {e.id: (e.label or e.text or "") for e in sc.elements if e.id}
    out: list[tuple[Element, str]] = []
    for e in sc.elements:
        if e.kind in ("arrow", "line"):
            a, b = names.get(e.source or "", ""), names.get(e.target or "", "")
            if a and b:
                verb = f" {e.label} " if e.label else (" → " if e.kind == "arrow" else " — ")
                out.append((e, f"{a}{verb}{b}"))
        elif e.kind == "number" and e.text:
            out.append((e, f"{e.text} {e.label}".strip()))
        elif e.label or e.text:
            out.append((e, e.label or e.text))
    return out


def compile_illustrated(
    sc: Scene, when: Callable[[Beat], float], start_ms: float, end_ms: float
) -> Drawing:
    """An illustrated scene placed and timed: the picture is the board; the
    scene's own words are cards in the calm band at its foot, each laid down
    on the word that names it, and its title on a card at the top."""
    look = th.current()
    texts = _card_texts(sc)
    rows: list[list[tuple[Element, str, g.Text]]] = []
    size = CARD_SIZES[-1]
    for size in CARD_SIZES:
        rows = [[]]
        width = 0.0
        for e, s in texts:
            t = fit_text(s, SAFE[2] - SAFE[0] - 2 * CARD_PAD[0], size)[0]
            w = t.width + 2 * CARD_PAD[0]
            if rows[-1] and width + CARD_GAP + w > SAFE[2] - SAFE[0]:
                rows.append([])
                width = 0.0
            rows[-1].append((e, s, t))
            width += (CARD_GAP if width else 0) + w
        line_h = size * 1.15 + 2 * CARD_PAD[1]
        if len(rows) * (line_h + CARD_GAP) <= CARD_BAND[1] - CARD_BAND[0]:
            break
    line_h = size * 1.15 + 2 * CARD_PAD[1]
    top = CARD_BAND[1] - len(rows) * (line_h + CARD_GAP)
    groups: list[tuple[float, int, list[Piece]]] = []
    k = 0
    for r, row in enumerate(rows):
        widths = [t.width + 2 * CARD_PAD[0] for _, _, t in row]
        x = (SAFE[0] + SAFE[2]) / 2 - (sum(widths) + CARD_GAP * (len(row) - 1)) / 2
        y = top + r * (line_h + CARD_GAP)
        for (e, said, t), w in zip(row, widths, strict=True):
            box = (x, y, x + w, y + line_h)
            base = y + CARD_PAD[1] + (size * 1.15 - (t.ascent + t.descent)) / 2 + t.ascent
            card = Piece(e.id or f"_c{k}", "card", look.paper, box=box)
            word = Piece(
                e.id or f"_c{k}", "text", look.ink[e.tone], text=t, at=(x + CARD_PAD[0], base),
                said=said,
                box=(x + CARD_PAD[0], base - t.ascent, x + CARD_PAD[0] + t.width, base + t.descent),
            )  # fmt: skip
            groups.append((when(e.beat), k, [card, word]))
            x += w + CARD_GAP
            k += 1
    title: list[Piece] = []
    if sc.title:
        t = fit_text(sc.title, SAFE[2] - SAFE[0] - 2 * CARD_PAD[0], look.title_size * 0.85)[0]
        x0, y0 = SAFE[0] - CARD_PAD[0], 64.0
        h = t.ascent + t.descent + 2 * CARD_PAD[1]
        x1 = x0 + t.width + 2 * CARD_PAD[0]
        title = [
            Piece("title", "card", look.paper, box=(x0, y0, x1, y0 + h)),
            Piece(
                "title", "text", look.ink["ink"], text=t, at=(SAFE[0], y0 + CARD_PAD[1] + t.ascent),
                said=sc.title, box=(SAFE[0], y0, SAFE[0] + t.width, y0 + h),
            ),
        ]  # fmt: skip
    groups.sort(key=lambda x: (x[0], x[1]))
    return Drawing(_schedule(title, groups, start_ms, end_ms), {}, [], set())


def _schedule(
    title: list[Piece],
    groups: list[tuple[float, int, list[Piece]]],
    start_ms: float,
    end_ms: float,
) -> list[Piece]:
    """Give every piece its time, one at a time, done `SETTLE_MS` before the
    scene ends.

    A piece starts on its word, or as soon as the one before it is done;
    but never later than leaves time for every piece after it, so a scene
    whose last words come at its very end draws them a little early rather
    than late. Only when the drawing is longer than the whole scene is the
    pen faster, by just enough."""
    limit = max(end_ms - SETTLE_MS, start_ms + 200)
    order: list[tuple[float | None, Piece, float]] = []
    for p in title:
        order.append((None, p, _clamp(_duration(p), LABEL_MS)))
    for at, _, pieces in groups:
        shape_ms = sum(_duration(p) for p in pieces if p.kind == "line")
        share = _clamp(shape_ms, SHAPE_MS) / shape_ms if shape_ms else 1.0
        for k, p in enumerate(pieces):
            if p.kind == "line":
                d = _duration(p) * share
            elif p.kind == "text":
                d = _clamp(_duration(p), LABEL_MS)
            else:
                d = _duration(p)
            # Only a group's first piece waits for its word; the rest follow it.
            order.append((max(at - LEAD_MS, start_ms) if k == 0 else None, p, d))
    first = start_ms + 100
    need = sum(d for _, _, d in order)
    speed = max(need / max(limit - first, 1.0), 1.0)
    # The latest each piece may start: what is left after it must still fit.
    after = 0.0
    latest: list[float] = []
    for _, _, d in reversed(order):
        after += d / speed
        latest.append(limit - after)
    latest.reverse()
    cursor = first
    out: list[Piece] = []
    for (word, p, d), last in zip(order, latest, strict=True):
        begin = cursor if word is None else max(cursor, word)
        begin = max(min(begin, last), cursor)
        p.start_ms, p.end_ms = begin, begin + d / speed
        cursor = p.end_ms
        out.append(p)
    return out


def extent(p: Piece) -> Box:
    if p.kind == "line" and p.points:
        xs, ys = [x for x, _ in p.points], [y for _, y in p.points]
        return (min(xs), min(ys), max(xs), max(ys))
    return p.box


def _union(bs: list[Box]) -> Box:
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )


def bounds_of(p: Piece) -> skia.Rect:
    if p.kind == "line" and p.points:
        xs, ys = [x for x, _ in p.points], [y for _, y in p.points]
        return skia.Rect.MakeLTRB(min(xs), min(ys), max(xs), max(ys))
    return skia.Rect.MakeLTRB(*p.box)
