"""Frames of a scene being drawn, encoded as one video segment per scene
(docs/video-overview-spec.md, section 4.8).

Each frame is drawn with skia on the CPU: deterministic, and well under a
millisecond for a board (measured 2026-10-05), so the encoder sets the
pace. What is finished is kept on a board image and drawn once; a frame
copies the board and adds what is being drawn at that moment, and the
marker at its tip.

Scenes are rendered in separate processes, one segment each, and joined
without re-encoding. A segment is described by plain data (`Segment`), so a
process can be handed one, and it compiles the scene itself.
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import contextlib
import functools
import json
import math
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import skia

from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import H, Piece, W, compile_illustrated, compile_scene
from opennotebook.build.whiteboard.scene import Beat, Scene

FPS = 30
# The whiteboard's paper; a theme has its own (`paper`).
BOARD = th.WHITEBOARD.paper
# The board is wiped in this long at the end of a scene.
WIPE_MS = 280


@dataclass(frozen=True)
class Segment:
    """One scene to render: the scene, when each of its words is said
    (line id to each word's start, ms from the start of the video), and the
    frames it covers."""

    scene: dict[str, Any]
    words: dict[str, list[float]]
    line_starts: dict[str, float]
    start_ms: float
    end_ms: float
    first_frame: int
    frames: int
    wipe: bool
    out: str
    ffmpeg: str
    # A fixed slide shown whole instead of a drawing: a PNG at the video's
    # size (`frame.py`). The scene is then only what it says.
    still: str = ""
    # The theme it is drawn in, by id (`theme.py`).
    theme: str = th.DEFAULT
    # An illustrated scene's picture: a PNG at the video's size, shown with a
    # slow push-in under the scene's words on cards (`illustrate.py`).
    picture: str = ""


def when_of(seg: Segment) -> Any:
    def when(b: Beat) -> float:
        ws = seg.words.get(b.line)
        if ws:
            return ws[min(b.word, len(ws) - 1)]
        return seg.line_starts.get(b.line, seg.start_ms)

    return when


def _paint(color: tuple[int, int, int], kind: str, width: float = 5.0) -> skia.Paint:
    p = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*color))
    if kind in ("line", "text") and th.current().pen == "chalk":
        # Chalk: the ink only where the grain lets it through. The grain is in
        # the board's own coordinates, so it stays put from frame to frame.
        p.setShader(_chalk(color))
    if kind == "line":
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(width)
        p.setStrokeCap(skia.Paint.kRound_Cap)
        p.setStrokeJoin(skia.Paint.kRound_Join)
    elif kind == "wash" and th.current().highlight_blend == "multiply":
        # Multiplied, so a highlight laid over lines leaves them dark. On
        # dark paper it is laid over instead, lighter (`theme.py`).
        p.setBlendMode(skia.BlendMode.kMultiply)
    return p


def paper(c: skia.Canvas) -> None:
    """The theme's paper, and what is printed on it: what every board starts
    as and every wipe returns to."""
    look = th.current()
    c.clear(skia.ColorSetRGB(*look.paper))
    if look.background == "plain":
        return
    rule = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*look.rule), StrokeWidth=1.6)
    step = look.spacing
    if look.background == "lined":
        # Ruled under the title band, as a notebook's first line is.
        for y in range(160, H, step):
            c.drawLine(0, y, W, y, rule)
        if look.margin is not None:
            red = skia.ColorSetRGB(*look.margin)
            margin = skia.Paint(AntiAlias=True, Color=red, StrokeWidth=2.5)
            c.drawLine(88, 0, 88, H, margin)
    elif look.background == "grid":
        for y in range(step, H, step):
            c.drawLine(0, y, W, y, rule)
        for x in range(step, W, step):
            c.drawLine(x, 0, x, H, rule)
    elif look.background == "slate":
        c.drawImage(_slate(look.paper), 0, 0)
    elif look.background in ("newsprint", "card"):
        c.drawImage(_stock(look.paper, look.background), 0, 0)


def _noise(size: int, seed: int) -> np.ndarray:
    """Random values in [0, 1), the same every time for one seed."""
    return np.random.default_rng(seed).random((size, size), dtype=np.float32)


@functools.cache
def _grain() -> skia.Image:
    """Chalk's grain: a tile that is mostly solid, with pits where the chalk
    skipped the slate's surface."""
    n = _noise(256, 7)
    alpha = np.where(n < 0.16, 0.18, 0.62 + 0.38 * _noise(256, 11))
    rgba = np.zeros((256, 256, 4), dtype=np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    rgba[..., :3] = rgba[..., 3:4]  # premultiplied white
    return skia.Image.fromarray(rgba, colorType=skia.kRGBA_8888_ColorType)


@functools.cache
def _chalk(color: tuple[int, int, int]) -> skia.Shader:
    grain = _grain().makeShader(skia.TileMode.kRepeat, skia.TileMode.kRepeat)
    ink = skia.Shaders.Color(skia.ColorSetRGB(*color))
    return skia.Shaders.Blend(skia.BlendMode.kDstIn, ink, grain)


@functools.cache
def _stock(base: tuple[int, int, int], kind: str) -> skia.Image:
    """Paper with a grain: newsprint's fine specks, or card's soft fibres."""
    n = _noise(512, 5 if kind == "newsprint" else 9)
    if kind == "card":
        # Fibres: the noise smeared along one direction.
        n = (n + np.roll(n, 1, axis=1) + np.roll(n, 2, axis=1) + np.roll(n, 3, axis=1)) / 4
        shade = (n - 0.5) * 0.10
    else:
        shade = np.where(n > 0.985, -0.22, (n - 0.5) * 0.05)
    rgb = np.clip(np.array(base, dtype=np.float32) / 255 * (1 + shade[..., None]), 0, 1)
    tile = np.dstack([rgb * 255, np.full((512, 512, 1), 255.0)]).astype(np.uint8)
    img = skia.Image.fromarray(np.ascontiguousarray(tile), colorType=skia.kRGBA_8888_ColorType)
    surface = skia.Surface(W, H)
    paint = skia.Paint(Shader=img.makeShader(skia.TileMode.kRepeat, skia.TileMode.kRepeat))
    surface.getCanvas().drawRect(skia.Rect.MakeWH(W, H), paint)
    return surface.makeImageSnapshot()


# How strong a halftone screen is printed: its dots cover about a seventh of
# the shape, at this strength.
HALFTONE_ALPHA = 0.42


@functools.cache
def _dots() -> skia.Image:
    """A halftone screen's tile: one dot, for a shape's fill to show through."""
    size, r = 12, 2.6
    yy, xx = np.mgrid[0:size, 0:size] + 0.5
    d = np.hypot(xx - size / 2, yy - size / 2)
    alpha = np.clip(r + 0.5 - d, 0, 1)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    rgba[..., :3] = rgba[..., 3:4]
    return skia.Image.fromarray(rgba, colorType=skia.kRGBA_8888_ColorType)


@functools.cache
def _halftone(color: tuple[int, int, int]) -> skia.Shader:
    screen = _dots().makeShader(skia.TileMode.kRepeat, skia.TileMode.kRepeat)
    return skia.Shaders.Blend(
        skia.BlendMode.kDstIn, skia.Shaders.Color(skia.ColorSetRGB(*color)), screen
    )


@functools.cache
def _slate(base: tuple[int, int, int]) -> skia.Image:
    """A used slate: faint clouds of old chalk wiped across it."""
    surface = skia.Surface(W, H)
    c = surface.getCanvas()
    c.clear(skia.ColorSetRGB(*base))
    rnd = np.random.default_rng(3)
    blur = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 90)
    cloud = skia.Paint(AntiAlias=True, MaskFilter=blur)
    for _ in range(14):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        rx, ry = rnd.uniform(220, 520), rnd.uniform(90, 220)
        cloud.setColor(skia.Color4f(1, 1, 1, float(rnd.uniform(0.025, 0.055))).toColor())
        c.drawOval(skia.Rect.MakeXYWH(x - rx, y - ry, 2 * rx, 2 * ry), cloud)
    return surface.makeImageSnapshot()


def _faded(p: skia.Paint, alpha: float) -> skia.Paint:
    if alpha < 1.0:
        p.setAlphaf(alpha)
    return p


def _poly(points: list[tuple[float, float]]) -> skia.Path:
    path = skia.Path()
    if points:
        path.moveTo(*points[0])
        for pt in points[1:]:
            path.lineTo(*pt)
    return path


def draw_piece(c: skia.Canvas, p: Piece, f: float) -> tuple[float, float] | None:
    """Draw `p`, the share `f` of it done, and return where the pen is."""
    if f <= 0:
        return None
    look = th.current()
    if p.kind == "fill" and p.shape is not None:
        _fill(c, p, min(f, 1.0))
        return None
    if p.kind == "card":
        _card(c, p, min(f, 1.0))
        return None
    if look.ghost is not None and p.kind in ("line", "text"):
        # The second impression, out of register: in the ghost ink, or for
        # what is printed in that ink already, in the blue.
        ghost = look.ghost if p.color != look.ink["red"] else look.ink["blue"]
        c.save()
        c.translate(2.4, 1.8)
        _draw_ink(c, Piece(p.element, p.kind, ghost, points=p.points, text=p.text, at=p.at,
                           box=p.box, width=p.width), f, 0.32)  # fmt: skip
        c.restore()
    return _draw_ink(c, p, f, 1.0)


def _card(c: skia.Canvas, p: Piece, f: float) -> None:
    """A card of paper under words on a picture, laid down with its shadow."""
    x0, y0, x1, y1 = p.box
    rect = skia.RRect.MakeRectXY(skia.Rect.MakeLTRB(x0, y0, x1, y1), 14, 14)
    blur = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 10)
    shadow = skia.Paint(AntiAlias=True, MaskFilter=blur)
    shadow.setColor(skia.Color4f(0, 0, 0, 0.22 * f).toColor())
    c.save()
    c.translate(0, 4)
    c.drawRRect(rect, shadow)
    c.restore()
    paint = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*p.color))
    paint.setAlphaf(0.92 * f)
    c.drawRRect(rect, paint)


