"""What is wrong with a compiled scene, said precisely enough to fix: a
label a line crosses, two labels on each other, two elements in the same
cells, something off the board, a board left mostly empty, or a part the
compiler had to leave out (docs/video-overview-spec.md, section 4.7).

The compiler placed every point, so this is exact geometry, not a guess
from a picture: a label is crossed when a point of another element's line
falls inside the label's box.
"""

from opennotebook.build.whiteboard.compile import SAFE, Drawing, H, Piece, W
from opennotebook.build.whiteboard.geometry import length_of

# A label's box is trimmed by this much before testing: a stroke that only
# grazes a letter's edge does not hide it.
GRAZE = 4.0
# Below this share of the board's area the drawing reads as a few small
# things in a large empty space. The proof of concept's good scenes filled
# 0.73 to 0.88 of it, its weak ones 0.47 to 0.60, measured by bounding box.
MIN_FILL = 0.4
EDGE = 8.0
# Shorter than this an arrow is a mark, not a join.
MIN_ARROW = 60.0


def _inside(pt: tuple[float, float], b: tuple[float, float, float, float]) -> bool:
    return b[0] < pt[0] < b[2] and b[1] < pt[1] < b[3]


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def problems(d: Drawing) -> list[str]:
    # A thing drawn as a labelled box because no icon shows it is a fair
    # drawing; only what had to be left out is a fault.
    found: list[str] = [n for n in d.notes if "left out" in n]
    texts = [p for p in d.pieces if p.kind == "text" and p.text is not None]
    lines = [p for p in d.pieces if p.kind == "line"]
    for t in texts:
        tb = (t.box[0] + GRAZE, t.box[1] + GRAZE, t.box[2] - GRAZE, t.box[3] - GRAZE)
        crossing = {ln.element for ln in lines if ln.element != t.element
                    and any(_inside(pt, tb) for pt in ln.points)}  # fmt: skip
        for other in sorted(crossing):
            found.append(f"the label {_said(t)} of {t.element} is crossed by {other}")
    for i, a in enumerate(texts):
        for b in texts[i + 1 :]:
            if a.element != b.element and _overlap(a.box, b.box):
                found.append(f"the labels {_said(a)} and {_said(b)} overlap")
    ids = sorted(d.boxes)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if _overlap(d.boxes[a], d.boxes[b]):
                found.append(f"{a} and {b} are in the same cells")
    for p in d.pieces:
        x0, y0, x1, y1 = _extent(p)
        if x0 < EDGE or y0 < EDGE or x1 > W - EDGE or y1 > H - EDGE:
            found.append(f"{p.element} runs off the board")
            break
    for el, n in _shafts(d).items():
        if n < MIN_ARROW:
            found.append(
                f"the arrow {el} is only {n:.0f} px long, too short to see: leave a free cell "
                "between the elements it joins"
            )
    drawn = [_extent(p) for p in d.pieces if p.element != "title"]
    if drawn:
        x0, y0 = min(b[0] for b in drawn), min(b[1] for b in drawn)
        x1, y1 = max(b[2] for b in drawn), max(b[3] for b in drawn)
        share = (x1 - x0) * (y1 - y0) / ((SAFE[2] - SAFE[0]) * (SAFE[3] - SAFE[1]))
        if share < MIN_FILL:
            found.append(f"the drawing fills only {share:.0%} of the board; use more of the grid")
    else:
        found.append("the scene draws nothing")
    return found


def _shafts(d: Drawing) -> dict[str, float]:
    """Each arrow's or line's length: its first stroke, the shaft."""
    out: dict[str, float] = {}
    for p in d.pieces:
        if p.kind == "line" and p.element in d.connectors and p.element not in out:
            out[p.element] = length_of(p.points)
    return out


def _extent(p: Piece) -> tuple[float, float, float, float]:
    if p.kind == "line" and p.points:
        xs, ys = [x for x, _ in p.points], [y for _, y in p.points]
        return (min(xs), min(ys), max(xs), max(ys))
    return p.box


def _said(t: Piece) -> str:
    return repr(t.said)
