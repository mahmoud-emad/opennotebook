"""The Microsoft neural voices the studio offers, for both ways of reaching
them (`speech/microsoft.py`): the free Edge Read Aloud service and Azure
Speech serve the same voices under the same names.

Mostly the *Multilingual* voices: they read whatever language the text is in
with that language's own pronunciation, so a French script sounds French
rather than English-accented. Every voice here was checked against Edge's
voice list on 2026-10-05.
"""

import re
from dataclasses import dataclass
from typing import Literal

Gender = Literal["female", "male"]


@dataclass(frozen=True)
class MicrosoftVoice:
    # The service's short name, which is what a request names.
    id: str
    label: str
    gender: Gender


VOICES: tuple[MicrosoftVoice, ...] = (
    MicrosoftVoice("en-US-AvaMultilingualNeural", "Ava (US, female, multilingual)", "female"),
    MicrosoftVoice("en-US-AndrewMultilingualNeural", "Andrew (US, male, multilingual)", "male"),
    MicrosoftVoice("en-US-EmmaMultilingualNeural", "Emma (US, female, multilingual)", "female"),
    MicrosoftVoice("en-US-BrianMultilingualNeural", "Brian (US, male, multilingual)", "male"),
    MicrosoftVoice(
        "en-AU-WilliamMultilingualNeural", "William (Australia, male, multilingual)", "male"
    ),
    MicrosoftVoice("en-GB-SoniaNeural", "Sonia (UK, female)", "female"),
    MicrosoftVoice("en-GB-RyanNeural", "Ryan (UK, male)", "male"),
    MicrosoftVoice("en-US-AriaNeural", "Aria (US, female)", "female"),
    MicrosoftVoice("en-US-GuyNeural", "Guy (US, male)", "male"),
    MicrosoftVoice(
        "fr-FR-VivienneMultilingualNeural", "Vivienne (French, female, multilingual)", "female"
    ),
    MicrosoftVoice("fr-FR-RemyMultilingualNeural", "Rémy (French, male, multilingual)", "male"),
    MicrosoftVoice(
        "de-DE-SeraphinaMultilingualNeural", "Seraphina (German, female, multilingual)", "female"
    ),
    MicrosoftVoice(
        "de-DE-FlorianMultilingualNeural", "Florian (German, male, multilingual)", "male"
    ),
)

# The pair that sounds most like the old Kokoro defaults (Bella and Adam):
# a female host and a male second voice.
HOST_DEFAULT = "en-US-AvaMultilingualNeural"
SECOND_DEFAULT = "en-US-AndrewMultilingualNeural"

BY_ID = {v.id: v for v in VOICES}

# A Microsoft voice's short name: a locale, then a name ending in `Neural`,
# such as `en-US-AvaMultilingualNeural` or `zh-CN-shaanxi-XiaoniNeural`. An
# operator may name any of them in the environment, not only those above.
_SHORT_NAME = re.compile(r"[a-z]{2,3}-[A-Z]{2}(?:-[A-Za-z]+)?-[A-Za-z]+Neural")


def is_microsoft(voice: str) -> bool:
    """True for a Microsoft voice's short name."""
    return _SHORT_NAME.fullmatch(voice.strip()) is not None


def locale(voice: str) -> str:
    """The locale a voice belongs to: `en-US` for `en-US-AvaMultilingualNeural`."""
    return "-".join(voice.split("-")[:2])