def _fill(c: skia.Canvas, p: Piece, f: float) -> None:
    """A shape filled in: a halftone screen wiped across it (print), or a
    piece of cut paper dropped into place with its shadow (craft)."""
    look = th.current()
    assert p.shape is not None
    x0, y0, x1, y1 = p.box
    if look.fill == "halftone":
        c.save()
        c.clipRect(skia.Rect.MakeLTRB(x0 - 8, y0 - 8, x0 + (x1 - x0 + 16) * f, y1 + 8))
        # A light screen: a tint behind the type, never as dark as the type.
        screen = skia.Paint(AntiAlias=True, Shader=_halftone(p.color))
        screen.setAlphaf(HALFTONE_ALPHA)
        c.drawPath(p.shape, screen)
        c.restore()
        return
    # Cut paper: it settles from a little above the card, its shadow
    # tightening as it lands.
    ease = 1 - (1 - f) ** 3
    scale = 1.08 - 0.08 * ease
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    c.save()
    c.translate(cx, cy)
    c.scale(scale, scale)
    c.translate(-cx, -cy)
    if look.shadow:
        blur = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 7 + 5 * (1 - ease))
        shadow = skia.Paint(AntiAlias=True, MaskFilter=blur)
        shadow.setColor(skia.Color4f(0.18, 0.13, 0.08, 0.28 * ease).toColor())
        c.save()
        c.translate(5 + 6 * (1 - ease), 7 + 8 * (1 - ease))
        c.drawPath(p.shape, shadow)
        c.restore()
    paint = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*p.color))
    paint.setAlphaf(min(1.0, 0.25 + ease))
    c.drawPath(p.shape, paint)
    c.restore()


