"""Microsoft's neural voices, two ways (`speech/provider.py` picks one):

* `edge`: the free Read Aloud service Microsoft Edge uses, through the
  `edge-tts` package (LGPLv3, used unmodified as a dependency). It answers
  in MP3, which is decoded here with `miniaudio` (MIT, no ffmpeg), and it
  says when each word starts and ends, which is kept as the line's word
  timings.
* `azure`: Azure Speech's REST endpoint, with a key, asked for 24 kHz mono
  16-bit WAV directly. No SDK. It has no word timings over REST.

Both deliver the studio's one clip format (24 kHz mono 16-bit WAV, see
`speech/__init__.py`), so narration, the courtesy lines and spoken answers
use them without knowing which is in force. Speech to text stays on the
OpenAI-compatible client, which this one is built from.

Both are services on the internet, so a failure that may pass (no answer, a
dropped connection, a busy server) is tried again after a short wait, and
only a few lines are read at once across the whole process.
"""

import asyncio
import os
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import aiohttp
import edge_tts
import edge_tts.exceptions
import httpx
import miniaudio

from opennotebook import speech
from opennotebook.speech import provider as providers
from opennotebook.speech import voices_microsoft as ms
from opennotebook.speech.wav import EmptyAudio, NotWav, encode, normalize

AZURE_KEY_KEY = "OPENNOTEBOOK_AZURE_SPEECH_KEY"
AZURE_REGION_KEY = "OPENNOTEBOOK_AZURE_SPEECH_REGION"
# `off` drops Edge's word timings; anything else keeps them.
TIMESTAMPS_KEY = "OPENNOTEBOOK_TTS_TIMESTAMPS"

# Lines read at once across the process, whichever outputs and answers ask:
# a build already reads four at a time, and the service is shared.
PARALLEL = 4
# The waits before each retry of a failure that may pass, in seconds.
RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0)
# The longest wait an Azure `Retry-After` is honoured for.
MAX_RETRY_AFTER = 5.0
# The longest one line may take to arrive from Edge, every chunk of it. A
# line is seconds of audio and comes in well under this.
EDGE_LINE_TIMEOUT_S = 60

# Edge reports times in ticks of 100 ns.
_TICKS_PER_MS = 10_000

# One gate per event loop: a semaphore belongs to the loop that first waits
# on it, and tests run more than one.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if (g := _gates.get(loop)) is None:
        g = _gates[loop] = asyncio.Semaphore(PARALLEL)
    return g


# ── what a person reads ──────────────────────────────────────────────────────


