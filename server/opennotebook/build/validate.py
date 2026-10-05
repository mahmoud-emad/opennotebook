"""Render validation, which is a liveness check and only that. A port of
`opennotebook_build/src/validate.rs`.

It answers "is there something to look at", nothing more. The slide that
motivated the whole `ok: true` trap returned 131,541 characters of
well-formed HTML in which the title overflowed its box, the accent rule
struck through a word, and the literal placeholder `SUBHEAD` was still there.
A non-empty check passes that slide, and would pass it every time. Quality
rests on the character budgets applied when the script is generated.
"""

from opennotebook import storage
from opennotebook.build.clean import ascii_lower
from opennotebook.build.errors import EmptyAudio, SlideMissing
from opennotebook.build.slides import slide_path
from opennotebook.domain.sessions import Part, in_order


def slides_are_written(sid: object, parts: list[Part]) -> None:
    """Every slide was written to the files volume as a complete HTML
    document.

    The writer never leaves a slide out — one the model did not deliver is
    drawn plain — so this guards the one thing that could still go wrong
    after it: a file that is missing or cut short, which the player would
    show as a blank.
    """
    for part in in_order(parts):
        path = slide_path(sid, part.slide)
        try:
            html = storage.read(path).decode(errors="replace")
        except OSError:
            html = ""
        if "</html>" not in ascii_lower(html):
            raise SlideMissing(part.slide, path)


def narration_is_playable(parts: list[Part]) -> None:
    """Every line has audio, and that audio lasts a non-zero time.

    A zero duration means synthesis returned a header and no samples, which
    plays as nothing and would otherwise reach the player as a slide that
    advances instantly. Both halves of the check fail on their own: a path
    with no duration and a duration of zero are different bugs.
    """
    for part in in_order(parts):
        for line in part.lines_in_order():
            if line.audio_path is None or not line.duration_ms or line.duration_ms <= 0:
                raise EmptyAudio(line.line_id)