def _draw_ink(c: skia.Canvas, p: Piece, f: float, alpha: float) -> tuple[float, float] | None:
    """A line, a label or a highlight, the share `f` of it done."""
    look = th.current()
    if p.kind == "line" and look.shadow:
        # Strips of paper: each line lifted off the card by its shadow.
        n = len(p.points)
        k = max(2, min(n, math.ceil(n * f)))
        blur = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 3)
        shade = _paint((0x2E, 0x22, 0x14), "line", p.width)
        shade.setMaskFilter(blur)
        shade.setAlphaf(0.25)
        c.save()
        c.translate(2.5, 3.5)
        c.drawPath(_poly(p.points[:k]), shade)
        c.restore()
    if p.kind == "line":
        n = len(p.points)
        k = max(2, min(n, math.ceil(n * f)))
        path = _poly(p.points[:k])
        if th.current().pen == "chalk":
            # Chalk's dusty edge: a wider, fainter pass under the line.
            dust = _paint(p.color, "line", p.width + 3.5)
            dust.setAlphaf(0.22)
            c.drawPath(path, dust)
        c.drawPath(path, _faded(_paint(p.color, "line", p.width), alpha))
        return p.points[k - 1] if f < 1 else None
    if p.kind == "text" and p.text is not None:
        x, y = p.at
        if f >= 1:
            # Finished: drawn whole. Letters reach past their advance width,
            # and a clip at the width cut each label's last one.
            c.save()
            c.translate(x, y)
            c.drawPath(p.text.path, _faded(_paint(p.color, "text"), alpha))
            c.restore()
            return None
        right = max(p.text.path.getBounds().right(), p.text.width)
        edge = x + right * f
        c.save()
        c.clipRect(
            skia.Rect.MakeLTRB(x - 12, y - p.text.ascent * 1.8, edge, y + p.text.descent * 2.5)
        )
        c.translate(x, y)
        c.drawPath(p.text.path, _faded(_paint(p.color, "text"), alpha))
        c.restore()
        return (edge, y - p.text.ascent * 0.3)
    x0, y0, x1, y1 = p.box
    paint = _paint(p.color, "wash")
    paint.setAlphaf(min(f, 1.0) * th.current().highlight_alpha)
    c.drawRoundRect(skia.Rect.MakeLTRB(x0, y0, x1, y1), 18, 18, paint)
    return None


