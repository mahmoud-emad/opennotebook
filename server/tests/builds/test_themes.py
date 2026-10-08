"""Themes for whiteboard videos (docs/plans/video-themes.md): one look in
one place, the whiteboard's unchanged by them, and a theme carried from the
request to the last frame."""

import dataclasses
import os
import typing
from pathlib import Path

import numpy as np
import pytest
import skia
from httpx import AsyncClient

from opennotebook.api import video as video_api
from opennotebook.build.whiteboard import check, draw, frame, illustrate
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import (
    CARD_BAND,
    H,
    W,
    compile_illustrated,
    compile_scene,
)
from opennotebook.build.whiteboard.scene import TONES, PlannedScene
from tests.builds.test_whiteboard import (
    _scene,  # pyright: ignore[reportPrivateUsage]
    _segment,  # pyright: ignore[reportPrivateUsage]
    _when,  # pyright: ignore[reportPrivateUsage]
)

GOLDEN = Path(__file__).parent / "golden"
# Mean difference per channel, out of 255, that a golden image may differ by:
# enough for one skia build to anti-alias an edge unlike another, far too
# little for a colour, a line or a label to change.
TOLERANCE = 1.0


def _pixels(png: bytes) -> np.ndarray:
    img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    assert img is not None, "a PNG"
    return img.resize(960, 540).toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3]


def _frame_png(rgba: bytes) -> bytes:
    arr = np.frombuffer(rgba, dtype=np.uint8).reshape(H, W, 4)
    data = skia.Image.fromarray(arr, colorType=skia.kRGBA_8888_ColorType).encodeToData()
    assert data is not None
    return bytes(data)


def _like_golden(name: str, png: bytes) -> None:
    """`png` looks like the golden image `name`; with UPDATE_GOLDEN=1 it
    becomes it, for a change of look made on purpose."""
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
        img.resize(960, 540).save(str(path), skia.kPNG)
        return
    want = _pixels(path.read_bytes())
    got = _pixels(png)
    diff = float(np.abs(want.astype(np.int16) - got.astype(np.int16)).mean())
    assert diff < TOLERANCE, f"{name} differs from its golden image by {diff:.2f}/255"


# ── the whiteboard, unchanged ────────────────────────────────────────────────


def test_the_whiteboard_looks_as_it_did_before_themes(tmp_path: Path) -> None:
    """Drawing, holding, wiping, the check's still and a slide's board, all
    as they were made before there were themes."""
    seg = dataclasses.replace(_segment(tmp_path, frames=60), wipe=True)
    frames = [f for f, _ in draw.frames(seg)]
    for i in (25, 45, 58):
        _like_golden(f"whiteboard-frame-{i}.png", _frame_png(frames[i]))
    _like_golden("whiteboard-still.png", check.still(_scene(), _when, 0, 6000))
    board = frame.board_png(frame.board("Recap", ["One", "Two"], ticked=True))
    _like_golden("whiteboard-board.png", board)


# ── a theme in one place ─────────────────────────────────────────────────────


def test_every_theme_is_complete_and_the_api_names_them_all() -> None:
    assert th.DEFAULT == "whiteboard" and next(iter(th.THEMES)) == th.DEFAULT
    for t in th.THEMES.values():
        assert set(t.ink) == set(TONES), t.id
        assert (Path(frame.ASSETS) / t.font).exists(), t.id
    assert set(typing.get_args(video_api.ThemeId)) == set(th.THEMES)


def test_an_unknown_theme_is_the_whiteboard_and_a_theme_does_not_leak() -> None:
    assert th.theme_of(None) is th.WHITEBOARD
    assert th.theme_of("no-such-theme") is th.WHITEBOARD
    other = dataclasses.replace(th.WHITEBOARD, id="other", paper=(0, 0, 0))
    with th.using(other):
        assert th.current() is other
    assert th.current() is th.WHITEBOARD


def test_frames_set_their_theme_only_while_drawing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dark = dataclasses.replace(th.WHITEBOARD, id="dark", paper=(0x10, 0x10, 0x10))
    monkeypatch.setitem(th.THEMES, "dark", dark)
    seg = dataclasses.replace(_segment(tmp_path, frames=4), theme="dark")
    for f, _ in draw.frames(seg):
        # Between frames, the reader is back in its own theme.
        assert th.current() is th.WHITEBOARD
        assert f[:3] == bytes((0x10, 0x10, 0x10)), "the frame is on the theme's paper"


# ── from the request to the video ────────────────────────────────────────────


async def test_the_api_lists_themes_and_refuses_one_it_does_not_have(client: AsyncClient) -> None:
    got = (await client.get("/api/video/themes")).json()
    assert got[0] == {"id": "whiteboard", "label": "Whiteboard", "family": "drawn"}
    r = await client.post(
        "/api/sessions/01a1108a-1435-7da5-9e45-707d1c9ee178/video",
        json={"style": "whiteboard", "theme": "no-such-theme"},
    )
    assert r.status_code == 422


# ── the drawn themes ─────────────────────────────────────────────────────────


