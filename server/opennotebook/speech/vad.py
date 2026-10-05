"""Did this clip hold speech worth spending an answer on? The server half of
`opennotebook_vad`, ported.

# Two questions, one gate

The phase 2 spec records that the page's turn-ending gate and the server's
silence gate answer different questions, and that neither covers the other:

* **When does the turn end?** The page answers that live, in its
  `AudioWorklet`, because that is where the microphone is
  (`web/src/ui/playerGate.ts`).
* **Did this clip hold speech worth spending an answer on?** This module,
  offline, over the uploaded WAV, before the answer model is called, so
  silence is never billed.

Both drive the same `Gate`, with the same thresholds, over the same detector
fed the same way, so one recording gets the same answer from both. That is
why the detector here is the one the page runs, not a better one: two
detectors means two sets of thresholds, which is the drift the Rust crate
existed to prevent.

# Which detector

Silero VAD v5 (MIT, `silero/LICENSE`), run through onnxruntime. The model is
the file vad-web ships as `silero_vad_v5.onnx`, byte for byte: silero-vad's
own `src/silero_vad/data/silero_vad.onnx` at tag v5.1.2, sha256 `MODEL_SHA256`.
It is bundled rather than downloaded, so the gate needs nothing at start up
and nothing from the network.

It reads 512 samples at 16 kHz with the previous frame's last 64 samples in
front, as vad-web's `Silero.process` and silero-vad's `OnnxWrapper` both do,
so a frame is 32 ms here where earshot's was 16. The gate counts in frames of
whatever length the detector reads; its thresholds are in milliseconds and
probabilities, and did not move.
"""

import hashlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

log = logging.getLogger(__name__)

Samples = NDArray[np.float32]

# The detector's rate and frame.
SAMPLE_RATE = 16_000
FRAME_SIZE = 512
# Milliseconds of audio in one frame: 32.
FRAME_MS = FRAME_SIZE * 1000 // SAMPLE_RATE
# What Silero v5 reads in front of each frame: the tail of the previous one,
# so an onset straddling a frame boundary is still seen whole.
CONTEXT_SAMPLES = 64

MODEL_PATH = Path(__file__).parent / "silero" / "silero_vad_v5.onnx"
MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"


class NotAudio(ValueError):
    """The upload could not be read as audio. Malformed audio stays loud, per
    phase 2 §8: it is a different thing from silence, and a gate that read
    garbage and called it silence would hide it."""

    def __init__(self, why: str) -> None:
        super().__init__(f"WAV parse failed: {why}")
        self.sentence = (
            "The recording could not be read as audio. Check the microphone, then ask again."
        )


class DetectorMissing(RuntimeError):
    """The voice detector could not be loaded: the install is broken."""

    def __init__(self, why: str) -> None:
        super().__init__(f"the voice detector did not load: {why}")
        self.sentence = (
            "The studio's voice detector did not load, so the question was not sent. "
            "Reinstall the studio server, then try again."
        )