class _Again(Exception):
    """A failure that may pass on another try, with the error to raise when
    the tries run out."""

    def __init__(self, error: speech.SpeechError, wait: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        # How long the service asked to be left alone, when it said.
        self.wait = wait


def edge_unreachable(detail: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Microsoft's Edge voice service did not answer: {detail}",
        "Microsoft's voice service did not answer. Try again in a minute, or set "
        f"{providers.PROVIDER_KEY} to azure or openai.",
    )


def edge_silent(voice: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Microsoft's Edge voice service sent no audio for voice {voice}",
        f"Microsoft's voice service sent back no audio for the voice {voice}. Pick another "
        "voice in Settings › Voices, or try again in a minute.",
    )


def undecodable(why: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Microsoft's voice service sent audio that could not be decoded: {why}",
        "Microsoft's voice service sent back audio the studio cannot read. Try again; if it "
        f"keeps happening, set {providers.PROVIDER_KEY} to azure or openai.",
    )


def no_audio() -> speech.SpeechError:
    return speech.SpeechError(
        "Microsoft's voice service sent no audio",
        "Microsoft's voice service sent back no audio. Try again in a minute.",
    )


def azure_unconfigured(missing: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Azure Speech is chosen but {missing} is not set",
        f"Azure voices need a key and a region. Set {AZURE_KEY_KEY} and {AZURE_REGION_KEY}, "
        f"or set {providers.PROVIDER_KEY} to edge for the free voices.",
    )


def azure_key_refused(status: int, region: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Azure Speech refused the key (HTTP {status}) in region {region}",
        f"Azure Speech refused the key. Check {AZURE_KEY_KEY}, and that {AZURE_REGION_KEY} is "
        "the region the key was made in, then try again.",
        status,
    )


def azure_refused(status: int, said: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Azure Speech refused the request (HTTP {status}): {said}",
        "Azure Speech refused to read a line aloud. Check that the voices chosen in "
        "Settings › Voices exist on it, then try again.",
        status,
        said,
    )


def azure_busy(status: int) -> speech.SpeechError:
    return speech.SpeechError(
        f"Azure Speech is busy or down (HTTP {status})",
        "Azure Speech is busy or down. Try again in a minute.",
        status,
    )


def azure_unreachable(region: str, detail: str) -> speech.SpeechError:
    return speech.SpeechError(
        f"Azure Speech in region {region} could not be reached: {detail}",
        f"Azure Speech in the region “{region}” did not answer. Check {AZURE_REGION_KEY} "
        "and the network, then try again.",
    )


# ── audio and timings ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WordTiming:
    """One spoken word and when it is said, in ms from the clip's start."""

    word: str
    start_ms: int
    end_ms: int

    def as_cue(self) -> dict[str, Any]:
        return {"word": self.word, "start_ms": self.start_ms, "end_ms": self.end_ms}


def timings_on(env: Mapping[str, str] | None = None) -> bool:
    v = (os.environ if env is None else env).get(TIMESTAMPS_KEY, "").strip().lower()
    return v not in ("off", "false", "0", "no")


def mp3_to_wav(mp3: bytes) -> bytes:
    """MP3 (or any format miniaudio reads) as the studio's clip: 24 kHz mono
    16-bit WAV, resampled and mixed down by the decoder when it is not."""
    if not mp3:
        raise no_audio()
    try:
        pcm = miniaudio.decode(
            mp3,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=speech.SAMPLE_RATE,
        )
    except miniaudio.MiniaudioError as e:
        raise undecodable(str(e) or type(e).__name__) from e
    if not pcm.samples:
        raise no_audio()
    return encode(speech.SAMPLE_RATE, pcm.samples.tolist())


def word_of(chunk: Mapping[str, Any]) -> WordTiming | None:
    """One of Edge's word boundaries as a timed word, or None when it is not
    one, or is punctuation, or has no usable time."""
    if chunk.get("type") != "WordBoundary":
        return None
    text, offset, duration = chunk.get("text"), chunk.get("offset"), chunk.get("duration")
    if not isinstance(text, str) or not any(c.isalnum() for c in text):
        return None
    if isinstance(offset, bool) or not isinstance(offset, int | float) or offset < 0:
        return None
    if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
        duration = 0
    start = round(offset / _TICKS_PER_MS)
    return WordTiming(text.strip(), start, start + round(duration / _TICKS_PER_MS))


# ── the transports ───────────────────────────────────────────────────────────

# What Edge streams for one line: audio chunks and word boundaries, as
# `edge_tts.Communicate.stream` yields them.
EdgeStream = Callable[[str, str], AsyncIterator[Mapping[str, Any]]]


async def edge_stream(text: str, voice: str) -> AsyncIterator[Mapping[str, Any]]:
    """The real Edge service. Tests replace it; none reaches the internet."""
    async for chunk in edge_tts.Communicate(text, voice, boundary="WordBoundary").stream():
        yield chunk


def new_http() -> httpx.AsyncClient:
    """A client for Azure. Tests replace it; none reaches the internet."""
    return httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))


def azure_ssml(text: str, voice: str) -> str:
    lang = ms.locale(voice)
    return (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xml:lang={quoteattr(lang)}><voice name={quoteattr(voice)}>{escape(text)}</voice>"
        "</speak>"
    )


def azure_url(region: str) -> str:
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"


def _seconds(v: str | None) -> float | None:
    try:
        return min(max(float(v), 0.0), MAX_RETRY_AFTER) if v else None
    except ValueError:
        return None


