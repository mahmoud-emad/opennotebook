"""Why an output did not get built. A port of
`opennotebook_build/src/error.rs`, with the sentence each failure reaches a
person as.

Every failure names the call or the slide it came from, because a build that
fails halfway is diagnosed from the worker's log and this string; the
sentence is what the row's `failure` says.
"""


class BuildError(Exception):
    sentence: str = "Something went wrong while making this. Your sources are kept, so try again."


class Abandoned(BuildError):
    """Nobody wants this output any more: what it was being made for was
    deleted while it was being made. Not a failure of the build, and the
    caller treats it as a clean stop, because a `failed` row written now
    would bring the deleted thing back to every list that reads rows."""

    def __init__(self, sid: object) -> None:
        super().__init__(f"output {sid} was abandoned while it was being prepared")


class EmptyAudio(BuildError):
    """A line with no audio, or audio that lasts no time: it would play as
    nothing and reach the player as a slide that advances instantly."""

    sentence = (
        "The voice server sent back a line with no sound in it. Try again; if it keeps "
        "happening, restart the speech server."
    )

    def __init__(self, line: str) -> None:
        super().__init__(f"line `{line}`: synthesis returned no audio")
        self.line = line


class NotWav(BuildError):
    sentence = (
        "The voice server sent back audio the studio cannot play. Check that it serves 16-bit "
        "WAV, then try again."
    )

    def __init__(self, name: str, why: str) -> None:
        super().__init__(f"`{name}`: not a WAV: {why}")


class UnknownSpeaker(BuildError):
    """A line naming a speaker the output does not declare has no voice to
    be spoken in."""

    sentence = "The script named a speaker this output does not have. Try again."

    def __init__(self, line: str, speaker: str) -> None:
        super().__init__(
            f"line `{line}` names speaker `{speaker}`, which the output does not declare"
        )


class SlideMissing(BuildError):
    sentence = "A slide could not be saved, so the deck is incomplete. Try again."

    def __init__(self, slide: str, path: str) -> None:
        super().__init__(f"slide `{slide}` was not written: {path} is missing or incomplete")


class Voice(BuildError):
    """A speech call that failed, with the speech client's own sentence."""

    def __init__(self, detail: str, sentence: str) -> None:
        super().__init__(f"speech call failed: {detail}")
        self.sentence = sentence


class FilesNotWritten(BuildError):
    sentence = (
        "The studio could not save the files for this output. Check that the disk has space, "
        "then try again."
    )
