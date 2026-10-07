"""Synthesis: one WAV per line, one voice id per speaker. A port of
`opennotebook_build/src/narrate.rs`.

Through `speech`, which always hands back a 24 kHz mono 16-bit WAV, so every
line's duration can be read from its own header and the lines join without
re-encoding. Files: `audio/<sid>/<line>.wav` on the files volume, the layout
the Rust studio wrote and the player reads.
"""

import asyncio

from opennotebook import speech, storage
from opennotebook.build import wav
from opennotebook.build.errors import BuildError, EmptyAudio, FilesNotWritten, UnknownSpeaker, Voice
from opennotebook.domain.sessions import Line, Part, Speaker
from opennotebook.script.budget import speakable

# Lines synthesised at once. Four keeps a local speech server's engines busy
# without starving the rest of the box: one line at a time left all but one
# idle, and a 20-minute session spent about twelve minutes here at 1.6x real
# time.
TTS_PARALLEL = 4


def audio_dir(sid: object) -> str:
    return f"audio/{sid}"


def line_path(sid: object, line_id: str) -> str:
    return f"{audio_dir(sid)}/{line_id}.wav"


async def synthesise_line(client: speech.Speech, sid: object, line: Line, voice_id: str) -> None:
    """One line. The duration comes from the header of the file that will
    play, not from an estimate and not from the text length."""
    # Spoken, not written. A dash is a held beat on the page and silence to
    # Kokoro, so the clauses either side run together into one breathless
    # phrase. `speakable` turns it into a stop the voice actually takes. The
    # STORED text keeps its dash: that is what the subtitle and the transcript
    # show, and it is what the writer wrote.
    try:
        data, words = await client.synthesize_timed(speakable(line.text), voice_id)
    except speech.SpeechError as e:
        raise Voice(str(e), e.sentence) from e
    # When the server times its words, they are kept: they are what a video
    # draws on and a transcript highlights. Times are of the spoken text,
    # which `speakable` changes only in punctuation.
    line.cues = [w.as_cue() for w in words]
    if not data:
        raise EmptyAudio(line.line_id)
    duration = wav.duration_of(data, line.line_id)
    try:
        path = storage.put(line_path(sid, line.line_id), data)
    except OSError as e:
        raise FilesNotWritten(f"line {line.line_id}: {e}") from e
    line.audio_path = path
    line.duration_ms = duration


async def synthesise_all(
    client: speech.Speech, sid: object, speakers: list[Speaker], parts: list[Part]
) -> None:
    """Synthesise every line of every part, writing one WAV per line.

    Mutates the parts in place so that a failure part way through leaves the
    lines that did succeed carrying their `audio_path` and `duration_ms`. The
    caller persists that: a half-built output is worth keeping, and its state
    is what stops it being read as finished.
    """
    voices = {s.speaker_id: s.voice_id for s in speakers}
    # Every line, in playing order, with its voice resolved up front so an
    # unknown speaker fails before anything is synthesised.
    work: list[tuple[Line, str]] = []
    for part in sorted(parts, key=lambda p: p.ordinal):
        for line in part.lines_in_order():
            voice = voices.get(line.speaker_id)
            if voice is None:
                raise UnknownSpeaker(line.line_id, line.speaker_id)
            work.append((line, voice))

    gate = asyncio.Semaphore(TTS_PARALLEL)

    async def one(line: Line, voice: str) -> None:
        async with gate:
            await synthesise_line(client, sid, line, voice)

    # Every finished line is kept even when another fails, so a half-built
    # output carries the audio it does have; the first error is raised.
    results = await asyncio.gather(*(one(line, v) for line, v in work), return_exceptions=True)
    for r in results:
        if isinstance(r, BuildError):
            raise r
        if isinstance(r, BaseException):
            raise r


def episode(parts: list[Part]) -> bytes:
    """Every line, in order, as one WAV with natural pauses between them.
    NotebookLM's download is a WAV too."""
    clips = [(lid, storage.read(path), gap) for lid, path, gap in wav.episode_plan(parts)]
    return wav.join(clips)
