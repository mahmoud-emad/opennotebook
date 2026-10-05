"""Speech, WAVs, narration and the courtesy lines. Ported from
`opennotebook_speech` (its stand-in server tests answer through httpx's
MockTransport here), `opennotebook_build/src/{wav,narrate}.rs` and
`opennotebook_server/src/banter.rs`. Plan §9: answers are 24 kHz PCM16; the
episode gaps are 220 / 320 / 650 ms."""

import json
from pathlib import Path

import httpx
import pytest

from opennotebook import speech, storage
from opennotebook.build import narrate, wav
from opennotebook.build.errors import NotWav, UnknownSpeaker, Voice
from opennotebook.domain.sessions import Line, Part, Speaker
from opennotebook.speech import banter
from opennotebook.speech.wav import EmptyAudio, normalize, parse, ramp_wav
from opennotebook.speech.wav import NotWav as SpeechNotWav
from tests.builds.fake import install

# ── the WAV contract ─────────────────────────────────────────────────────────


def test_the_target_format_passes_through_unchanged() -> None:
    w = ramp_wav(24_000, 1, 2400)
    assert normalize(w, 24_000) == w


def test_stereo_is_averaged_and_the_rate_converted() -> None:
    out = parse(normalize(ramp_wav(48_000, 2, 4800), 24_000))  # 0.1 s
    assert (out.rate, out.channels) == (24_000, 1)
    assert len(out.samples) == 2400, "same duration at half the rate"


def test_a_streaming_header_with_no_size_is_read_to_the_end() -> None:
    w = bytearray(ramp_wav(24_000, 1, 100))
    at = len(w) - 200 - 4
    w[at : at + 4] = (0xFFFFFFFF).to_bytes(4, "little")
    assert len(parse(normalize(bytes(w), 24_000)).samples) == 100


def test_mp3_and_float_wavs_are_refused_by_name() -> None:
    with pytest.raises(SpeechNotWav):
        normalize(b"ID3\x03\x00 mp3 bytes", 24_000)
    f = bytearray(ramp_wav(24_000, 1, 10))
    f[20:22] = (3).to_bytes(2, "little")  # IEEE float
    f[34:36] = (32).to_bytes(2, "little")
    with pytest.raises(SpeechNotWav):
        normalize(bytes(f), 24_000)
    with pytest.raises(EmptyAudio):
        normalize(ramp_wav(24_000, 1, 0), 24_000)


def mono(rate: int, samples: int, junk_before_data: bool = False) -> bytes:
    """A minimal 16-bit mono WAV, optionally with a junk chunk before `data`."""
    w = ramp_wav(rate, 1, samples)
    if not junk_before_data:
        return w
    return w[:36] + b"LIST" + (5).to_bytes(4, "little") + bytes([1, 2, 3, 4, 5, 0]) + w[36:]


def test_one_second_of_mono_24k_reads_as_1000ms() -> None:
    assert wav.duration_of(mono(24_000, 24_000), "x") == 1000


def test_a_chunk_before_data_does_not_shift_the_measurement() -> None:
    # The reason chunks are walked instead of assumed at byte 44.
    assert wav.duration_of(mono(24_000, 12_000), "x") == 500
    assert wav.duration_of(mono(24_000, 12_000, True), "x") == 500


def test_a_truncated_file_measures_what_is_actually_there() -> None:
    w = mono(24_000, 24_000)
    assert wav.duration_of(w[: len(w) - 24_000], "x") == 500


def test_non_wav_bytes_are_refused_rather_than_measured() -> None:
    with pytest.raises(NotWav):
        wav.duration_of(b"ID3\x04not audio at all", "clip.mp3")


def test_clips_join_into_one_wav_with_silence_between() -> None:
    # 24 kHz mono: 0.5 s, then 1 s, with a 250 ms gap; the first gap is
    # ignored.
    joined = wav.join([("a", mono(24_000, 12_000, True), 900), ("b", mono(24_000, 24_000), 250)])
    assert wav.duration_of(joined, "joined") == 1_750
    with pytest.raises(NotWav):
        wav.join([])
    with pytest.raises(NotWav):
        wav.join([("a", mono(24_000, 10), 0), ("o", mono(16_000, 100), 0)])


def test_the_episode_pauses_are_a_turn_a_breath_and_a_chapter() -> None:
    # The first line's gap is computed like any other and ignored by `join`.
    assert (wav.GAP_TURN_MS, wav.GAP_SAME_MS, wav.GAP_CHAPTER_MS) == (220, 320, 650)
    parts = [
        Part(
            "a",
            0,
            "A",
            [],
            [Line("s0l0", "host", 0, "x", "p0"), Line("s0l1", "expert", 1, "x", "p1")],
        ),
        Part(
            "b",
            1,
            "B",
            [],
            [Line("s1l0", "expert", 0, "x", "p2"), Line("s1l1", "expert", 1, "x", "p3")],
        ),
    ]
    assert [gap for _, _, gap in wav.episode_plan(parts)] == [220, 220, 650, 320]


# ── the speech client ────────────────────────────────────────────────────────


async def test_synthesis_asks_for_wav_and_delivers_24k_mono() -> None:
    seen: list[httpx.Request] = []

    def server(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        # A 48 kHz stereo server: normalised on the way out.
        return httpx.Response(200, content=ramp_wav(48_000, 2, 4800))

    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    s = speech.Speech(
        "http://tts.test/v1/", "http://tts.test/v1", "kokoro", "whisper", "k", http=http
    )
    out = await s.synthesize("Hello there.", "af_bella")
    assert out[:4] == b"RIFF"
    assert int.from_bytes(out[24:28], "little") == 24_000
    assert int.from_bytes(out[22:24], "little") == 1, "mono"
    assert str(seen[0].url) == "http://tts.test/v1/audio/speech"
    assert json.loads(seen[0].content) == {
        "model": "kokoro",
        "input": "Hello there.",
        "voice": "af_bella",
        "response_format": "wav",
    }
    assert seen[0].headers["authorization"] == "Bearer k"


async def test_a_server_error_names_the_reason() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(404, json={"detail": "Voice 'zz_nobody' not found"})
        )
    )
    s = speech.Speech("http://t/v1", "http://t/v1", "m", "m", http=http)
    with pytest.raises(speech.SpeechError) as e:
        await s.synthesize("Hi.", "zz_nobody")
    assert (e.value.status, e.value.said) == (404, "Voice 'zz_nobody' not found")
    assert "Settings › Voices" in e.value.sentence


async def test_transcription_sends_the_wav_as_a_file() -> None:
    seen: list[httpx.Request] = []

    def server(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"text": "  what is a page table?  "})

    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    s = speech.Speech("http://t/v1", "http://stt/v1", "tts", "whisper-small", http=http)
    w = ramp_wav(16_000, 1, 1600)
    assert await s.transcribe(w) == "what is a page table?"
    body = seen[0].content
    assert str(seen[0].url) == "http://stt/v1/audio/transcriptions"
    assert b'name="file"; filename="question.wav"' in body and w in body
    assert b'name="model"' in body and b"whisper-small" in body


async def test_an_unreachable_server_says_where_it_looked() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    s = speech.Speech("http://127.0.0.1:9/v1", "http://127.0.0.1:9/v1", "m", "m", http=http)
    with pytest.raises(speech.SpeechError) as e:
        await s.synthesize("Hi.", "af_bella")
    assert "127.0.0.1:9" in str(e.value)
    assert "127.0.0.1:9" in e.value.sentence and e.value.sentence.endswith(".")


def test_the_environment_names_the_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(speech.TTS_BASE_URL_KEY, raising=False)
    monkeypatch.delenv(speech.STT_BASE_URL_KEY, raising=False)
    s = speech.from_env()
    assert (s.tts_url, s.stt_url) == ("http://localhost:8000/v1", "http://localhost:8000/v1")
    assert s.tts_model == speech.TTS_MODEL_DEFAULT
    monkeypatch.setenv(speech.TTS_BASE_URL_KEY, "http://tts:9/v1/")
    s = speech.from_env()
    # Speech-to-text defaults to the same server, so one serves both.
    assert (s.tts_url, s.stt_url) == ("http://tts:9/v1", "http://tts:9/v1")


