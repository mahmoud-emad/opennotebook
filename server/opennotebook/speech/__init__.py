"""Speech: text to speech for narration and replies, speech to text for a
listener's question. A port of `opennotebook_speech`.

Both go to OpenAI-compatible audio endpoints (`POST /audio/speech`,
`POST /audio/transcriptions`), so any of these work by changing a URL:

* Speaches — Kokoro voices and Whisper in one local server; the default, at
  `http://localhost:8000/v1`.
* Kokoro-FastAPI — speech only.
* OpenAI — with its own voice names.

The voice ids OpenNotebook offers are Kokoro's (`af_bella`, `bm_george`, …),
which Speaches and Kokoro-FastAPI both accept as they are.

Whatever the server sends, `Speech.synthesize` returns a WAV at
`SAMPLE_RATE`, mono, 16-bit: narration joins clips with no re-encoding and the
player streams the samples straight into an `AudioContext`, so the format is a
contract, not a preference.

The settings are what whoever runs the studio sets, read from the
environment as the Rust server read them.

Word timings come from Kokoro-FastAPI's `POST /dev/captioned_speech`, which
returns the audio with each word's start and end, taken from the model's own
predicted durations. Speaches has no such route and its Kokoro model has no
duration output, so there a line is voiced without them and the video times
its words by estimate (`build/timeline.py`). Which servers answer the route
is learned once per server and remembered.
"""

import base64
import binascii
import functools
import json
import math
import os
from dataclasses import dataclass
from typing import Any

import httpx

from opennotebook.speech.wav import EmptyAudio, NotWav, normalize

# The sample rate every synthesised clip is delivered at.
SAMPLE_RATE = 24_000

TTS_BASE_URL_KEY = "OPENNOTEBOOK_TTS_BASE_URL"
STT_BASE_URL_KEY = "OPENNOTEBOOK_STT_BASE_URL"
TTS_MODEL_KEY = "OPENNOTEBOOK_TTS_MODEL"
STT_MODEL_KEY = "OPENNOTEBOOK_STT_MODEL"
API_KEY_KEY = "OPENNOTEBOOK_SPEECH_API_KEY"
# `auto` asks the speech server for word timings and falls back when it has
# none; `off` never asks.
TIMESTAMPS_KEY = "OPENNOTEBOOK_TTS_TIMESTAMPS"

BASE_URL_DEFAULT = "http://localhost:8000/v1"
TTS_MODEL_DEFAULT = "speaches-ai/Kokoro-82M-v1.0-ONNX"
STT_MODEL_DEFAULT = "Systran/faster-whisper-small"


class SpeechError(Exception):
    """Why the speech server did not deliver, with the sentence a person
    sees."""

    def __init__(
        self, detail: str, sentence: str, status: int | None = None, said: str = ""
    ) -> None:
        super().__init__(detail)
        self.sentence = sentence
        # For a refusal: the HTTP status and the server's own reason.
        self.status = status
        self.said = said


def unreachable(url: str, detail: str) -> SpeechError:
    return SpeechError(
        f"the speech server at {url} could not be reached: {detail}",
        f"The voice server at {url} is not answering. Start the speech server, or set "
        f"{TTS_BASE_URL_KEY} to where it runs, then try again.",
    )


def refused(status: int, body: bytes) -> SpeechError:
    """The server's own reason for refusing, when its body has one."""
    text = body.decode(errors="replace")
    detail = text[:300]
    try:
        v: Any = json.loads(text)
        err: Any = v.get("error") if isinstance(v, dict) else None
        for said in (
            err.get("message") if isinstance(err, dict) else None,
            v.get("detail") if isinstance(v, dict) else None,
            err if isinstance(err, str) else None,
        ):
            if isinstance(said, str):
                detail = said
                break
    except ValueError:
        pass
    return SpeechError(
        f"the speech server refused the request (HTTP {status}): {detail}",
        "The voice server refused to read a line aloud. Check that the voices chosen in "
        "Settings › Voices exist on it, then try again.",
        status,
        detail,
    )


def not_wav(why: str) -> SpeechError:
    return SpeechError(
        f"the speech server returned audio that is not a usable WAV: {why}",
        "The voice server sent back audio the studio cannot play. Check that it serves 16-bit "
        "WAV, then try again.",
    )


