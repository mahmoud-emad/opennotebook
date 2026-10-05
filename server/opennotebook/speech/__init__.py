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
"""

import json
import os
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
    ) -> None:
        self.tts_url = tts_url.strip().rstrip("/")
        self.stt_url = stt_url.strip().rstrip("/")
        self.tts_model = tts_model
        self.stt_model = stt_model
        self.api_key = api_key if api_key and api_key.strip() else None
        self._http = http

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
        try:
            return normalize(r.content, SAMPLE_RATE)
        except EmptyAudio as e:
            raise empty() from e
        except NotWav as e:
            raise not_wav(str(e)) from e

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
    )


def speech() -> Speech:
    """The studio's speech client: Microsoft's voices or the OpenAI-compatible
    server, as `OPENNOTEBOOK_TTS_PROVIDER` says (`speech/provider.py`), and
    the OpenAI-compatible server for speech to text. A function, so tests can
    replace it."""
    from opennotebook.speech.provider import chosen

    return chosen(from_env())
