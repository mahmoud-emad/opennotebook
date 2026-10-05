"""Which service reads lines aloud, and which voices it offers.

`OPENNOTEBOOK_TTS_PROVIDER`, set by whoever runs the studio:

* `edge` (the default): Microsoft's neural voices through the free Edge
  Read Aloud service. No key and no server to run.
* `azure`: the same voices through Azure Speech, with a key: the paid,
  supported way for production.
* `openai`: any OpenAI-compatible speech server, as before: Speaches or
  Kokoro-FastAPI with Kokoro's voices, or OpenAI itself.

Speech to text is the OpenAI-compatible client's whichever provider reads
aloud: a spoken question goes to the audio answer model anyway.

A voice id belongs to one provider. One that does not belong to the provider
in force (a Kokoro id kept from before the switch, or a Microsoft one after
switching back) is never an error: it is read by that provider's own voice of
the same gender, so no build fails over a stored voice.
"""

import logging
import os
from collections.abc import Mapping
from typing import Literal

from opennotebook import speech
from opennotebook.speech import voices_microsoft as ms

log = logging.getLogger(__name__)

PROVIDER_KEY = "OPENNOTEBOOK_TTS_PROVIDER"

Provider = Literal["edge", "azure", "openai"]
PROVIDERS: tuple[Provider, ...] = ("edge", "azure", "openai")
DEFAULT: Provider = "edge"

# The voices offered on an OpenAI-compatible server, by their Kokoro names,
# which Speaches and Kokoro-FastAPI both accept. A speech server that does not
# know an id may quietly fall back to a default voice, which is why only these
# are offered.
KOKORO_VOICES: tuple[tuple[str, str], ...] = (
    ("af_bella", "Bella (US, female)"),
    ("af_nicole", "Nicole (US, female)"),
    ("af_sarah", "Sarah (US, female)"),
    ("af_sky", "Sky (US, female)"),
    ("am_adam", "Adam (US, male)"),
    ("am_michael", "Michael (US, male)"),
    ("bf_emma", "Emma (UK, female)"),
    ("bf_isabella", "Isabella (UK, female)"),
    ("bm_george", "George (UK, male)"),
    ("bm_lewis", "Lewis (UK, male)"),
)
KOKORO_HOST_DEFAULT = "af_bella"
KOKORO_SECOND_DEFAULT = "am_adam"


def provider(env: Mapping[str, str] | None = None) -> Provider:
    """The provider in force. An unknown name is logged and read as the
    default, so a typo costs the operator's choice and not every voice."""
    v = (os.environ if env is None else env).get(PROVIDER_KEY, "").strip().lower()
    for p in PROVIDERS:
        if v == p:
            return p
    if v:
        log.warning(
            "%s is “%s”, which is not edge, azure or openai; using %s. Set it to one of those.",
            PROVIDER_KEY,
            v,
            DEFAULT,
        )
    return DEFAULT


def is_microsoft(p: Provider) -> bool:
    return p in ("edge", "azure")


def voices(p: Provider) -> tuple[tuple[str, str], ...]:
    """The (id, label) pairs Settings › Voices offers under `p`."""
    if is_microsoft(p):
        return tuple((v.id, v.label) for v in ms.VOICES)
    return KOKORO_VOICES


def default_voice(p: Provider, speaker: Literal[1, 2]) -> str:
    """The host's (1) or the second voice's (2) default under `p`."""
    if is_microsoft(p):
        return ms.HOST_DEFAULT if speaker == 1 else ms.SECOND_DEFAULT
    return KOKORO_HOST_DEFAULT if speaker == 1 else KOKORO_SECOND_DEFAULT


def belongs(p: Provider, voice: str) -> bool:
    """True when `p` can read in `voice` as it is. A Microsoft provider takes
    any Microsoft short name; an OpenAI-compatible server takes anything
    else, so an operator's own voice names there are left alone."""
    return ms.is_microsoft(voice) == is_microsoft(p)


def gender(voice: str) -> ms.Gender:
    """A voice's gender when its id says it: Kokoro's `af_` / `am_`, or the
    Microsoft catalogue. Female otherwise, like the host default."""
    if (v := ms.BY_ID.get(voice)) is not None:
        return v.gender
    return "male" if len(voice) > 2 and voice[1] == "m" and voice[2] == "_" else "female"


def resolve(p: Provider, voice: str, speaker: Literal[1, 2] | None = None) -> str:
    """The voice `p` reads in for a requested `voice`: itself when it belongs,
    else `p`'s default for that speaker, or for the voice's gender when the
    speaker is not known."""
    v = voice.strip()
    if v and belongs(p, v):
        return v
    if speaker is None:
        speaker = 2 if gender(v) == "male" else 1
    return default_voice(p, speaker)


class OpenAICompatible(speech.Speech):
    """The OpenAI-compatible client, reading a voice it does not know (a
    Microsoft one, from an output voiced before a switch) in its own default
    voice of the same gender rather than refusing it."""

    @classmethod
    def of(cls, base: speech.Speech) -> OpenAICompatible:
        out = cls.__new__(cls)
        out.__dict__.update(vars(base))
        return out

    async def synthesize(self, text: str, voice: str) -> bytes:
        return await super().synthesize(text, resolve("openai", voice))


def chosen(
    base: speech.Speech, env: Mapping[str, str] | None = None, *, shared: bool = False
) -> speech.Speech:
    """The client that reads aloud under the provider in force. `base` is the
    OpenAI-compatible client the environment describes: it reads aloud under
    `openai`, and it transcribes under every provider. `shared`: Azure's
    lines go through the one client kept for them (`microsoft.azure_http`)."""
    p = provider(env)
    if is_microsoft(p):
        from opennotebook.speech import microsoft

        azure = microsoft.azure_http() if shared and p == "azure" else None
        return microsoft.MicrosoftSpeech.of(base, p, env, azure_http=azure)
    return OpenAICompatible.of(base)
