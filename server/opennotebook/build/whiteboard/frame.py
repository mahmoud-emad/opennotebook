"""The frame of a whiteboard video: its opening and closing slides.

A video made for people opens on what it is and what it covers, and closes
on what to remember and a thanks, the way an explainer on camera does
(NotebookLM's hosts open by saying what they will walk you through, and
close on the takeaways). Both are fixed slides, not drawings: each is shown
whole while the presenter speaks over it, and fades to the board after.

* **The opening**: the title, a sentence on what the video is about, and
  "In this video": each chapter with the time it starts.
* **The closing**: "Recap": the takeaways, each ticked, and the thanks.

What they write is the presenter's (`presenter.py`), held to the same rule
as the narration: nothing the script and its sources do not say. They are
drawn by the browser that draws a deck's slides, in the board's paper and
ink and its handwriting for the headings, so they belong to the video; with
no browser, the studio draws them on the board itself (`board_png`).
"""

import base64
import html
from functools import cache
from pathlib import Path

from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import compile_scene
from opennotebook.build.whiteboard.scene import COLS, Beat, Element, Scene

ASSETS = Path(__file__).parent / "assets"
OPENING = "Introduction"
CLOSING = "Recap"
THANKS = "Thanks for watching"
# The most chapters the opening lists: more is a list nobody reads.
MOST = 10

W, H = 1920, 1080


@cache
def _hand(name: str) -> str:
    """A theme's hand, for the slide to embed."""
    return base64.b64encode((ASSETS / name).read_bytes()).decode()


def _clock(ms: int) -> str:
    s = max(ms, 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


def _page(body: str) -> str:
    """A slide in the theme in use: its colours are CSS variables, set from
    the theme's `Slides`."""
    look = th.current()
    v = look.slides
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:Hand;src:url(data:font/ttf;base64,{_hand(look.font)}) format("truetype")}}
:root{{--paper:{v.paper};--ink:{v.ink};--muted:{v.muted};--soft:{v.soft};--accent:{v.accent};
--card:{v.card};--edge:{v.edge};--rule:{v.rule};--tick:{v.tick};--on-accent:{v.on_accent}}}
html,body{{margin:0;width:{W}px;height:{H}px;background:var(--paper);color:var(--ink);
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{position:absolute;inset:110px 140px;display:grid;grid-template-columns:1.05fr .95fr;
gap:90px;align-items:center}}
.kicker{{font-size:30px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
color:var(--accent);margin:0 0 22px}}
h1{{font-family:Hand,cursive;font-size:118px;line-height:1.02;margin:0 0 34px;color:var(--ink)}}
.about{{font-size:38px;line-height:1.4;color:var(--soft);margin:0}}
.card{{background:var(--card);border:3px solid var(--edge);border-radius:28px;padding:44px 50px;
box-shadow:10px 12px 0 var(--edge)}}
.card h2{{font-family:Hand,cursive;font-size:64px;margin:0 0 18px;color:var(--accent)}}
ol,ul{{list-style:none;margin:0;padding:0}}
li{{display:flex;align-items:baseline;gap:22px;font-size:34px;line-height:1.3;padding:13px 0;
border-top:2px dashed var(--rule)}}
li:first-child{{border-top:0}}
.n{{flex:0 0 auto;width:52px;height:52px;border-radius:50%;background:var(--accent);
color:var(--on-accent);display:grid;place-items:center;font-size:28px;font-weight:700;align-self:center}}
.t{{flex:1}}
.at{{color:var(--muted);font-variant-numeric:tabular-nums;font-size:28px}}
.tick{{flex:0 0 auto;color:var(--tick);font-size:40px;font-weight:800;align-self:center}}
.thanks{{font-family:Hand,cursive;font-size:132px;line-height:1;margin:0 0 30px;
color:var(--ink)}}
.of{{font-size:34px;color:var(--muted);margin:0}}
</style></head><body><main>{body}</main></body></html>"""


def opening_html(title: str, about: str, agenda: list[str], starts: list[int], minutes: int) -> str:
    """The opening slide: what the video is, and its chapters with times."""
    esc = html.escape
    rows = "".join(
        f'<li><span class="n">{i + 1}</span><span class="t">{esc(item)}</span>'
        f'<span class="at">{_clock(at)}</span></li>'
        for i, (item, at) in enumerate(zip(agenda[:MOST], starts, strict=False))
    )
    length = f"Video overview · {minutes} min" if minutes > 0 else "Video overview"
    about_p = f'<p class="about">{esc(about)}</p>' if about else ""
    return _page(
        f'<section><p class="kicker">{esc(length)}</p><h1>{esc(title)}</h1>{about_p}</section>'
        f'<section class="card"><h2>In this video</h2><ol>{rows}</ol></section>'
    )


def closing_html(title: str, takeaways: list[str]) -> str:
    """The closing slide: the takeaways, ticked, and the thanks."""
    esc = html.escape
    rows = "".join(
        f'<li><span class="tick">✓</span><span class="t">{esc(t)}</span></li>' for t in takeaways
    )
    return _page(
        f'<section class="card"><h2>{CLOSING}</h2><ul>{rows}</ul></section>'
        f'<section><p class="thanks">{THANKS}!</p><p class="of">{esc(title)}</p></section>'
    )


# ── without a browser: the slide drawn on the board ──────────────────────────


def _el(**v: object) -> Element:
    return Element.model_validate(v)


def board(heading: str, items: list[str], ticked: bool) -> Scene:
    """A slide as a finished board: its heading in the title band, its items
    one per row, centred, numbered or ticked."""
    items = items[:6]
    first = (6 - len(items)) // 2
    beat = Beat(line="", word=0)
    elements: list[Element] = []
    for k, item in enumerate(items):
        label = item if ticked else f"{k + 1} · {item}"
        at = f"{COLS[0]}{first + k + 1}"
        kind = "box" if ticked else "label"
        elements.append(_el(id=f"t{k}", kind=kind, label=label, at=at, span=[6, 1], beat=beat))
    if not elements:
        elements.append(_el(id="t0", kind="label", label=heading, at="A3", span=[6, 2], beat=beat))
    return Scene(title=heading, layout="stack", elements=elements)


def board_png(sc: Scene, theme: th.Theme | str | None = None) -> bytes:
    """A scene as a finished board, a PNG at the video's size, in the theme
    given (or the one in use)."""
    import skia

    from opennotebook.build.whiteboard.draw import draw_piece, paper

    with th.using(theme if theme is not None else th.current()):
        d = compile_scene(sc, lambda _b: 0.0, 0.0, 1000.0)
        surface = skia.Surface(W, H)
        c = surface.getCanvas()
        paper(c)
        for p in d.pieces:
            draw_piece(c, p, 1.0)
    data = surface.makeImageSnapshot().encodeToData()
    return bytes(data) if data is not None else b""
