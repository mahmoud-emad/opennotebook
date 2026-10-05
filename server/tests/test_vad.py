"""The server's silence gate. Ported from `opennotebook_vad` (`lib.rs`,
`wav.rs`, `resample.rs`), with the detector now Silero v5 at the page's
32 ms frames."""

import hashlib
import math
import struct

import numpy as np
import pytest

from opennotebook.speech import vad
from opennotebook.speech.vad import DEFAULT, Gate, Samples


def wav16(samples: Samples, rate: int, channels: int = 1) -> bytes:
    """A 16-bit PCM WAV, as the page's `toWav` builds one."""
    ints = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    if channels > 1:
        ints = np.repeat(ints, channels)
    data = ints.tobytes()
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


VOWELS = ((730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240), (530, 1840, 2480))


def voiced(seconds: float, rate: int) -> Samples:
    """Something Silero hears as a person talking: a voice at 120 Hz with a
    vowel's formants, a new vowel every 220 ms, each rising and falling like
    a syllable. No recording ships with the tests; this is enough to show the
    gate lets speech through, not only that it stops silence."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    phase = np.cumsum(2 * np.pi * (120 + 15 * np.sin(2 * np.pi * 0.7 * t)) / rate)
    syllable = int(0.22 * rate)
    out = np.zeros(n)
    for i, a in enumerate(range(0, n, syllable)):
        b = min(a + syllable, n)
        seg = np.zeros(b - a)
        for k in range(1, 30):
            gain = sum(1 / (1 + ((k * 120.0 - f) / 100) ** 2) for f in VOWELS[i % len(VOWELS)])
            seg += gain / math.sqrt(k) * np.sin(k * phase[a:b])
        out[a:b] = seg * np.sin(np.pi * np.linspace(0, 1, b - a)) ** 0.5
    return (out / np.abs(out).max() * 0.5).astype(np.float32)


def silence(seconds: float, rate: int) -> Samples:
    return np.zeros(int(seconds * rate), dtype=np.float32)


# ── the gate, at frame level ──────────────────────────────────────────────────


def test_the_thresholds_are_the_pages() -> None:
    """The page's `DEFAULT_GATE` (playerGate.ts) and these are one set: two
    sets drift, and then the page and the server disagree on a recording."""
    assert (
        DEFAULT.smoothing,
        DEFAULT.attack,
        DEFAULT.decay,
        DEFAULT.onset_frames,
        DEFAULT.hang_ms,
        DEFAULT.min_speech_ms,
    ) == (0.4, 0.6, 0.5, 2, 2000, 300)
    assert (vad.SAMPLE_RATE, vad.FRAME_SIZE, vad.FRAME_MS) == (16_000, 512, 32)


def test_a_noisy_room_still_ends_the_turn() -> None:
    """Reported from studio1: the listener asked a question, stopped talking,
    and the turn never sent, because background noise kept it listening.
    After real speech the track holds the smoothed probability in the band
    between `decay` and `attack`, which is what an ordinary room does.

    The bug it guards: the band used to set `quiet_ms = 0`, so a single frame
    above `decay` restarted the whole two second countdown and the turn could
    never end. Only a frame at or above `attack` may reset it."""
    g = Gate()
    for _ in range(32):
        g.step(0.95)
    assert g.heard_speech, "the speech at the start was not detected"
    ended_at = None
    for i in range(500):
        # Sustained, not a spike: smoothing damps a lone frame.
        if g.step(0.50 if i % 2 == 0 else 0.58) == "ended":
            ended_at = g.ended_at_ms
            break
    assert ended_at is not None, (
        "the turn never ended: background noise held it open, which is the studio1 report"
    )
    assert ended_at >= DEFAULT.hang_ms, f"ended at {ended_at} ms, before hang_ms"


def test_a_speaker_who_keeps_talking_is_not_cut_off() -> None:
    """A probability that keeps reaching `attack` is someone still talking,
    and the countdown restarts."""
    g = Gate()
    for i in range(1250):
        assert g.step(0.30 if i % 5 == 0 else 0.92) != "ended", (
            f"cut the speaker off at frame {i} while they were still talking"
        )


def test_a_cough_and_quiet_does_not_end_a_turn() -> None:
    """The `min_speech_ms` floor: a cough plus two seconds of quiet does not
    send an empty clip."""
    g = Gate()
    for _ in range(4):
        g.step(0.99)
    assert g.heard_speech
    assert all(g.step(0.0) != "ended" for _ in range(200))
    assert g.speech_ms < DEFAULT.min_speech_ms


# ── decoding the upload ───────────────────────────────────────────────────────


def test_silence_decodes_and_gates_as_silent() -> None:
    a = vad.analyze_wav(wav16(silence(2, 24_000), 24_000))
    assert a.is_silent, f"silence was not gated as silent: {a}"
    assert a.input_ms >= 1_900, f"input_ms looks wrong: {a.input_ms}"
    assert a.segment_count == 0
    assert a.would_end_at_ms is None, "a turn ended on silence"


def test_garbage_fails_by_name() -> None:
    """Malformed audio is the one case phase 2 §8 says is loud. Keep it
    loud, and in a sentence."""
    with pytest.raises(vad.NotAudio) as e:
        vad.analyze_wav(b"not a wav at all")
    assert "WAV parse failed" in str(e.value)
    assert e.value.sentence == (
        "The recording could not be read as audio. Check the microphone, then ask again."
    )


def test_an_empty_body_fails_by_name() -> None:
    with pytest.raises(vad.NotAudio):
        vad.analyze_wav(b"")
    header_only = wav16(np.zeros(0, dtype=np.float32), 16_000)
    with pytest.raises(vad.NotAudio):
        vad.analyze_wav(header_only)


def test_stereo_reads_the_first_channel_and_float_reads_as_float() -> None:
    x = np.linspace(-0.5, 0.5, 100, dtype=np.float32)
    pcm = vad.decode(wav16(x, 16_000, channels=2))
    assert pcm.rate == 16_000 and len(pcm.samples) == 100
    assert np.allclose(pcm.samples, x, atol=1e-4)
    data = x.astype("<f4").tobytes()
    float_wav = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 3, 1, 8_000, 32_000, 4, 32)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    pcm = vad.decode(float_wav)
    assert pcm.rate == 8_000 and np.array_equal(pcm.samples, x)


# ── resampling ────────────────────────────────────────────────────────────────


def _streaming(x: list[float], rate_in: float, chunk: int) -> list[float]:
    """The page's and the Rust crate's streaming resampler, as written there,
    fed in buffers of `chunk`."""
    ratio = rate_in / vad.SAMPLE_RATE
    pos, last, primed = 0.0, 0.0, False
    out: list[float] = []
    for at in range(0, len(x), chunk):
        buf = x[at : at + chunk]
        if not primed:
            last, primed = buf[0], True
        while pos < len(buf):
            i = math.floor(pos)
            frac = pos - i
            a = last if i < 0 else buf[i]
            if i + 1 >= len(buf):
                break
            b = buf[i + 1]
            out.append(a + (b - a) * frac)
            pos += ratio
        last = buf[-1]
        pos -= len(buf)
    return out


def test_resampling_matches_the_pages_streaming_resampler() -> None:
    """The property that lets page and server agree on a recording: the
    whole-clip conversion here is the page's buffer-by-buffer one. (A buffer
    boundary changes a few samples by interpolating across it; the one-shot
    form has none, so the comparison is against the one-buffer stream.)"""
    x = [math.sin(i / 7) for i in range(3000)]
    for rate in (48_000.0, 44_100.0, 24_000.0):
        want = _streaming(x, rate, len(x))
        got = vad.resample(np.array(x, dtype=np.float32), rate)
        assert abs(len(got) - len(want)) <= 1, rate
        n = min(len(got), len(want))
        assert np.allclose(got[:n], want[:n], atol=1e-5), rate


def test_resampling_keeps_length_and_rate() -> None:
    x = np.sin(np.arange(4800) / 100).astype(np.float32)
    assert abs(len(vad.resample(x, 48_000)) - 1600) <= 1
    assert np.array_equal(vad.resample(x, 16_000), x), "same rate is a copy"


# ── the detector ──────────────────────────────────────────────────────────────


def test_the_bundled_model_is_the_pinned_silero_v5() -> None:
    """vad-web's `silero_vad_v5.onnx` and silero-vad's v5.1.2 model are the
    same bytes, and the server runs those bytes, so page and server run one
    detector."""
    assert hashlib.sha256(vad.MODEL_PATH.read_bytes()).hexdigest() == vad.MODEL_SHA256
    assert (vad.MODEL_PATH.parent / "LICENSE").read_text().startswith("MIT License")


def test_silence_holds_no_speech() -> None:
    """Silence is silence. The property the server gate rests on."""
    a = vad.analyze(silence(3, 16_000), 16_000)
    assert a.speech_ms == 0 and a.is_silent
    assert a.would_end_at_ms is None


def test_a_click_is_not_speech() -> None:
    """A single transient is the failure the loudness gate has: one 8 ms
    full-scale burst, far above its 0.06."""
    clip = silence(2, 16_000)
    clip[8_000:8_128] = 0.9
    a = vad.analyze(clip, 16_000)
    assert a.would_end_at_ms is None, "a click ended a turn"
    assert a.speech_ms < DEFAULT.min_speech_ms, f"a click counted as {a.speech_ms} ms"


def test_steady_noise_is_not_speech() -> None:
    rng = np.random.default_rng(7)
    a = vad.analyze((rng.standard_normal(48_000) * 0.05).astype(np.float32), 16_000)
    assert a.is_silent, a


def test_a_voice_is_speech_and_ends_the_turn_after_it() -> None:
    """Speech gets through the gate, at the page's 48 kHz, and the turn ends
    once the hang has passed after it."""
    rate = 48_000
    clip = np.concatenate([silence(0.5, rate), voiced(2.0, rate), silence(2.5, rate)])
    a = vad.analyze_wav(wav16(clip, rate))
    assert not a.is_silent
    assert 1_200 <= a.speech_ms <= 2_400, a
    assert a.segment_count >= 1
    assert a.would_end_at_ms is not None
    assert a.would_end_at_ms >= 2_500 + DEFAULT.hang_ms - 200, a
