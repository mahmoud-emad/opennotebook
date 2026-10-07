"""Render the theme picker's thumbnails: one sample scene, drawn in each
whiteboard theme, written to web/public/themes/<id>.jpg.

Run from server/ after adding or changing a theme, and commit the images:

    uv run python scripts/theme_previews.py
"""

from pathlib import Path

import skia

from opennotebook.build.whiteboard import draw
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import compile_scene
from opennotebook.build.whiteboard.scene import Scene

OUT = Path(__file__).resolve().parents[2] / "web" / "public" / "themes"
SIZE = (384, 216)

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


def render(theme_id: str) -> skia.Image:
    with th.using(theme_id):
        d = compile_scene(SAMPLE, lambda b: b.word * 400.0, 0, 4000)
        surface = skia.Surface(1920, 1080)
        c = surface.getCanvas()
        draw.paper(c)
        for p in sorted(d.pieces, key=lambda p: p.start_ms):
            draw.draw_piece(c, p, 1.0)
        # Crop to the drawing, as the picker shows a corner of the board.
        crop = surface.makeImageSnapshot().makeSubset(skia.IRect.MakeXYWH(0, 0, 1920, 760))
        assert crop is not None
        return crop.resize(*SIZE)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for theme_id in th.THEMES:
        render(theme_id).save(str(OUT / f"{theme_id}.jpg"), skia.kJPEG, 84)
        print(OUT / f"{theme_id}.jpg")


if __name__ == "__main__":
    main()
