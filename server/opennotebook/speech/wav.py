"""Bringing a server's WAV to the one format OpenNotebook plays: mono, 16-bit
PCM, at a fixed rate. A port of `opennotebook_speech/src/wav.rs`."""

import math
import struct
from dataclasses import dataclass


class NotWav(ValueError):
    """Audio that is not a usable WAV, and why."""


@dataclass
class Pcm:
    rate: int
    channels: int
    # Interleaved 16-bit samples.
    samples: list[int]


def _u16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 2], "little")


def _u32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 4], "little")


def parse(b: bytes) -> Pcm:
    if len(b) < 12 or b[0:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise NotWav("no RIFF/WAVE header (was another format asked for?)")
    fmt: tuple[int, int, int, int] | None = None
    i = 12
    while i + 8 <= len(b):
        cid = b[i : i + 4]
        size = _u32(b, i + 4)
        body = i + 8
        # A streaming server writes 0 or 0xFFFFFFFF for a data size it did not
        # know in advance: the data runs to the end of the file.
        if cid == b"data" and (size == 0 or body + size > len(b)):
            end = len(b)
        else:
            end = min(body + size, len(b))
        if cid == b"fmt " and end - body >= 16:
            fmt = (_u16(b, body), _u16(b, body + 2), _u32(b, body + 4), _u16(b, body + 14))
        elif cid == b"data":
            if fmt is None:
                raise NotWav("data before fmt")
            fmt_code, channels, rate, bits = fmt
            # 1 is PCM; 0xFFFE is WAVE_FORMAT_EXTENSIBLE, PCM in practice here.
            if fmt_code not in (1, 0xFFFE) or bits != 16:
                raise NotWav(f"format {fmt_code} at {bits} bits; only 16-bit PCM is supported")
            if channels == 0 or rate == 0:
                raise NotWav("zero channels or rate")
            data = b[body:end]
            n = len(data) // 2
            samples = list(struct.unpack(f"<{n}h", data[: n * 2]))
            return Pcm(rate, channels, samples)
        i = body + size + (size & 1)
    raise NotWav("no data chunk")


def encode(rate: int, samples: list[int]) -> bytes:
    """A canonical mono 16-bit PCM WAV."""
    data = struct.pack(f"<{len(samples)}h", *samples)
    head = (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + (rate * 2).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
    )
    return head + data


class EmptyAudio(ValueError):
    """A WAV with no samples in it."""


def normalize(data: bytes, rate: int) -> bytes:
    """`data` as a canonical mono 16-bit WAV at `rate`: channels averaged,
    then resampled linearly when the rate differs. Always re-encoded, so a
    header with a streaming placeholder size comes out with real sizes."""
    pcm = parse(data)
    ch = pcm.channels
    frames = len(pcm.samples) // ch
    # Integer division toward zero, as the Rust `i32 / i32` did.
    mono = [int(sum(pcm.samples[f * ch : f * ch + ch]) / ch) for f in range(frames)]
    if not mono:
        raise EmptyAudio
    if pcm.rate == rate:
        return encode(rate, mono)
    n = max(len(mono) * rate // pcm.rate, 1)
    step = pcm.rate / rate
    last = len(mono) - 1
    out: list[int] = []
    for i in range(n):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a, b = mono[min(j, last)], mono[min(j + 1, last)]
        x = a + (b - a) * frac
        # Half away from zero, as Rust's `round` does.
        out.append(int(math.copysign(math.floor(abs(x) + 0.5), x)))
    return encode(rate, out)


def ramp_wav(rate: int, channels: int, frames: int) -> bytes:
    """A small PCM WAV of a ramp, for tests and stand-in speech servers."""
    data = b"".join(
        ((i % 100) * 100).to_bytes(2, "little", signed=True) for i in range(frames * channels)
    )
    return (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + (rate * 2 * channels).to_bytes(4, "little")
        + (2 * channels).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )
