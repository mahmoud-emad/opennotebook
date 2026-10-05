"""Microsoft's voices: which provider reads aloud, the voices each offers and
the fallback between them, Edge's MP3 decoded to the studio's one WAV format,
Edge's word timings, and Azure's REST endpoint. Nothing here reaches the
internet: Edge's stream and Azure's HTTP are stand-ins."""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import aiohttp
import edge_tts.exceptions
import httpx
import pytest

from opennotebook import speech, storage
from opennotebook.build import wav as build_wav
from opennotebook.domain import sessions
from opennotebook.domain import settings as st
from opennotebook.speech import banter, microsoft
from opennotebook.speech import provider as providers
from opennotebook.speech import voices_microsoft as ms
from opennotebook.speech.wav import parse, ramp_wav

AVA, ANDREW = ms.HOST_DEFAULT, ms.SECOND_DEFAULT


@pytest.fixture(autouse=True)
def no_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        providers.PROVIDER_KEY,
        microsoft.AZURE_KEY_KEY,
        microsoft.AZURE_REGION_KEY,
        microsoft.TIMESTAMPS_KEY,
        *(d.key for d in st.CATALOGUE),
    ):
        monkeypatch.delenv(key, raising=False)


def use(monkeypatch: pytest.MonkeyPatch, p: str) -> None:
    monkeypatch.setenv(providers.PROVIDER_KEY, p)


# ── MP3 that decodes, built by hand ──────────────────────────────────────────


def mp3_frames(n: int, rate: int = 24_000) -> bytes:
    """`n` silent MP3 frames, mono. At 24 kHz they are MPEG-2 layer III at
    48 kbit/s, which is what Edge sends: 144 bytes and 576 samples each. At
    48 kHz, MPEG-1 at 128 kbit/s: 384 bytes and 1152 samples. Zero side
    information decodes to silence, which is all a format test needs."""
    if rate == 24_000:
        head, size = bytes([0xFF, 0xF3, 0x64, 0xC0]), 144
    else:
        head, size = bytes([0xFF, 0xFB, 0x94, 0xC0]), 384
    return (head + bytes(size - 4)) * n


def header(w: bytes) -> tuple[int, int, int, int, int, int]:
    """format, channels, rate, byte rate, block align, bits."""

    def i(a: int, b: int) -> int:
        return int.from_bytes(w[a:b], "little")

    return i(20, 22), i(22, 24), i(24, 28), i(28, 32), i(32, 34), i(34, 36)


def test_edges_mp3_becomes_the_exact_clip_format() -> None:
    w = microsoft.mp3_to_wav(mp3_frames(50))
    assert w[:4] == b"RIFF" and w[8:16] == b"WAVEfmt " and w[36:40] == b"data"
    assert header(w) == (1, 1, 24_000, 48_000, 2, 16)
    assert int.from_bytes(w[40:44], "little") == len(w) - 44
    # 50 frames of 576 samples at 24 kHz.
    assert len(parse(w).samples) == 28_800
    assert build_wav.duration_of(w, "x") == 1_200


def test_mp3_at_another_rate_is_resampled_to_24k() -> None:
    w = microsoft.mp3_to_wav(mp3_frames(50, 48_000))
    assert header(w) == (1, 1, 24_000, 48_000, 2, 16)
    assert build_wav.duration_of(w, "x") == 1_200


def test_audio_that_will_not_decode_says_so() -> None:
    with pytest.raises(speech.SpeechError) as e:
        microsoft.mp3_to_wav(b"not audio at all" * 20)
    assert e.value.sentence.startswith("Microsoft's voice service sent back audio")
    assert providers.PROVIDER_KEY in e.value.sentence
    with pytest.raises(speech.SpeechError):
        microsoft.mp3_to_wav(b"")


# ── word timings ─────────────────────────────────────────────────────────────


def boundary(text: Any, offset: Any, duration: Any = 2_000_000) -> dict[str, Any]:
    return {"type": "WordBoundary", "text": text, "offset": offset, "duration": duration}


def test_word_boundaries_are_read_in_ms() -> None:
    # Ticks of 100 ns: 1_000_000 is 100 ms.
    assert microsoft.word_of(boundary("Coral", 1_000_000, 3_500_000)) == microsoft.WordTiming(
        "Coral", 100, 450
    )
    # A boundary with no duration is a word of no length, not a lost word.
    assert microsoft.word_of(boundary("reefs", 5_000_000, None)) == microsoft.WordTiming(
        "reefs", 500, 500
    )


@pytest.mark.parametrize(
    "chunk",
    [
        boundary(",", 1_000_000),
        boundary("", 1_000_000),
        boundary(None, 1_000_000),
        boundary("word", None),
        boundary("word", True),
        boundary("word", -5),
        {"type": "SentenceBoundary", "text": "A sentence.", "offset": 0, "duration": 1},
        {"type": "audio", "data": b"x"},
    ],
)
def test_what_is_not_a_timed_word_is_dropped(chunk: dict[str, Any]) -> None:
    assert microsoft.word_of(chunk) is None


# ── Edge ─────────────────────────────────────────────────────────────────────


class Edge:
    """Edge's stream: the line's MP3 in a few chunks, with a boundary for
    each word. `failures` are raised, one per call, before it answers."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures = list(failures)
        self.calls: list[tuple[str, str]] = []
        self.inflight = 0
        self.most = 0

    async def stream(self, text: str, voice: str) -> AsyncIterator[Mapping[str, Any]]:
        self.calls.append((text, voice))
        self.inflight += 1
        self.most = max(self.most, self.inflight)
        try:
            await asyncio.sleep(0.01)
            if self.failures:
                raise self.failures.pop(0)
            audio = mp3_frames(25)
            yield {"type": "audio", "data": audio[:1000]}
            at = 0
            for word in text.split():
                yield boundary(word, at, 2_000_000)
                at += 2_500_000
            yield boundary(".", at)
            yield {"type": "audio", "data": audio[1000:]}
        finally:
            self.inflight -= 1


def edge(fake: Edge, p: providers.Provider = "edge") -> microsoft.MicrosoftSpeech:
    base = speech.Speech("http://stt.test/v1", "http://stt.test/v1", "kokoro", "whisper")
    return microsoft.MicrosoftSpeech.of(base, p, {}, stream=fake.stream, retry_delays=(0, 0))


async def test_edge_reads_a_line_with_its_word_timings() -> None:
    fake = Edge()
    audio, words = await edge(fake).synthesize_timed("Coral reefs grow", AVA)
    assert header(audio) == (1, 1, 24_000, 48_000, 2, 16)
    assert build_wav.duration_of(audio, "x") == 600
    assert words == [
        microsoft.WordTiming("Coral", 0, 200),
        microsoft.WordTiming("reefs", 250, 450),
        microsoft.WordTiming("grow", 500, 700),
    ]
    assert words[0].as_cue() == {"word": "Coral", "start_ms": 0, "end_ms": 200}
    assert fake.calls == [("Coral reefs grow", AVA)]
    # The plain call is the same audio.
    assert await edge(Edge()).synthesize("Coral reefs grow", AVA) == audio


async def test_a_kokoro_voice_is_read_by_the_microsoft_voice_of_its_gender() -> None:
    fake = Edge()
    s = edge(fake)
    await s.synthesize("Hi.", "af_bella")
    await s.synthesize("Hi.", "bm_george")
    await s.synthesize("Hi.", "")
    await s.synthesize("Hi.", "en-GB-RyanNeural")
    assert [v for _, v in fake.calls] == [AVA, ANDREW, AVA, "en-GB-RyanNeural"]


async def test_a_dropped_connection_is_tried_again() -> None:
    fake = Edge(aiohttp.ClientConnectionError("reset"), TimeoutError())
    audio, _ = await edge(fake).synthesize_timed("Hello there", AVA)
    assert len(fake.calls) == 3 and audio[:4] == b"RIFF"


async def test_an_edge_that_never_answers_says_what_to_do() -> None:
    fake = Edge(*(aiohttp.ClientConnectionError("blocked") for _ in range(3)))
    with pytest.raises(speech.SpeechError) as e:
        await edge(fake).synthesize("Hello", AVA)
    assert len(fake.calls) == 3
    assert e.value.sentence == (
        "Microsoft's voice service did not answer. Try again in a minute, or set "
        "OPENNOTEBOOK_TTS_PROVIDER to azure or openai."
    )
    assert "blocked" in str(e.value)


async def test_an_edge_that_sends_no_audio_names_the_voice() -> None:
    fake = Edge(*(edge_tts.exceptions.NoAudioReceived("none") for _ in range(3)))
    with pytest.raises(speech.SpeechError) as e:
        await edge(fake).synthesize("Hello", ANDREW)
    assert ANDREW in e.value.sentence and "Settings › Voices" in e.value.sentence


async def test_only_a_few_lines_are_with_the_service_at_once() -> None:
    fake = Edge()
    s = edge(fake)
    await asyncio.gather(*(s.synthesize(f"Line {i}", AVA) for i in range(12)))
    assert len(fake.calls) == 12
    assert fake.most == microsoft.PARALLEL


async def test_timings_off_keeps_the_audio_and_drops_the_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(microsoft.TIMESTAMPS_KEY, "off")
    s = edge(Edge())
    audio, words = await s.synthesize_timed("Coral reefs", AVA)
    assert words == [] and audio[:4] == b"RIFF"


# ── Azure ────────────────────────────────────────────────────────────────────


class Azure:
    """Azure's REST endpoint: answers in turn from `replies`, the last one
    repeated."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies) or [httpx.Response(200, content=ramp_wav(24_000, 1, 2400))]
        self.seen: list[httpx.Request] = []

    def answer(self, r: httpx.Request) -> httpx.Response:
        self.seen.append(r)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def azure(fake: Azure, key: str = "k3y", region: str = "WestEurope") -> microsoft.MicrosoftSpeech:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.answer))
    base = speech.Speech("http://stt.test/v1", "http://stt.test/v1", "kokoro", "whisper", http=http)
    env = {microsoft.AZURE_KEY_KEY: key, microsoft.AZURE_REGION_KEY: region}
    return microsoft.MicrosoftSpeech.of(base, "azure", env, retry_delays=(0, 0))


async def test_azure_is_asked_for_24k_mono_wav_with_ssml() -> None:
    fake = Azure()
    audio, words = await azure(fake).synthesize_timed(
        "Fish & <chips>", "fr-FR-RemyMultilingualNeural"
    )
    assert words == [] and header(audio) == (1, 1, 24_000, 48_000, 2, 16)
    (r,) = fake.seen
    assert str(r.url) == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert r.headers["Ocp-Apim-Subscription-Key"] == "k3y"
    assert r.headers["X-Microsoft-OutputFormat"] == "riff-24khz-16bit-mono-pcm"
    assert r.headers["Content-Type"] == "application/ssml+xml"
    body = r.content.decode()
    assert 'xml:lang="fr-FR"' in body and 'name="fr-FR-RemyMultilingualNeural"' in body
    assert "Fish &amp; &lt;chips&gt;" in body


async def test_azure_audio_at_another_rate_is_brought_to_the_contract() -> None:
    fake = Azure(httpx.Response(200, content=ramp_wav(48_000, 2, 4800)))
    audio = await azure(fake).synthesize("Hi.", AVA)
    assert header(audio) == (1, 1, 24_000, 48_000, 2, 16)
    assert build_wav.duration_of(audio, "x") == 100


async def test_azure_refusing_the_key_says_which_settings_to_check() -> None:
    fake = Azure(httpx.Response(401))
    with pytest.raises(speech.SpeechError) as e:
        await azure(fake).synthesize("Hi.", AVA)
    assert len(fake.seen) == 1, "a wrong key is not tried again"
    assert e.value.sentence == (
        "Azure Speech refused the key. Check OPENNOTEBOOK_AZURE_SPEECH_KEY, and that "
        "OPENNOTEBOOK_AZURE_SPEECH_REGION is the region the key was made in, then try again."
    )


async def test_a_busy_azure_is_tried_again_then_said_to_be_busy() -> None:
    ok = httpx.Response(200, content=ramp_wav(24_000, 1, 240))
    fake = Azure(httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(503), ok)
    assert (await azure(fake).synthesize("Hi.", AVA))[:4] == b"RIFF"
    assert len(fake.seen) == 3
    fake = Azure(httpx.Response(500))
    with pytest.raises(speech.SpeechError) as e:
        await azure(fake).synthesize("Hi.", AVA)
    assert len(fake.seen) == 3
    assert e.value.sentence == "Azure Speech is busy or down. Try again in a minute."


async def test_azure_refusing_a_line_points_at_the_voices() -> None:
    fake = Azure(httpx.Response(400, text="Unsupported voice"))
    with pytest.raises(speech.SpeechError) as e:
        await azure(fake).synthesize("Hi.", AVA)
    assert "Settings › Voices" in e.value.sentence and e.value.said == "Unsupported voice"


async def test_an_unreachable_azure_names_its_region() -> None:
    fake = Azure(httpx.ConnectError("no route"))
    with pytest.raises(speech.SpeechError) as e:
        await azure(fake).synthesize("Hi.", AVA)
    assert len(fake.seen) == 3
    assert "“westeurope”" in e.value.sentence and "OPENNOTEBOOK_AZURE_SPEECH_REGION" in (
        e.value.sentence
    )


async def test_azure_without_a_key_or_region_says_what_to_set() -> None:
    for key, region, missing in (
        ("", "eastus", microsoft.AZURE_KEY_KEY),
        ("k", "", microsoft.AZURE_REGION_KEY),
    ):
        fake = Azure()
        with pytest.raises(speech.SpeechError) as e:
            await azure(fake, key, region).synthesize("Hi.", AVA)
        assert fake.seen == [] and missing in str(e.value)
        assert e.value.sentence == (
            "Azure voices need a key and a region. Set OPENNOTEBOOK_AZURE_SPEECH_KEY and "
            "OPENNOTEBOOK_AZURE_SPEECH_REGION, or set OPENNOTEBOOK_TTS_PROVIDER to edge for the "
            "free voices."
        )


# ── which provider ───────────────────────────────────────────────────────────


def test_the_provider_is_edge_unless_the_operator_says(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert providers.provider() == "edge"
    for p in ("azure", "openai", "edge"):
        use(monkeypatch, f" {p.upper()} ")
        assert providers.provider() == p
    use(monkeypatch, "elevenlabs")
    with caplog.at_level(logging.WARNING):
        assert providers.provider() == "edge"
    assert "not edge, azure or openai" in caplog.text


@pytest.mark.parametrize("p", ["edge", "azure"])
def test_microsofts_voices_read_aloud_and_the_speech_server_transcribes(
    monkeypatch: pytest.MonkeyPatch, p: str
) -> None:
    use(monkeypatch, p)
    monkeypatch.setenv(speech.STT_BASE_URL_KEY, "http://stt:9/v1")
    s = speech.speech()
    assert isinstance(s, microsoft.MicrosoftSpeech) and s.provider == p
    assert s.stt_url == "http://stt:9/v1"


async def test_the_speech_server_transcribes_under_microsofts_voices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def stt(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"text": " Why coral? "})

    http = httpx.AsyncClient(transport=httpx.MockTransport(stt))
    base = speech.Speech("http://tts/v1", "http://stt/v1", "kokoro", "whisper", http=http)
    s = microsoft.MicrosoftSpeech.of(base, "edge", {})
    assert await s.transcribe(ramp_wav(24_000, 1, 100)) == "Why coral?"
    assert str(seen[0].url) == "http://stt/v1/audio/transcriptions"


async def test_the_openai_provider_reads_a_microsoft_voice_in_kokoros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, "openai")
    asked: list[str] = []

    def server(r: httpx.Request) -> httpx.Response:
        import json

        asked.append(json.loads(r.content)["voice"])
        return httpx.Response(200, content=ramp_wav(24_000, 1, 240))

    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    s = providers.chosen(speech.Speech("http://tts/v1", "http://tts/v1", "k", "w", http=http))
    assert isinstance(s, providers.OpenAICompatible)
    for v in (ANDREW, AVA, "bm_lewis", "alloy"):
        await s.synthesize("Hi.", v)
    assert asked == ["am_adam", "af_bella", "bm_lewis", "alloy"]


async def test_no_test_reaches_microsoft_without_a_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for p in ("edge", "azure"):
        use(monkeypatch, p)
        monkeypatch.setenv(microsoft.AZURE_KEY_KEY, "k")
        monkeypatch.setenv(microsoft.AZURE_REGION_KEY, "eastus")
        s = speech.speech()
        assert isinstance(s, microsoft.MicrosoftSpeech)
        s.retry_delays = (0, 0)
        with pytest.raises(speech.SpeechError):
            await s.synthesize("Hi.", AVA)


# ── the voices ───────────────────────────────────────────────────────────────


def test_the_catalogue_is_microsoft_voices_with_a_female_host_and_male_second() -> None:
    assert len({v.id for v in ms.VOICES}) == len(ms.VOICES) >= 10
    for v in ms.VOICES:
        assert ms.is_microsoft(v.id), v.id
        assert v.label.split(" ")[0].replace("é", "e") in v.id
        assert ("female" in v.label) == (v.gender == "female")
    assert ms.BY_ID[AVA].gender == "female" and ms.BY_ID[ANDREW].gender == "male"
    assert sum("Multilingual" in v.id for v in ms.VOICES) >= 6
    assert not any(ms.is_microsoft(v) for v, _ in providers.KOKORO_VOICES)


@pytest.mark.parametrize(
    ("p", "voice", "speaker", "read"),
    [
        ("edge", AVA, 2, AVA),
        ("edge", "en-GB-SoniaNeural", None, "en-GB-SoniaNeural"),
        # Any Microsoft short name an operator names is theirs to name.
        ("azure", "ja-JP-NanamiNeural", 1, "ja-JP-NanamiNeural"),
        ("edge", "af_bella", 2, ANDREW),
        ("edge", "af_bella", None, AVA),
        ("edge", "am_michael", None, ANDREW),
        ("edge", "", 1, AVA),
        ("openai", "af_sky", 1, "af_sky"),
        ("openai", ANDREW, 1, "af_bella"),
        ("openai", ANDREW, None, "am_adam"),
        ("openai", "", 2, "am_adam"),
    ],
)
def test_a_voice_that_is_not_the_providers_falls_back_to_its_default(
    p: providers.Provider, voice: str, speaker: Any, read: str
) -> None:
    assert providers.resolve(p, voice, speaker) == read


