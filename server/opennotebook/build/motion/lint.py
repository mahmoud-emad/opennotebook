"""What is wrong with a motion page before it is recorded, and what is
wrong with where it put things after (`docs/motion-style.md`).

Two kinds of check, both deterministic and free:

`page_problems` runs on the model's answer: its declared ids are unique,
its arrows join ids that exist, every declared thing writes something, and
every word it declares it will write was said — the whiteboard's grounding
(`ground.py`) over the manifest instead of over a scene's elements.

`layout_problems` runs in the recorded page, over the boxes of everything
the page marked with `data-mn` at the end of its scene: two labels on top
of each other, a label off the frame, nothing marked at all. The browser is
the layout measurer here where the whiteboard's compiler is for a board:
the page placed its own pixels, so the lint asks the page where they ended
up rather than guessing from a picture.
"""

from opennotebook.build.encode import HEIGHT, WIDTH
from opennotebook.build.motion import ground
from opennotebook.build.motion.scene import Node, Page

# The kinds a page must mark with data-mn: everything a viewer reads or
# points at. Panels (background layers) and marks need no box.
BOXED = frozenset({"text", "card", "figure", "bar", "icon", "line", "arrow"})

# A label's box shrunk by this much before testing: a thing that only
# grazes its neighbour's edge does not hide it.
GRAZE = 6.0
# Nearer than this to the frame's edge, a label runs off screen.
EDGE = 4.0
# Below this share of the frame a scene with this many things reads as a
# few small marks in a large empty space. Only a scene declaring at least
# this many things is held to it: two kinetic labels are a legitimate
# scene, four scattered specks are not.
MIN_FILL = 0.15
FILL_AT = 3


def page_problems(page: Page, said: ground.Said | None = None) -> list[str]:
    """What is wrong with a written page before it is run. `said` is None
    to skip the grounding round — a repair after a record is checked for
    form, not for words: its words were right when the page was accepted."""
    found: list[str] = []
    ids = [n.id for n in page.elements]
    if len(ids) != len(set(ids)):
        found.append("two elements share an id")
    known = set(ids)
    for n in page.elements:
        if n.kind in ("arrow", "line"):
            if not {n.source, n.target} <= known:
                found.append(f"{n.kind} {n.id} joins ids that are not in the manifest")
            elif n.source == n.target:
                found.append(f"{n.kind} {n.id} joins {n.source!r} to itself")
        if n.kind in ("text", "card", "figure", "bar", "panel") and not (
            n.text.strip() or n.about.strip()
        ):
            found.append(f"{n.id} writes nothing: name what it stands for")
        if said is not None and n.text.strip():
            missing = ground.unsaid(n.text, said)
            if missing:
                found.append(
                    f"{n.id} writes {n.text!r}, but {', '.join(repr(m) for m in missing)} is not "
                    "in the narration or the sources: use the narration's own words"
                )
    if said is not None:
        for c in page.claims:
            missing = ground.unsaid(c.text, said)
            if missing:
                found.append(
                    f"the claim {c.text!r} is not in the narration or the sources "
                    f"({', '.join(repr(m) for m in missing)}): change it to what they say, or "
                    "leave it out"
                )
    return found


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    # a and b are (x0, y0, x1, y1); `a` is already shrunk by GRAZE.
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def layout_problems(declared: list[Node], boxes: list[dict[str, object]]) -> list[str]:
    """What is wrong with where one scene's page put its declared things,
    from the boxes it reported at the end of that scene: each
    `{"id": "kind:id", "x", "y", "w", "h"}` in the video's pixels."""
    found: list[str] = []
    seen: list[tuple[str, float, float, float, float]] = []
    for b in boxes:
        what = str(b.get("id", "?"))
        try:
            x, y = float(b["x"]), float(b["y"])  # pyright: ignore[reportArgumentType]
            w, h = float(b["w"]), float(b["h"])  # pyright: ignore[reportArgumentType]
        except KeyError, TypeError, ValueError:
            found.append(f"{what} reported a box the lint cannot read")
            continue
        if w <= 0 or h <= 0:
            # Hidden at this moment (the page hides what is not yet on
            # screen): nothing to place, nothing to fault.
            continue
        seen.append((what, x, y, x + w, y + h))
        if x < EDGE or y < EDGE or x + w > WIDTH - EDGE or y + h > HEIGHT - EDGE:
            found.append(f"{what} is at least partly off the frame: keep every label inside it")
    expected = {f"{n.kind}:{n.id}" for n in declared if n.kind in BOXED}
    missing = sorted(expected - {b[0] for b in seen})
    if not expected:
        found.append(
            "the manifest declares nothing a viewer can see: write at least one label "
            "of the narration's own words"
        )
    elif not seen:
        found.append(
            "the page reported nothing it placed: every declared element must set "
            'data-mn="kind:id" on the node showing it'
        )
    elif missing:
        found.append(
            "these declared elements are not on screen (each visible thing needs "
            f'data-mn="kind:id"): {", ".join(missing[:6])}'
        )
    else:
        texts = [b for b in seen if b[0].startswith("text:")]
        for i, a in enumerate(texts):
            box = (a[1] + GRAZE, a[2] + GRAZE, a[3] - GRAZE, a[4] - GRAZE)
            for c in texts[i + 1 :]:
                if _overlap(box, (c[1], c[2], c[3], c[4])):
                    found.append(
                        f"{a[0][5:]} and {c[0][5:]} are written on top of each other: give "
                        "them room"
                    )
        if len(seen) >= FILL_AT:
            x0 = min(b[1] for b in seen)
            y0 = min(b[2] for b in seen)
            x1 = max(b[3] for b in seen)
            y1 = max(b[4] for b in seen)
            share = (x1 - x0) * (y1 - y0) / (WIDTH * HEIGHT)
            if share < MIN_FILL:
                found.append(
                    f"this scene's things fill only {share:.0%} of the frame; spread them over "
                    "the viewport and set their size"
                )
    return found


def counts(declared: list[Node], boxes: list[dict[str, object]]) -> tuple[int, int]:
    """How many things a scene declares a box for, and how many were on
    screen at its end."""
    want = {f"{n.kind}:{n.id}" for n in declared if n.kind in BOXED}
    got = {str(b.get("id", "")) for b in boxes}
    return len(want), len(want & got)