# ── narration ────────────────────────────────────────────────────────────────


def parts() -> list[Part]:
    return [
        Part(
            "a",
            0,
            "A",
            [],
            [
                Line("s0l0", "host", 0, "Polyps build reefs — slowly."),
                Line("s0l1", "expert", 1, "Over centuries."),
            ],
        ),
        Part("b", 1, "B", [], [Line("s1l0", "host", 0, "And they bleach in heat.")]),
    ]


SPEAKERS = [Speaker("host", "af_bella", "Bella"), Speaker("expert", "am_adam", "Adam")]


async def test_every_line_is_voiced_and_measured_from_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, voice = install(monkeypatch, tmp_path)
    ps = parts()
    await narrate.synthesise_all(speech.speech(), "sid1", SPEAKERS, ps)
    for p in ps:
        for line in p.lines:
            path = line.audio_path
            assert path is not None
            assert path == f"audio/sid1/{line.line_id}.wav"
            assert line.duration_ms == wav.duration_of(storage.read(path), "x") > 0
    # Spoken, not written: the dash is a stop the voice takes; the stored
    # text keeps its dash.
    said = {b["input"] for b in voice.said}
    assert "Polyps build reefs. Slowly." in said
    assert ps[0].lines[0].text == "Polyps build reefs — slowly."
    assert {b["voice"] for b in voice.said} == {"af_bella", "am_adam"}
    episode = narrate.episode(ps)
    total = sum(line.duration_ms or 0 for p in ps for line in p.lines)
    assert wav.duration_of(episode, "episode") == total + 220 + 650


async def test_a_line_whose_speaker_has_no_voice_fails_before_any_synthesis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, voice = install(monkeypatch, tmp_path)
    with pytest.raises(UnknownSpeaker):
        await narrate.synthesise_all(speech.speech(), "sid1", SPEAKERS[:1], parts())
    assert voice.said == []


async def test_a_refused_line_fails_with_the_speech_servers_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, voice = install(monkeypatch, tmp_path)
    voice.status = 404
    with pytest.raises(Voice) as e:
        await narrate.synthesise_all(speech.speech(), "sid1", SPEAKERS, parts())
    assert "Settings › Voices" in e.value.sentence


# ── banter ───────────────────────────────────────────────────────────────────


def test_both_banks_are_big_enough_to_not_repeat() -> None:
    # The floor the owner set is twenty.
    assert len(banter.INVITE) >= 20 and len(banter.THANKS) >= 20
    assert len(banter.INVITE) <= 50 and len(banter.THANKS) <= 50


def test_no_line_appears_twice_in_a_bank() -> None:
    for bank in (banter.Bank.INVITE, banter.Bank.THANKS):
        assert len(set(bank.lines)) == len(bank.lines), bank


def test_a_session_walks_the_whole_bank_before_repeating() -> None:
    n = len(banter.INVITE)
    seen = [banter.next_line("walk-the-bank", banter.Bank.INVITE) for _ in range(n)]
    assert len(set(seen)) == n, "a line repeated inside one pass of the bank"


def test_two_sessions_do_not_open_identically() -> None:
    a = banter.next_line("session-alpha", banter.Bank.THANKS)
    b = banter.next_line("session-beta", banter.Bank.THANKS)
    assert a != b, "two sessions opened with the same line"


def test_every_line_is_speakable() -> None:
    for bank in (banter.Bank.INVITE, banter.Bank.THANKS):
        for line in bank.lines:
            assert line and "\n" not in line
            assert "<" not in line and '"' not in line, line
            assert len(line.split()) <= 10, line


async def test_a_line_is_synthesised_once_and_then_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, voice = install(monkeypatch, tmp_path)
    first = await banter.spoken("af_bella", "Fire away.")
    again = await banter.spoken("af_bella", "Fire away.")
    assert first is not None and first == again
    assert len(voice.said) == 1
    assert banter.cache_path("af_bella", "x") != banter.cache_path("af_bella", "y")
    assert banter.cache_path("../af", "x").startswith("banter/af/")
    # No speech server and no copy: nothing to play, and no failure.
    voice.status = 500
    assert await banter.spoken("am_adam", "Fire away.") is None