# ── the gate ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    """Every threshold in one place, shared with the page.

    The defaults are a starting point, not a tuned answer; a replay over
    recorded clips is what justifies moving one."""

    # Exponential smoothing on the raw per-frame probability, in [0, 1).
    # 0 disables it. Raw output is jumpy frame to frame and an unsmoothed
    # threshold chatters.
    smoothing: float = 0.4
    # Smoothed probability at or above which a frame starts speech.
    attack: float = 0.6
    # Smoothed probability below which a frame stops counting toward
    # `speech_ms`. Deliberately under `attack`: one threshold with no gap
    # flips on every frame sitting near it.
    #
    # It does NOT gate the hang countdown. Only a frame at or above `attack`
    # restarts that, so audio parked between the two thresholds can keep a
    # turn alive for `speech_ms` purposes without keeping it open forever.
    decay: float = 0.5
    # Consecutive attacking frames before the turn counts as speaking. This is
    # what rejects a door slam, which a loudness gate cannot do at all.
    onset_frames: int = 2
    # Trailing quiet that ends the turn. 2000 to match what the page did
    # before, so the detector changed and not the feel of the turn: changing
    # both at once makes a regression impossible to attribute.
    hang_ms: int = 2000
    # A clip holding less speech than this did not contain a question.
    min_speech_ms: int = 300


DEFAULT = Config()

Event = Literal["idle", "speaking", "ended"]


class Gate:
    """The frame-level decision, with no audio handling at all. The page runs
    the same one (`playerGate.ts`), and nothing else decides."""

    def __init__(self, cfg: Config = DEFAULT, frame_ms: int = FRAME_MS) -> None:
        self.cfg = cfg
        self.frame_ms = frame_ms
        self.reset()

    def reset(self) -> None:
        self.smoothed = 0.0
        self.run = 0
        self.speech_frames = 0
        self.quiet_ms = 0
        self.started = False
        self.ended_at_frame: int | None = None
        self.frame = 0

    @property
    def speech_ms(self) -> int:
        return self.speech_frames * self.frame_ms

    @property
    def heard_speech(self) -> bool:
        return self.started

    @property
    def ended_at_ms(self) -> int | None:
        """Millisecond offset at which the turn ended, if it did."""
        return None if self.ended_at_frame is None else self.ended_at_frame * self.frame_ms

    def step(self, raw: float) -> Event:
        """Feed one frame's raw probability; the state after it."""
        if self.ended_at_frame is not None:
            return "ended"
        self.frame += 1
        c = self.cfg
        self.smoothed = (
            self.smoothed * c.smoothing + raw * (1.0 - c.smoothing) if c.smoothing > 0 else raw
        )
        if self.smoothed >= c.attack:
            self.run += 1
            if self.run >= c.onset_frames:
                self.started = True
                self.quiet_ms = 0
                self.speech_frames += 1
        else:
            self.run = 0
            if self.started:
                # EVERYTHING below `attack` counts toward the hang, including
                # the band between `decay` and `attack`.
                #
                # The band used to reset `quiet_ms`, which reads as harmless
                # hysteresis and is not: a room that parks the smoothed
                # probability at 0.55 restarts the countdown on every frame and
                # the turn listens forever. That is the studio1 report, and
                # `a_noisy_room_still_ends_the_turn` is it at frame level.
                #
                # The band still counts as speech for `speech_ms`, because it
                # probably is speech. It just may not hold the floor.
                if self.smoothed >= c.decay:
                    self.speech_frames += 1
                self.quiet_ms += self.frame_ms
                # The min_speech_ms floor stops a cough plus two seconds of
                # quiet from sending an empty clip.
                if self.quiet_ms >= c.hang_ms and self.speech_ms >= c.min_speech_ms:
                    self.ended_at_frame = self.frame
        if self.ended_at_frame is not None:
            return "ended"
        return "speaking" if self.started else "idle"


# ── audio in ──────────────────────────────────────────────────────────────────


@dataclass
class Pcm:
    """Mono samples in [-1, 1] and their rate."""

    samples: Samples
    rate: int


def _u16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 2], "little")


def _u32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 4], "little")


