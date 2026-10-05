"""The lines the narrator says around a question, without spending a model
call. A port of `opennotebook_server/src/banter.rs`.

Every question used to cost TWO calls to the audio model: one to answer it,
and one before that to generate "I see your hand, go ahead" fresh at
temperature 1.1. The second one buys variety and nothing else, and variety is
the one thing a written list gives away for nothing.

So the courtesy lines are written down here, spoken by the output's own
voice, and cached on the files volume. The first time a voice says a line it
costs local synthesis; every time after that it is a file read.

Why a rotation and not a random pick: random repeats. With 24 lines and a
random draw, the same line comes up twice inside six questions about half the
time, and a listener notices a repeat far more than they notice a list. The
counter walks the bank in order from a per-output offset, so nothing repeats
until the bank is exhausted.
"""

import contextlib
import logging
from enum import StrEnum

from opennotebook import speech, storage

log = logging.getLogger(__name__)

# What the narrator says when the hand goes up, before the listener speaks.
# Under ten words, no greeting, no name, no question mark that expects an
# answer other than the listener's own, and nothing that assumes what the
# question is about.
INVITE = (
    "Go ahead, I'd love to hear your question.",
    "Yes, go ahead.",
    "Please, ask away.",
    "Sure, what would you like to know?",
    "Of course, go ahead.",
    "I see a hand. Go ahead.",
    "Happy to take that. Go ahead.",
    "Let's hear it.",
    "Go on, I'm listening.",
    "Please, go ahead.",
    "Yes? What's on your mind?",
    "Good, let's pause there. Go ahead.",
    "Absolutely, ask away.",
    "I'm listening.",
    "Go ahead, take your time.",
    "Sure thing, what is it?",
    "Right, let's hear your question.",
    "Yes, please go ahead.",
    "Fire away.",
    "Of course. What would you like to ask?",
    "Let's take that now. Go ahead.",
    "Please do, I'm listening.",
    "Good moment to stop. Go ahead.",
    "Yes, what would you like to know?",
)

# What the narrator says once the question has been sent, while the answer
# is still being worked out. It acknowledges the person rather than the
# machine.
THANKS = (
    "Thanks, let me think about that.",
    "Good question, one moment.",
    "Thank you, let me take that.",
    "Right, let me answer that.",
    "Good one. Let me explain.",
    "Thanks for asking, here goes.",
    "That's worth answering properly.",
    "Let me give you a proper answer.",
    "Thanks, here's how I'd put it.",
    "Good, let me address that.",
    "Nice question. One second.",
    "Thank you, I'll take that now.",
    "Let me think that through.",
    "Good point, let me answer.",
    "Thanks, that's a fair question.",
    "Let me come back on that.",
    "Right, here's the answer.",
    "Thanks, let me unpack that.",
    "Good, I can speak to that.",
    "Let me take that one.",
)

# What the narrator says while the answer is still being written and voiced.
# An answer used to be spoken clause by clause as the model wrote it, and
# every time the writing or the synthesis fell behind the speaking the
# listener heard the narrator stop mid-thought. Now the whole answer is
# prepared first and these fill the wait.
HOLD = (
    "Let me look into that.",
    "One moment, I'm putting it together.",
    "Let me check what we covered.",
    "Bear with me a second.",
    "Let me put that simply.",
    "Almost there.",
    "Let me find the right way to say this.",
    "Just a moment.",
    "Let me pull that together for you.",
    "Nearly ready.",
)

# What the narrator says when the listener cut in only to say "keep going".
RESUME = (
    "Sure, let's keep going.",
    "Okay, carrying on.",
    "Right, back to it.",
    "Sure thing, picking up again.",
    "Okay, on we go.",
    "Got it, let's keep going.",
)


class Bank(StrEnum):
    INVITE = "invite"
    THANKS = "thanks"
    HOLD = "hold"
    RESUME = "resume"

    @property
    def lines(self) -> tuple[str, ...]:
        return {
            Bank.INVITE: INVITE,
            Bank.THANKS: THANKS,
            Bank.HOLD: HOLD,
            Bank.RESUME: RESUME,
        }[self]


# Per output, per bank: how many lines have been used. In memory, as the
# playhead was: losing it on restart costs one possible repeat, which is not
# worth a write.
_counters: dict[tuple[str, Bank], int] = {}

_MASK = (1 << 64) - 1


def next_line(sid: str, bank: Bank) -> str:
    """The next line for this output, walking the bank in order.

    The starting offset is derived from the id, so two outputs playing at
    once do not open with the same line, and one never repeats until it has
    been all the way round."""
    lines = bank.lines
    seed = 0
    for b in sid.encode():
        seed = (seed * 31 + b) & _MASK
    n = _counters.get((sid, bank), 0)
    _counters[(sid, bank)] = n + 1
    return lines[((seed + n) & _MASK) % len(lines)]


def cache_path(voice: str, text: str) -> str:
    """Where a spoken line is cached: by voice and by the text's own hash,
    not by output, so every output after the first gets it for free."""
    h = 1469598103934665603
    for b in text.encode():
        h = ((h ^ b) * 1099511628211) & _MASK
    # The voice id is a Kokoro name like `af_bella`; anything else is dropped
    # so it is a safe path segment.
    safe = "".join(c for c in voice if (c.isascii() and c.isalnum()) or c == "_")
    return f"banter/{safe}/{h:016x}.wav"


async def spoken(voice: str, text: str) -> bytes | None:
    """The line's audio as a WAV, synthesised once and then read from the
    files volume. None when there is no speech server and no cached copy,
    which the caller turns into "say it on screen and play nothing": a
    courtesy that cannot be spoken is not a reason to refuse the question."""
    path = cache_path(voice, text)
    try:
        return storage.read(path)
    except OSError:
        pass
    try:
        wav = await speech.speech().synthesize(text, voice)
    except speech.SpeechError as e:
        log.info("banter: %s", e)
        return None
    # A write that fails costs a re-synthesis next time and nothing else.
    with contextlib.suppress(OSError):
        storage.put(path, wav)
    return wav
