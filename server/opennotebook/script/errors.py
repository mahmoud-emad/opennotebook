"""Why a piece of writing did not come back usable, and how a route answers
it. A port of `opennotebook_script/src/error.rs`, the kinds in use so far.

A failed model call stays an `AiError`, which already knows its sentence;
these are the failures of a call that did answer.
"""

from opennotebook.ai.errors import AiError, Kind
from opennotebook.errors import Problem


class ScriptError(Exception):
    @property
    def sentence(self) -> str:
        raise NotImplementedError


class Truncated(ScriptError):
    """The model stopped because it ran out of room, so the text is a
    fragment that reads like a whole answer. Caught by reading
    `finish_reason` rather than by inspecting the text, which cannot show
    this."""

    def __init__(self, stage: str, finish_reason: str, text: str) -> None:
        super().__init__(
            f"the model truncated its answer for {stage} (finish_reason `{finish_reason}`)"
        )
        self.stage = stage
        self.finish_reason = finish_reason
        # What the model managed to say before the cut, so a caller that can
        # use a partial answer does not have to ask again for the same reply.
        self.text = text

    @property
    def sentence(self) -> str:
        return (
            f"The AI model ran out of room before it finished the {self.stage}. "
            "Try again with fewer sources, or pick another model in Settings › Models."
        )


class Empty(ScriptError):
    """The model answered with nothing that could be used."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"the model returned no usable {stage}")
        self.stage = stage

    @property
    def sentence(self) -> str:
        return (
            f"The AI model gave back no usable {self.stage}. Try again; if it keeps "
            "happening, pick another model in Settings › Models."
        )


# The status a failed model call is answered with. Out of credit and a busy
# provider are the person's to wait out or fix; the rest are the studio's
# upstream failing it.
_STATUS = {
    Kind.QUOTA: 402,
    Kind.RATE_LIMITED: 429,
    Kind.UNAVAILABLE: 503,
}


def problem(e: AiError | ScriptError) -> Problem:
    """A failed piece of writing as the route's answer."""
    if isinstance(e, AiError):
        return Problem(_STATUS.get(e.kind, 502), e.sentence)
    return Problem(502, e.sentence)


def too_slow(seconds: int, what: str) -> Problem:
    return Problem(
        504,
        f"The AI model took longer than {seconds} seconds to {what}. "
        "Try again, or choose fewer sources.",
    )