def test_settings_offer_the_voices_of_the_provider_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, lang = st.need(st.SPEAKER1_VOICE_KEY), st.need(st.LANGUAGE_KEY)
    assert host.default == AVA and st.need(st.SPEAKER2_VOICE_KEY).default == ANDREW
    assert [v for v, _ in host.kind.options] == [v.id for v in ms.VOICES]
    assert "natively" in lang.help and "English-accented" not in lang.help
    assert st.validate(host, "af_bella") is not None and st.validate(host, AVA) is None
    use(monkeypatch, "openai")
    host, lang = st.need(st.SPEAKER1_VOICE_KEY), st.need(st.LANGUAGE_KEY)
    assert host.default == "af_bella" and st.need(st.SPEAKER2_VOICE_KEY).default == "am_adam"
    assert host.kind.options == providers.KOKORO_VOICES and host.help == "Local English voice."
    assert "English-accented" in lang.help
    assert st.validate(host, "bm_lewis") is None and st.validate(host, AVA) is not None
    # The rest of the catalogue is the same under either.
    assert st.need(st.STYLE_KEY) is st.find(st.STYLE_KEY)


def test_a_stored_voice_of_another_provider_reads_as_the_speakers_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = {st.SPEAKER1_VOICE_KEY: "af_sky", st.SPEAKER2_VOICE_KEY: "af_nicole"}
    assert st.effective(st.SPEAKER1_VOICE_KEY, mine, {}) == AVA
    # The second voice's default, not the voice's own gender: the speaker
    # keeps the voice they were set up with.
    assert st.effective(st.SPEAKER2_VOICE_KEY, mine, {}) == ANDREW
    env = {st.SPEAKER2_VOICE_KEY: "en-GB-RyanNeural"}
    assert st.effective(st.SPEAKER2_VOICE_KEY, mine, {}, env) == "en-GB-RyanNeural"
    # The page shows the voice that is read in, not one it cannot offer.
    c = st.current(st.need(st.SPEAKER1_VOICE_KEY), mine, {st.SPEAKER1_VOICE_KEY: "bm_lewis"})
    assert (c.value, c.default, c.effective) == ("", AVA, AVA)
    use(monkeypatch, "openai")
    assert st.effective(st.SPEAKER1_VOICE_KEY, mine, {}) == "af_sky"
    theirs = {st.SPEAKER1_VOICE_KEY: "en-GB-SoniaNeural"}
    assert st.effective(st.SPEAKER1_VOICE_KEY, theirs, {}) == "af_bella"
    c = st.current(st.need(st.SPEAKER1_VOICE_KEY), theirs, {})
    assert (c.value, c.effective) == ("", "af_bella")


async def test_the_settings_page_lists_microsofts_voices(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, "edge")
    r = await client.get("/api/settings")
    host = next(x for x in r.json()["settings"] if x["key"] == st.SPEAKER1_VOICE_KEY)
    assert host["default"] == AVA
    assert {"value": AVA, "label": "Ava (US, female, multilingual)", "hint": ""} in host["options"]
    r = await client.patch(f"/api/settings/{st.SPEAKER1_VOICE_KEY}", json={"value": "af_bella"})
    assert r.status_code == 422 and "Pick one from the list" in r.json()["detail"]
    r = await client.patch(
        f"/api/settings/{st.SPEAKER1_VOICE_KEY}", json={"value": "en-GB-SoniaNeural"}
    )
    assert r.status_code == 200 and r.json()["value"] == "en-GB-SoniaNeural"


def test_a_microsoft_voice_gives_its_speaker_a_first_name() -> None:
    assert sessions.voice_name(AVA) == "Ava"
    assert sessions.voice_name("en-GB-RyanNeural") == "Ryan"
    assert sessions.voice_name("af_bella") == "Bella"
    assert sessions.display_name("Host", ANDREW) == "Andrew"


async def test_courtesy_lines_are_cached_under_the_voice_that_reads_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use(monkeypatch, "edge")
    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    fake = Edge()
    monkeypatch.setattr(speech, "speech", lambda: edge(fake))
    assert (await banter.spoken("af_bella", "Go ahead.")) is not None
    assert fake.calls == [("Go ahead.", AVA)]
    assert storage.exists(banter.cache_path(AVA, "Go ahead."))
    assert not storage.exists(banter.cache_path("af_bella", "Go ahead."))