def _luminance(c: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = c
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _under_highlight(t: th.Theme) -> tuple[int, int, int]:
    """The paper's colour where the highlighter has been."""
    p, h, a = t.paper, t.highlight, t.highlight_alpha
    over = (
        tuple(pp * hh // 255 for pp, hh in zip(p, h, strict=True))
        if t.highlight_blend == "multiply"
        else h
    )
    r, g, b = (round(pp + (oo - pp) * a) for pp, oo in zip(p, over, strict=True))
    return (r, g, b)


# WCAG's bar for large text: every label is at least 24 px.
LARGE_TEXT = 3.0
# What a theme added since must reach, everywhere.
NEW_THEMES = 4.0
# The whiteboard as it was before themes, kept as it was: its amber and green
# on its yellow highlighter fall short of the large-text bar.
KNOWN_WEAK = {("whiteboard", "amber"), ("whiteboard", "green")}


@pytest.mark.parametrize("theme_id", list(th.THEMES))
def test_every_ink_reads_on_its_paper_and_its_highlight(theme_id: str) -> None:
    t = th.THEMES[theme_id]
    lit = _under_highlight(t)
    for tone, ink in t.ink.items():
        assert _contrast(ink, t.paper) >= LARGE_TEXT, (theme_id, tone)
        if (theme_id, tone) not in KNOWN_WEAK:
            assert _contrast(ink, lit) >= LARGE_TEXT, (theme_id, tone, "highlighted")
        if theme_id != "whiteboard":
            assert min(_contrast(ink, t.paper), _contrast(ink, lit)) >= NEW_THEMES, (theme_id, tone)


@pytest.mark.parametrize("theme_id", list(th.THEMES))
def test_each_theme_starts_on_its_paper_and_draws_the_same_every_time(
    tmp_path: Path, theme_id: str
) -> None:
    seg = dataclasses.replace(_segment(tmp_path, frames=30), theme=theme_id)
    a = [f for f, _ in draw.frames(seg)]
    b = [f for f, _ in draw.frames(seg)]
    assert a == b, "a render is deterministic"
    with th.using(theme_id):
        surface = skia.Surface(W, H)
        draw.paper(surface.getCanvas())
        blank = surface.makeImageSnapshot().tobytes()
    assert a[0] == blank, "the first frame is the empty paper"
    assert len(set(a)) > 5, "the board changes as it is drawn"


@pytest.mark.parametrize(
    "theme_id", [t.id for t in th.THEMES.values() if t.family == "drawn" and t.id != "whiteboard"]
)
def test_each_drawn_theme_looks_as_designed(tmp_path: Path, theme_id: str) -> None:
    """A still of the test scene in each theme, against its golden image:
    regenerate on purpose with UPDATE_GOLDEN=1."""
    _like_golden(f"{theme_id}-still.png", check.still(_scene(), _when, 0, 6000, theme_id))
    with th.using(theme_id):
        board = frame.board_png(frame.board("Recap", ["One", "Two"], ticked=True))
    _like_golden(f"{theme_id}-board.png", board)


@pytest.mark.parametrize("theme_id", list(th.THEMES))
def test_each_theme_has_its_own_slides(theme_id: str) -> None:
    t = th.THEMES[theme_id]
    with th.using(theme_id):
        page = frame.opening_html("Plants", "How plants eat.", ["Light"], [0], 3)
    assert f"--paper:{t.slides.paper}" in page and f"--accent:{t.slides.accent}" in page


# ── the filled themes ────────────────────────────────────────────────────────


@pytest.mark.parametrize("theme_id", [t.id for t in th.THEMES.values() if t.fill == "solid"])
def test_a_label_reads_on_its_cut_paper(theme_id: str) -> None:
    t = th.THEMES[theme_id]
    for tone, ink in t.ink.items():
        assert _contrast(ink, t.fills[tone]) >= NEW_THEMES, (theme_id, tone)


def test_a_filled_theme_fills_its_shapes_and_cut_paper_has_no_outline() -> None:
    def kinds(theme_id: str, element: str) -> list[str]:
        d = compile_scene(_scene(), _when, 0, 6000, theme_id)
        return [p.kind for p in d.pieces if p.element == element]

    assert "fill" not in kinds("whiteboard", "k")
    assert kinds("retro", "k")[0] == "fill" and "line" in kinds("retro", "k")
    assert kinds("papercraft", "k")[0] == "fill" and "line" not in kinds("papercraft", "k")
    # An icon on cut paper sits on a disc of its own.
    assert kinds("papercraft", "cpu")[0] == "fill" and "fill" not in kinds("retro", "cpu")


# ── the illustrated themes ───────────────────────────────────────────────────


def test_a_picture_is_asked_for_by_its_brief_never_its_words() -> None:
    ps = PlannedScene(lines=["l0"], title="The kernel", brief="A kernel between programs and a CPU",
                      concepts=["kernel", "CPU"])  # fmt: skip
    ask = illustrate.prompt(th.THEMES["watercolor"], ps)
    assert "watercolour" in ask and "A kernel between programs and a CPU" in ask
    assert "No text, letters, numbers" in ask
    # A custom style is the person's own words, cut to length, the rules kept.
    custom = illustrate.prompt(th.THEMES["watercolor"], ps, "x" * 500)
    assert (
        "x" * illustrate.CUSTOM_CHARS in custom
        and "x" * (illustrate.CUSTOM_CHARS + 1) not in custom
    )
    assert "No text, letters, numbers" in custom


def test_an_illustrated_scene_sets_its_words_on_cards_at_its_foot() -> None:
    with th.using("watercolor"):
        d = compile_illustrated(_scene(), _when, 0, 6000)
    cards = [p for p in d.pieces if p.kind == "card" and p.element != "title"]
    said = [p.said for p in d.pieces if p.kind == "text" and p.element != "title"]
    assert said == ["processor", "kernel", "kernel runs processor"]
    assert all(CARD_BAND[0] <= c.box[1] and c.box[3] <= CARD_BAND[1] for c in cards)
    assert not [p for p in d.pieces if p.kind == "line"], "nothing is drawn over a picture"


@pytest.mark.parametrize(
    "theme_id", [t.id for t in th.THEMES.values() if t.family == "illustrated"]
)
def test_an_illustrated_theme_falls_back_to_a_drawn_one(theme_id: str) -> None:
    t = th.THEMES[theme_id]
    assert t.style and th.THEMES[t.twin].family == "drawn"