def empty() -> SpeechError:
    return SpeechError(
        "the speech server returned no audio",
        "The voice server sent back no audio. Try again; if it keeps happening, restart it.",
    )


@dataclass(frozen=True)
class Word:
    """One spoken word and when it is said, in ms from the clip's start."""

    word: str
    start_ms: int
    end_ms: int

    def as_cue(self) -> dict[str, Any]:
        return {"word": self.word, "start_ms": self.start_ms, "end_ms": self.end_ms}


# Speech servers that answered the captioned route with something other than
# timed audio, by base URL: asked once per process, not once per line.
_UNTIMED: set[str] = set()


def captioned_url(tts_url: str) -> str:
    """Kokoro-FastAPI serves the OpenAI routes under `/v1` and its own under
    the root, so the captioned route is beside `/v1`, not inside it."""
    root = tts_url[: -len("/v1")] if tts_url.endswith("/v1") else tts_url
    return f"{root}/dev/captioned_speech"


def _ms(v: Any) -> int | None:
    """Seconds as ms, or None for anything that is not a finite number: a
    JSON reader accepts `NaN` and `Infinity`, and a bool is an int to
    Python."""
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return round(float(v) * 1000)


def words_of(raw: Any) -> list[Word]:
    """The timed words of a captioned reply. Kokoro-FastAPI's documentation
    names the fields `start_time` and `end_time` while one of its examples
    says `start` and `end`, so both are read. A word without a time (one
    outside Kokoro's lexicon comes back that way) is dropped here and
    estimated later between its neighbours."""
    out: list[Word] = []
    for t in raw if isinstance(raw, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(t, dict):
            continue
        w: Any = t.get("word", t.get("text"))  # pyright: ignore[reportUnknownMemberType]
        start = _ms(t.get("start_time", t.get("start")))  # pyright: ignore[reportUnknownMemberType]
        end = _ms(t.get("end_time", t.get("end")))  # pyright: ignore[reportUnknownMemberType]
        # Kokoro times punctuation as tokens of its own; only words are kept.
        has_letter = isinstance(w, str) and any(c.isalnum() for c in w)
        if has_letter and start is not None and end is not None:
            out.append(Word(str(w).strip(), start, max(end, start)))
    return out


class Speech:
    """A speech client."""

    def __init__(
        self,
        tts_url: str,
        stt_url: str,
        tts_model: str,
        stt_model: str,
        api_key: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timestamps: bool = True,
    ) -> None:
        self.tts_url = tts_url.strip().rstrip("/")
        self.stt_url = stt_url.strip().rstrip("/")
        self.tts_model = tts_model
        self.stt_model = stt_model
        self.api_key = api_key if api_key and api_key.strip() else None
        self._http = http
        self.timestamps = timestamps

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def _post(self, url: str, base: str, **kw: Any) -> httpx.Response:
        http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
        try:
            return await http.post(url, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise unreachable(base, str(e) or type(e).__name__) from e
        finally:
            if self._http is None:
                await http.aclose()

    async def synthesize(self, text: str, voice: str) -> bytes:
        """`text` spoken in `voice`, as a 24 kHz mono 16-bit WAV."""
        r = await self._post(
            f"{self.tts_url}/audio/speech",
            self.tts_url,
            json={
                "model": self.tts_model,
                "input": text,
                "voice": voice,
                "response_format": "wav",
            },
        )
        if not r.is_success:
            raise refused(r.status_code, r.content)
        if not r.content:
            raise empty()
        return _clip(r.content)

    async def synthesize_timed(self, text: str, voice: str) -> tuple[bytes, list[Word]]:
        """`text` spoken in `voice`, with each word's timing when the server
        gives them, and none when it does not. The audio is the same contract
        as `synthesize`; resampling to `SAMPLE_RATE` keeps every time."""
        if not self.timestamps or self.tts_url in _UNTIMED:
            return await self.synthesize(text, voice), []
        r = await self._post(
            captioned_url(self.tts_url),
            self.tts_url,
            json={
                "model": self.tts_model,
                "input": text,
                "voice": voice,
                "response_format": "wav",
                "stream": False,
            },
        )
        timed = _timed(r) if r.is_success else None
        if timed is None:
            # No such route, or a reply that is not timed audio: not a
            # captioning server, remembered. Any other refusal (an unknown
            # voice, say) is this line's, and the plain call says it properly.
            if r.is_success or r.status_code in (404, 405, 501):
                _UNTIMED.add(self.tts_url)
            return await self.synthesize(text, voice), []
        audio, words = timed
        try:
            return _clip(audio), words
        except SpeechError:
            # Timed, but not audio we can use: not a captioning server we
            # can read, so the plain route from now on.
            _UNTIMED.add(self.tts_url)
            return await self.synthesize(text, voice), []

    async def transcribe(self, wav: bytes) -> str:
        """The words in a WAV recording."""
        r = await self._post(
            f"{self.stt_url}/audio/transcriptions",
            self.stt_url,
            files={"file": ("question.wav", wav, "audio/wav")},
            data={"model": self.stt_model, "response_format": "json"},
        )
        if not r.is_success:
            raise refused(r.status_code, r.content)
        try:
            v: Any = r.json()
        except ValueError as e:
            raise _undecoded(str(e)) from e
        said = v.get("text") if isinstance(v, dict) else None
        if not isinstance(said, str):
            raise _undecoded("no `text` in the response")
        return said.strip()


def _clip(data: bytes) -> bytes:
    """A server's audio in the studio's one clip format, or the error that
    says why it is not usable."""
    if not data:
        raise empty()
    try:
        return normalize(data, SAMPLE_RATE)
    except EmptyAudio as e:
        raise empty() from e
    except NotWav as e:
        raise not_wav(str(e)) from e


def _timed(r: httpx.Response) -> tuple[bytes, list[Word]] | None:
    """The audio and words of a captioned reply, or None when it is not one:
    a JSON object with base64 audio and a list of timestamps."""
    try:
        v: Any = r.json()
    except ValueError:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("audio"), str):
        return None
    try:
        audio = base64.b64decode(v["audio"], validate=True)
    except binascii.Error, ValueError:
        return None
    if "timestamps" not in v:
        return None
    return audio, words_of(v["timestamps"])


def _undecoded(why: str) -> SpeechError:
    return SpeechError(
        f"the transcription could not be read: {why}",
        "The question could not be understood. Try asking again.",
    )


def _env(key: str, default: str) -> str:
    return os.environ.get(key, "").strip() or default


def from_env(http: httpx.AsyncClient | None = None) -> Speech:
    """The client the environment describes. Speech-to-text defaults to the
    same server as speech, so one Speaches instance serves both."""
    tts = _env(TTS_BASE_URL_KEY, BASE_URL_DEFAULT)
    return Speech(
        tts,
        _env(STT_BASE_URL_KEY, tts),
        _env(TTS_MODEL_KEY, TTS_MODEL_DEFAULT),
        _env(STT_MODEL_KEY, STT_MODEL_DEFAULT),
        _env(API_KEY_KEY, "") or None,
        http=http,
        timestamps=_env(TIMESTAMPS_KEY, "auto").lower() not in ("off", "false", "0", "no"),
    )


@functools.cache
def shared_http() -> httpx.AsyncClient:
    """The one HTTP client every line read aloud or transcribed by the
    OpenAI-compatible server goes through, made on first use: its
    connections are kept between lines rather than opened for each. Closed
    at shutdown (`close_shared`)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))


async def close_shared() -> None:
    """Close the shared speech clients that were made: this server's and
    Azure's."""
    from opennotebook.speech import microsoft

    for made in (shared_http, microsoft.azure_http):
        if made.cache_info().currsize:
            await made().aclose()
            made.cache_clear()


def speech() -> Speech:
    """The studio's speech client: Microsoft's voices or the OpenAI-compatible
    server, as `OPENNOTEBOOK_TTS_PROVIDER` says (`speech/provider.py`), and
    the OpenAI-compatible server for speech to text, each through its shared
    HTTP client. A function, so tests can replace it."""
    from opennotebook.speech.provider import chosen

    return chosen(from_env(shared_http()), shared=True)