def _marker(c: skia.Canvas, at: tuple[float, float], t: float) -> None:
    """A marker pen whose tip is on the stroke being drawn, with a little
    hand tremor."""
    if th.current().pen in ("print", "craft"):
        # Nothing holds a pen: a print appears, cut paper is laid down.
        return
    x, y = at
    c.save()
    c.translate(x, y + math.sin(t / 37) * 1.5)
    c.rotate(-35)
    shadow = skia.Paint(AntiAlias=True, Color=skia.Color4f(0, 0, 0, 0.18).toColor())
    c.drawRoundRect(skia.Rect.MakeXYWH(12, -4, 128, 30), 9, 9, shadow)
    tip = skia.Path()
    tip.moveTo(0, 0)
    tip.lineTo(12, -9)
    tip.lineTo(12, 9)
    tip.close()
    look = th.current()
    point, collar, body = look.tip
    if look.pen == "chalk":
        # A stick of chalk, its end worn round.
        stick = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*body))
        c.drawRoundRect(skia.Rect.MakeXYWH(-2, -11, 96, 22), 10, 10, stick)
        c.restore()
        return
    if look.pen in ("ballpoint", "technical"):
        # A slim pen: a fine point, a metal collar, a long body.
        tip = skia.Path()
        tip.moveTo(0, 0)
        tip.lineTo(16, -5)
        tip.lineTo(16, 5)
        tip.close()
        c.drawPath(tip, skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*point)))
        c.drawRoundRect(skia.Rect.MakeXYWH(16, -7, 18, 14), 3, 3,
                        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*collar)))  # fmt: skip
        c.drawRoundRect(skia.Rect.MakeXYWH(34, -9, 120, 18), 9, 9,
                        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*body)))  # fmt: skip
        c.restore()
        return
    c.drawPath(tip, skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*point)))
    c.drawRoundRect(
        skia.Rect.MakeXYWH(12, -12, 22, 24),
        3,
        3,
        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*collar)),
    )
    c.drawRoundRect(
        skia.Rect.MakeXYWH(34, -15, 100, 30),
        8,
        8,
        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*body)),
    )
    c.restore()


def _wipe(c: skia.Canvas, share: float) -> None:
    """Fade the frame back to the empty paper, `share` of the way."""
    look = th.current()
    paint = skia.Paint(Color=skia.ColorSetRGB(*look.paper))
    paint.setAlphaf(min(max(share, 0.0), 1.0))
    if look.background == "plain":
        c.drawRect(skia.Rect.MakeWH(W, H), paint)
    else:
        c.drawImage(_blank(look.id), 0, 0, skia.SamplingOptions(), paint)


@functools.cache
def _blank(theme_id: str) -> skia.Image:
    """A theme's empty paper, drawn once per process."""
    surface = skia.Surface(W, H)
    with th.using(theme_id):
        paper(surface.getCanvas())
    return surface.makeImageSnapshot()