def decode(data: bytes) -> Pcm:
    """A WAV body as mono samples.

    The page builds the body itself (`toWav`), so in practice this sees 16-bit
    PCM mono at the capture device's rate. It does not assume that: a WAV that
    arrives from anywhere else still has to decode or fail by name, because
    the alternative is a gate that silently reads garbage and calls it
    silence."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise NotAudio("no RIFF/WAVE header")
    fmt: tuple[int, int, int, int] | None = None
    i = 12
    while i + 8 <= len(data):
        cid, size, body = data[i : i + 4], _u32(data, i + 4), i + 8
        # A streaming writer puts 0 or 0xFFFFFFFF where it did not know the
        # size: the data runs to the end of the file.
        if cid == b"data" and (size == 0 or body + size > len(data)):
            end = len(data)
        else:
            end = min(body + size, len(data))
        if cid == b"fmt " and end - body >= 16:
            code, channels, rate, bits = (
                _u16(data, body),
                _u16(data, body + 2),
                _u32(data, body + 4),
                _u16(data, body + 14),
            )
            if code == 0xFFFE and end - body >= 26:
                # WAVE_FORMAT_EXTENSIBLE: the real format is the sub-format
                # GUID's first two bytes.
                code = _u16(data, body + 24)
            fmt = (code, channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise NotAudio("data before fmt")
            return _samples(data[body:end], *fmt)
        i = body + size + (size & 1)
    raise NotAudio("no data chunk")


def _samples(raw: bytes, code: int, channels: int, rate: int, bits: int) -> Pcm:
    if channels == 0 or rate == 0:
        raise NotAudio("zero channels or rate")
    if code == 3 and bits == 32:
        x = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<f4").astype(np.float32)
    elif code == 1 and bits in (8, 16, 24, 32):
        width = bits // 8
        n = len(raw) // width
        b = np.frombuffer(raw[: n * width], dtype=np.uint8)
        if bits == 8:
            # 8-bit WAV is unsigned.
            ints = b.astype(np.int32) - 128
        elif bits == 16:
            ints = b.view("<i2").astype(np.int32)
        elif bits == 32:
            ints = b.view("<i4").astype(np.int64)
        else:
            t = b.reshape(-1, 3).astype(np.int32)
            ints = t[:, 0] | (t[:, 1] << 8) | (t[:, 2] << 16)
            ints = np.where(ints & 0x800000, ints - (1 << 24), ints)
        x = (ints.astype(np.float64) / float(1 << (bits - 1))).astype(np.float32)
    else:
        raise NotAudio(f"format {code} at {bits} bits is not PCM or float")
    if x.size == 0:
        raise NotAudio("no samples")
    # First channel, not an average: the page captures mono, and averaging a
    # stereo clip halves the level a threshold was tuned against.
    return Pcm(x[::channels].copy() if channels > 1 else x, rate)


def resample(x: Samples, rate_in: float, rate_out: int = SAMPLE_RATE) -> Samples:
    """`x` at `rate_out`, by linear interpolation: the page's resampler, run
    on a whole clip at once.

    Linear on purpose. It aliases above about 8 kHz, which matters for audio
    a person will hear and not here: the detector weights those bands near
    zero. What it buys is that the page and the server convert identically,
    which is the property that lets them agree on a recording."""
    ratio = rate_in / rate_out if rate_in > 0 else 1.0
    if abs(ratio - 1.0) < 1e-9:
        return x.astype(np.float32, copy=False)
    n = len(x)
    if n < 2:
        return np.zeros(0, dtype=np.float32)
    # Every output position that has both its neighbours: the streaming
    # version stops at the last one and waits for the next push.
    pos = np.arange(math.ceil((n - 1) / ratio) + 1, dtype=np.float64) * ratio
    pos = pos[pos < n - 1]
    i = np.floor(pos).astype(np.int64)
    frac = (pos - i).astype(np.float32)
    a, b = x[i], x[i + 1]
    return (a + (b - a) * frac).astype(np.float32)


# ── the detector ──────────────────────────────────────────────────────────────

Detector = Callable[[Samples], float]


@lru_cache(maxsize=1)
def _session() -> Any:
    """The model, loaded once per process and shared: onnxruntime's `run` is
    safe to call from several threads, and each clip carries its own state."""
    try:
        import onnxruntime as ort

        model = MODEL_PATH.read_bytes()
    except (ImportError, OSError) as e:
        raise DetectorMissing(str(e)) from e
    got = hashlib.sha256(model).hexdigest()
    if got != MODEL_SHA256:
        raise DetectorMissing(f"{MODEL_PATH.name} has sha256 {got}, not the pinned model")
    opts = ort.SessionOptions()
    # Small model, short clips: one thread each keeps a question from taking
    # every core of the box.
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.log_severity_level = 3
    try:
        return ort.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
    except Exception as e:
        raise DetectorMissing(str(e)) from e


class Silero:
    """Silero v5 over one clip: a speech probability per 512-sample frame,
    carrying its recurrent state and context from frame to frame."""

    def __init__(self) -> None:
        self._session = _session()
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: Samples) -> float:
        x = np.concatenate([self._context, frame])[None, :]
        self._context = frame[-CONTEXT_SAMPLES:].copy()
        out, self._state = self._session.run(
            ["output", "stateN"], {"input": x, "state": self._state, "sr": self._sr}
        )
        return float(out[0][0])


# ── the offline question ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Analysis:
    """Field for field the shape a `/v1/audio/vad` endpoint commonly returns,
    so code written against one reads the same names here."""

    input_ms: int
    speech_ms: int
    segment_count: int
    # Where the page's gate would have ended the turn on this clip, if it
    # would have. None means it would still be listening at the end of the
    # recording, which is the NEVER SENDS failure.
    would_end_at_ms: int | None = None

    @property
    def is_silent(self) -> bool:
        """The gate the phase 2 spec decided: nothing here is worth an
        answer-model call."""
        return self.speech_ms == 0


def analyze(
    samples: Samples, rate: float, cfg: Config = DEFAULT, detector: Detector | None = None
) -> Analysis:
    """Did this clip hold speech worth spending an answer on?

    Runs the gate the page runs, to the end of the clip rather than stopping
    at the first end of turn, so one call reports both what the server needs
    and what the page would have done."""
    at16 = resample(samples, rate)
    input_ms = len(at16) * 1000 // SAMPLE_RATE
    detect = detector or Silero()
    gate = Gate(cfg)
    segments, in_segment = 0, False
    would_end: int | None = None
    for k in range(len(at16) // FRAME_SIZE):
        raw = detect(at16[k * FRAME_SIZE : (k + 1) * FRAME_SIZE])
        before = gate.speech_ms
        ev = gate.step(raw)
        if would_end is None and ev == "ended":
            # Drive the gate past the end of turn so the whole clip is
            # measured, recording where the page would have stopped.
            would_end = gate.ended_at_ms
            gate.ended_at_frame = None
        grew = gate.speech_ms > before
        if grew and not in_segment:
            segments += 1
        in_segment = grew
    return Analysis(input_ms, min(gate.speech_ms, input_ms), segments, would_end)


def analyze_wav(data: bytes, cfg: Config = DEFAULT) -> Analysis:
    """Decode and gate in one call, which is all the server wants. CPU work:
    call it off the event loop."""
    if not data:
        raise NotAudio("no samples")
    pcm = decode(data)
    return analyze(pcm.samples, pcm.rate, cfg)
