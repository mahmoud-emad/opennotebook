"""Render the theme picker's thumbnails: one sample scene, drawn in each
whiteboard theme, written to web/public/themes/<id>.jpg.

Run from server/ after adding or changing a theme, and commit the images:

    uv run python scripts/theme_previews.py
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from pathlib import Path

import skia

from opennotebook.build.whiteboard import draw
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import Drawing, H, W, compile_illustrated, compile_scene
from opennotebook.build.whiteboard.scene import Beat, Scene

OUT = Path(__file__).resolve().parents[2] / "web" / "public" / "themes"
SIZE = (384, 216)
# A drawn theme's thumbnail is the top of the board, this tall: the picker
# shows a corner of it.
DRAWN_CROP_H = 760
# The sample's drawing ends by then, ms.
END_MS = 4000
JPEG_QUALITY = 84

SAMPLE = Scene.model_validate(
    {
        "title": "How plants eat",
        "layout": "flow",
        "elements": [
            {"id": "sun", "kind": "icon", "icon": "sun", "label": "Light", "tone": "amber",
             "at": "A2", "span": [2, 2], "beat": {"line": "l", "word": 0}},
            {"id": "leaf", "kind": "icon", "icon": "leaf", "label": "Leaf", "tone": "green",
             "at": "C2", "span": [2, 2], "beat": {"line": "l", "word": 1}},
            {"id": "sugar", "kind": "box", "label": "Sugar", "tone": "blue", "at": "E2",
             "span": [2, 1], "beat": {"line": "l", "word": 2}},
            {"kind": "arrow", "from": "sun", "to": "leaf", "beat": {"line": "l", "word": 1}},
            {"kind": "arrow", "from": "leaf", "to": "sugar", "beat": {"line": "l", "word": 2}},
        ],
        "highlight": [{"target": "sugar", "beat": {"line": "l", "word": 3}}],
    }
)  # fmt: skip


def _wash(c: skia.Canvas, look: th.Theme) -> None:
    """An illustrated theme's stand-in picture: soft blots of its inks, as a
    painting is suggested before it is made. Real sample pictures can take
    its place once made by the image model."""
    blur = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 70)
    tones = list(look.ink.values())
    spots = [(420, 360, 330), (980, 300, 300), (1500, 420, 340), (760, 640, 260), (1260, 680, 240)]
    for i, (x, y, r) in enumerate(spots):
        paint = skia.Paint(AntiAlias=True, MaskFilter=blur)
        paint.setColor(
            skia.Color4f.FromColor(skia.ColorSetRGB(*tones[(i + 1) % len(tones)]))
            .makeOpaque()
            .toColor()
        )
        paint.setAlphaf(0.28)
        c.drawCircle(x, y, r, paint)


def _when(b: Beat) -> float:
    """The sample's words, 400 ms apart."""
    return b.word * 400.0


def _board(d: Drawing, look: th.Theme) -> skia.Image:
    """The drawing finished on the theme's paper; an illustrated theme's on
    its stand-in picture."""
    surface = skia.Surface(W, H)
    c = surface.getCanvas()
    draw.paper(c)
    if look.family == "illustrated":
        _wash(c, look)
    for p in sorted(d.pieces, key=lambda p: p.start_ms):
        draw.draw_piece(c, p, 1.0)
    return surface.makeImageSnapshot()


def render(theme_id: str) -> skia.Image:
    """The theme's thumbnail."""
    look = th.THEMES[theme_id]
    with th.using(look):
        if look.family == "illustrated":
            return _board(compile_illustrated(SAMPLE, _when, 0, END_MS), look).resize(*SIZE)
        board = _board(compile_scene(SAMPLE, _when, 0, END_MS), look)
    crop = board.makeSubset(skia.IRect.MakeXYWH(0, 0, W, DRAWN_CROP_H))
    assert crop is not None
    return crop.resize(*SIZE)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for theme_id in th.THEMES:
        path = OUT / f"{theme_id}.jpg"
        render(theme_id).save(str(path), skia.kJPEG, JPEG_QUALITY)
        print(path)


if __name__ == "__main__":
    main()