def _still_frames(seg: Segment) -> Iterator[tuple[bytes, bool]]:
    """A fixed slide: the same frame throughout, fading to the board at the
    end when the segment wipes."""
    info = skia.ImageInfo.Make(W, H, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
    frame = skia.Surface.MakeRaster(info)
    img = skia.Image.open(seg.still)
    last: bytes | None = None
    for i in range(seg.frames):
        t = (seg.first_frame + i) * 1000 / FPS
        wiping = seg.wipe and t > seg.end_ms - WIPE_MS
        if last is not None and not wiping:
            yield last, False
            continue
        c = frame.getCanvas()
        with th.using(seg.theme):
            paper(c)
            c.drawImageRect(img, skia.Rect.MakeWH(W, H))
            if wiping:
                _wipe(c, (t - (seg.end_ms - WIPE_MS)) / WIPE_MS)
        pixels: bytes = frame.makeImageSnapshot().tobytes()
        last = pixels
        yield pixels, True


# How much an illustrated scene's picture is pushed in over the scene, and
# how long it fades in from the paper.
PUSH_IN = 0.06
FADE_IN_MS = 400


def _illustrated_frames(seg: Segment) -> Iterator[tuple[bytes, bool]]:
    """An illustrated scene: its picture, pushed in slowly toward a point of
    its own, with the scene's words laid on cards as they are said. Every
    frame differs (the picture moves), so none is held."""
    look = th.theme_of(seg.theme)
    with th.using(look):
        drawing = compile_illustrated(
            Scene.model_validate(seg.scene), when_of(seg), seg.start_ms, seg.end_ms
        )
    pieces = sorted(drawing.pieces, key=lambda p: p.start_ms)
    img = skia.Image.open(seg.picture)
    rnd = np.random.default_rng(seg.first_frame)
    fx, fy = float(rnd.uniform(0.3, 0.7)) * W, float(rnd.uniform(0.25, 0.6)) * H
    info = skia.ImageInfo.Make(W, H, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
    frame = skia.Surface.MakeRaster(info)
    span = max(seg.end_ms - seg.start_ms, 1.0)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear)
    for i in range(seg.frames):
        t = (seg.first_frame + i) * 1000 / FPS
        c = frame.getCanvas()
        with th.using(look):
            paper(c)
            zoom = 1.0 + PUSH_IN * min(max((t - seg.start_ms) / span, 0.0), 1.0)
            c.save()
            c.translate(fx, fy)
            c.scale(zoom, zoom)
            c.translate(-fx, -fy)
            c.drawImageRect(img, skia.Rect.MakeWH(W, H), sampling)
            c.restore()
            for p in pieces:
                if p.start_ms >= t:
                    break
                draw_piece(c, p, (t - p.start_ms) / max(p.end_ms - p.start_ms, 1.0))
            if t - seg.start_ms < FADE_IN_MS:
                _wipe(c, 1 - (t - seg.start_ms) / FADE_IN_MS)
            if seg.wipe and t > seg.end_ms - WIPE_MS:
                _wipe(c, (t - (seg.end_ms - WIPE_MS)) / WIPE_MS)
        yield frame.makeImageSnapshot().tobytes(), True


def frames(seg: Segment) -> Iterator[tuple[bytes, bool]]:
    """The segment's frames as raw RGBA, each with whether it differs from
    the one before. A board holding still repeats the same bytes, without
    being drawn again. Drawn in the segment's theme; the theme is set only
    while a frame is drawn, never across a `yield`, so it does not leak into
    whoever reads the frames."""
    if seg.still:
        yield from _still_frames(seg)
        return
    if seg.picture:
        yield from _illustrated_frames(seg)
        return
    look = th.theme_of(seg.theme)
    drawing = compile_scene(
        Scene.model_validate(seg.scene), when_of(seg), seg.start_ms, seg.end_ms, look
    )
    pieces = sorted(drawing.pieces, key=lambda p: p.start_ms)
    info = skia.ImageInfo.Make(W, H, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
    board = skia.Surface.MakeRaster(info)
    with th.using(look):
        paper(board.getCanvas())
    board_img = board.makeImageSnapshot()
    frame = skia.Surface.MakeRaster(info)
    done = 0
    last: bytes | None = None
    for i in range(seg.frames):
        t = (seg.first_frame + i) * 1000 / FPS
        with th.using(look):
            # Everything finished by now goes onto the board, once.
            committed = False
            while done < len(pieces) and pieces[done].end_ms <= t:
                draw_piece(board.getCanvas(), pieces[done], 1.0)
                done += 1
                committed = True
            if committed:
                board_img = board.makeImageSnapshot()
            active = done < len(pieces) and pieces[done].start_ms < t
            wiping = seg.wipe and t > seg.end_ms - WIPE_MS
            if last is not None and not (committed or active or wiping):
                pixels = None
            else:
                c = frame.getCanvas()
                c.drawImage(board_img, 0, 0)
                tip = None
                for p in pieces[done:]:
                    if p.start_ms >= t:
                        break
                    span = max(p.end_ms - p.start_ms, 1.0)
                    tip = draw_piece(c, p, (t - p.start_ms) / span) or tip
                if wiping:
                    _wipe(c, (t - (seg.end_ms - WIPE_MS)) / WIPE_MS)
                elif tip is not None:
                    _marker(c, tip, t)
                pixels = frame.makeImageSnapshot().tobytes()
        if pixels is None:
            assert last is not None
            yield last, False
            continue
        last = pixels
        yield pixels, True


# A board still for at least this many frames is encoded from one frame,
# converted once and repeated by the encoder, rather than piped frame by
# frame: piping and converting a 1080p frame is most of its cost (5.6 of
# 6.5 ms, measured), and most of a whiteboard's frames are a board holding
# still (70% in a measured scene).
HOLD_FRAMES = 15

ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "animation", "-crf", "20"]


def _encoder(seg: Segment, out: str, hold: int = 0) -> subprocess.Popen[bytes]:
    """An encoder for one piece of a segment: frames piped as they come,
    or with `hold`, one frame shown for that many."""
    args = [
        seg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
    ]  # fmt: skip
    if hold:
        clone = f"format=yuv420p,tpad=stop_mode=clone:stop={hold - 1}"
        args += ["-vf", clone, "-frames:v", str(hold)]
    args += [*ENCODER, "-pix_fmt", "yuv420p", "-r", str(FPS), out]
    return subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _finish(proc: subprocess.Popen[bytes]) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    err = proc.stderr.read() if proc.stderr else b""
    if proc.wait() != 0:
        raise RuntimeError(
            f"ffmpeg exited {proc.returncode}: {err.decode(errors='replace')[-600:]}"
        )


def render_segment(seg: Segment) -> list[str]:
    """Render one segment as one or more files in playing order, and return
    them: a run of drawing is piped frame by frame; a board still for
    `HOLD_FRAMES` or more is a file of its own made from one frame. Runs in
    a worker process."""
    stem = seg.out[: -len(".mp4")] if seg.out.endswith(".mp4") else seg.out
    files: list[str] = []
    live: subprocess.Popen[bytes] | None = None
    held: bytes | None = None
    count = 0

    def piece() -> str:
        name = f"{stem}-{len(files):03d}.mp4"
        files.append(name)
        return name

    def write(f: bytes, n: int = 1) -> None:
        nonlocal live
        if live is None:
            live = _encoder(seg, piece())
        assert live.stdin is not None
        for _ in range(n):
            live.stdin.write(f)

    def flush_hold() -> None:
        nonlocal live, held, count
        if held is None:
            return
        if count >= HOLD_FRAMES:
            if live is not None:
                _finish(live)
                live = None
            proc = _encoder(seg, piece(), hold=count)
            assert proc.stdin is not None
            proc.stdin.write(held)
            _finish(proc)
        else:
            write(held, count)
        held, count = None, 0

    procs: list[subprocess.Popen[bytes]] = []
    try:
        for f, changed in frames(seg):
            if changed:
                flush_hold()
                # A changed frame may itself start a hold: keep it until the
                # next one says whether the board stays as it is.
                held, count = f, 1
            else:
                count += 1
        flush_hold()
        if live is not None:
            procs.append(live)
            _finish(live)
            live = None
    except BaseException:
        for p in [*procs, *([live] if live is not None else [])]:
            p.kill()
            with contextlib.suppress(Exception):
                p.wait()
        raise
    with open(f"{stem}.parts.json", "w", encoding="utf-8") as fh:
        json.dump(files, fh)
    return files


def to_json(seg: Segment) -> str:
    return json.dumps(asdict(seg))


def main() -> None:
    """`python -m opennotebook.build.whiteboard.draw <segment.json>`: render
    one segment in a process of its own, so a render that is stopped can
    stop it at once."""
    with open(sys.argv[1], encoding="utf-8") as f:
        render_segment(Segment(**json.load(f)))


if __name__ == "__main__":
    main()
