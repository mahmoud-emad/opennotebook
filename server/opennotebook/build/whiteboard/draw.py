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
import json
import math
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any

import skia

from opennotebook.build.whiteboard.compile import H, Piece, W, compile_scene
from opennotebook.build.whiteboard.scene import Beat, Scene

FPS = 30
BOARD = (0xFB, 0xFA, 0xF6)
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


def when_of(seg: Segment) -> Any:
    def when(b: Beat) -> float:
        ws = seg.words.get(b.line)
        if ws:
            return ws[min(b.word, len(ws) - 1)]
        return seg.line_starts.get(b.line, seg.start_ms)

    return when


def _paint(color: tuple[int, int, int], kind: str, width: float = 5.0) -> skia.Paint:
    p = skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(*color))
    if kind == "line":
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(width)
        p.setStrokeCap(skia.Paint.kRound_Cap)
        p.setStrokeJoin(skia.Paint.kRound_Join)
    elif kind == "wash":
        # Multiplied, so a highlight laid over lines leaves them dark.
        p.setBlendMode(skia.BlendMode.kMultiply)
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
    if p.kind == "line":
        n = len(p.points)
        k = max(2, min(n, math.ceil(n * f)))
        c.drawPath(_poly(p.points[:k]), _paint(p.color, "line", p.width))
        return p.points[k - 1] if f < 1 else None
    if p.kind == "text" and p.text is not None:
        x, y = p.at
        if f >= 1:
            # Finished: drawn whole. Letters reach past their advance width,
            # and a clip at the width cut each label's last one.
            c.save()
            c.translate(x, y)
            c.drawPath(p.text.path, _paint(p.color, "text"))
            c.restore()
            return None
        right = max(p.text.path.getBounds().right(), p.text.width)
        edge = x + right * f
        c.save()
        c.clipRect(
            skia.Rect.MakeLTRB(x - 12, y - p.text.ascent * 1.8, edge, y + p.text.descent * 2.5)
        )
        c.translate(x, y)
        c.drawPath(p.text.path, _paint(p.color, "text"))
        c.restore()
        return (edge, y - p.text.ascent * 0.3)
    x0, y0, x1, y1 = p.box
    paint = _paint(p.color, "wash")
    paint.setAlphaf(min(f, 1.0) * 0.85)
    c.drawRoundRect(skia.Rect.MakeLTRB(x0, y0, x1, y1), 18, 18, paint)
    return None


def _marker(c: skia.Canvas, at: tuple[float, float], t: float) -> None:
    """A marker pen whose tip is on the stroke being drawn, with a little
    hand tremor."""
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
    c.drawPath(tip, skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(0x1F, 0x29, 0x37)))
    c.drawRoundRect(
        skia.Rect.MakeXYWH(12, -12, 22, 24),
        3,
        3,
        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(0xE5, 0xE7, 0xEB)),
    )
    c.drawRoundRect(
        skia.Rect.MakeXYWH(34, -15, 100, 30),
        8,
        8,
        skia.Paint(AntiAlias=True, Color=skia.ColorSetRGB(0x25, 0x63, 0xEB)),
    )
    c.restore()


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
        c.clear(skia.ColorSetRGB(*BOARD))
        c.drawImageRect(img, skia.Rect.MakeWH(W, H))
        if wiping:
            wipe = skia.Paint(Color=skia.ColorSetRGB(*BOARD))
            wipe.setAlphaf(min((t - (seg.end_ms - WIPE_MS)) / WIPE_MS, 1.0))
            c.drawRect(skia.Rect.MakeWH(W, H), wipe)
        pixels: bytes = frame.makeImageSnapshot().tobytes()
        last = pixels
        yield pixels, True


def frames(seg: Segment) -> Iterator[tuple[bytes, bool]]:
    """The segment's frames as raw RGBA, each with whether it differs from
    the one before. A board holding still repeats the same bytes, without
    being drawn again."""
    if seg.still:
        yield from _still_frames(seg)
        return
    drawing = compile_scene(Scene.model_validate(seg.scene), when_of(seg), seg.start_ms, seg.end_ms)
    pieces = sorted(drawing.pieces, key=lambda p: p.start_ms)
    info = skia.ImageInfo.Make(W, H, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
    board = skia.Surface.MakeRaster(info)
    board.getCanvas().clear(skia.ColorSetRGB(*BOARD))
    board_img = board.makeImageSnapshot()
    frame = skia.Surface.MakeRaster(info)
    done = 0
    last: bytes | None = None
    for i in range(seg.frames):
        t = (seg.first_frame + i) * 1000 / FPS
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
            yield last, False
            continue
        c = frame.getCanvas()
        c.drawImage(board_img, 0, 0)
        tip = None
        for p in pieces[done:]:
            if p.start_ms >= t:
                break
            span = max(p.end_ms - p.start_ms, 1.0)
            tip = draw_piece(c, p, (t - p.start_ms) / span) or tip
        if wiping:
            wipe = skia.Paint(Color=skia.ColorSetRGB(*BOARD))
            wipe.setAlphaf(min((t - (seg.end_ms - WIPE_MS)) / WIPE_MS, 1.0))
            c.drawRect(skia.Rect.MakeWH(W, H), wipe)
        elif tip is not None:
            _marker(c, tip, t)
        pixels: bytes = frame.makeImageSnapshot().tobytes()
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