class MicrosoftSpeech(speech.Speech):
    """Reads aloud in Microsoft's voices; transcribes as the OpenAI-compatible
    client it was made from."""

    provider: providers.Provider
    azure_key: str
    azure_region: str
    retry_delays: tuple[float, ...]
    _stream: EdgeStream | None

    @classmethod
    def of(
        cls,
        base: speech.Speech,
        p: providers.Provider = "edge",
        env: Mapping[str, str] | None = None,
        *,
        stream: EdgeStream | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> MicrosoftSpeech:
        e = os.environ if env is None else env
        out = cls.__new__(cls)
        out.__dict__.update(vars(base))
        out.provider = p
        out.azure_key = e.get(AZURE_KEY_KEY, "").strip()
        out.azure_region = e.get(AZURE_REGION_KEY, "").strip().lower()
        out.retry_delays = retry_delays
        out._stream = stream
        return out

    async def synthesize(self, text: str, voice: str) -> bytes:
        """`text` spoken in `voice`, as a 24 kHz mono 16-bit WAV. A voice that
        is not Microsoft's is read in its default voice of the same gender."""
        return (await self.synthesize_timed(text, voice))[0]

    # Its own method with its own timing type. Should the base client grow a
    # timed method of its own, this one answers for it by shape (`word`,
    # `start_ms`, `end_ms`, `as_cue`), which is all a caller reads.
    async def synthesize_timed(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, text: str, voice: str
    ) -> tuple[bytes, list[WordTiming]]:
        """`text` spoken in `voice`, with each word's start and end in ms from
        the clip's start: Edge times every word; Azure's REST endpoint does
        not, so its lines come back with none."""
        v = providers.resolve(self.provider, voice)
        if self.provider == "azure":
            return await self._tries(lambda: self._azure(text, v)), []
        wav, words = await self._tries(lambda: self._edge(text, v))
        # The operator turned timings off (`OPENNOTEBOOK_TTS_TIMESTAMPS`).
        return wav, (words if timings_on() else [])

    async def _tries[T](self, once: Callable[[], Awaitable[T]]) -> T:
        """`once`, tried again after each wait while it fails in a way that
        may pass. Only `PARALLEL` lines are with the service at a time."""
        for wait in (*self.retry_delays, None):
            try:
                async with _gate():
                    return await once()
            except _Again as e:
                if wait is None:
                    raise e.error from e
                await asyncio.sleep(e.wait if e.wait is not None else wait)
        raise AssertionError("unreachable")

    async def _edge(self, text: str, voice: str) -> tuple[bytes, list[WordTiming]]:
        stream = self._stream or edge_stream
        audio = bytearray()
        words: list[WordTiming] = []
        try:
            # A whole line, however slowly the service sends it: a stalled
            # socket is tried again rather than waited on for good.
            async with asyncio.timeout(EDGE_LINE_TIMEOUT_S):
                async for chunk in stream(text, voice):
                    if chunk.get("type") == "audio":
                        data = chunk.get("data")
                        if isinstance(data, bytes | bytearray):
                            audio += data
                    elif (w := word_of(chunk)) is not None:
                        words.append(w)
        except TimeoutError as e:
            raise _Again(edge_unreachable(f"no whole line within {EDGE_LINE_TIMEOUT_S} s")) from e
        except edge_tts.exceptions.NoAudioReceived as e:
            raise _Again(edge_silent(voice)) from e
        except (
            aiohttp.ClientError,
            edge_tts.exceptions.EdgeTTSException,
            OSError,
        ) as e:
            raise _Again(edge_unreachable(str(e) or type(e).__name__)) from e
        if not audio:
            raise _Again(edge_silent(voice))
        # Decoding is CPU work: off the event loop.
        return await asyncio.to_thread(mp3_to_wav, bytes(audio)), words

    async def _azure(self, text: str, voice: str) -> bytes:
        if not self.azure_key:
            raise azure_unconfigured(AZURE_KEY_KEY)
        if not self.azure_region:
            raise azure_unconfigured(AZURE_REGION_KEY)
        http = self._http or new_http()
        try:
            r = await http.post(
                azure_url(self.azure_region),
                headers={
                    "Ocp-Apim-Subscription-Key": self.azure_key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
                    "User-Agent": "OpenNotebook",
                },
                content=azure_ssml(text, voice).encode(),
            )
        except httpx.HTTPError as e:
            raise _Again(azure_unreachable(self.azure_region, str(e) or type(e).__name__)) from e
        finally:
            if self._http is None:
                await http.aclose()
        if r.status_code in (401, 403):
            raise azure_key_refused(r.status_code, self.azure_region)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Again(azure_busy(r.status_code), _seconds(r.headers.get("Retry-After")))
        if not r.is_success:
            said = r.text.strip()[:300] or r.reason_phrase
            raise azure_refused(r.status_code, said)
        try:
            return await asyncio.to_thread(normalize, r.content, speech.SAMPLE_RATE)
        except EmptyAudio as e:
            raise no_audio() from e
        except NotWav as e:
            raise undecodable(str(e)) from e
