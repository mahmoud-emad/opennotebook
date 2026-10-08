"""Give videos made before word timings were kept their words' times, so
their captions light each word as it is said.

A video's script has kept each word's time since the captions were drawn
word by word; a script from before has its lines only, and the watch page
spreads a line's words evenly over it, which drifts from the voice. The
voice's word times can be had again: the same text in the same voice comes
back from the speech server with the same timing. So each line is voiced
again, its clip's length checked against the line's length in the video,
and its words timed from it as a render times them (`timeline.align`). A
line whose new clip is not the length of the old one keeps no words, and
is spread as before.

Run from server/ with the studio's environment (DATABASE_URL, the files
directory, the speech settings):

    uv run python scripts/video_words.py            # every whiteboard video
    uv run python scripts/video_words.py <sid> ...  # these outputs only
"""

import asyncio
import json
import sys
import uuid
from typing import Any

from sqlalchemy import select

from opennotebook import speech, storage
from opennotebook.build import timeline as tl
from opennotebook.build import video, wav
from opennotebook.db.models import Session
from opennotebook.db.session import sessionmaker
from opennotebook.domain.sessions import Line
from opennotebook.script.budget import speakable

STYLE = "whiteboard"
# How far a line's new clip may differ in length from its clip in the video,
# in ms, before its times are taken not to be the video's.
LENGTH_SLACK_MS = 120


async def words_for(
    voice_client: speech.Speech, voice: str, line: dict[str, Any]
) -> list[list[Any]] | None:
    """A script line's words with their times in the video, or None when the
    voice does not come back the same."""
    text = str(line.get("text", ""))
    start, end = int(line["start_ms"]), int(line["end_ms"])
    data, cues = await voice_client.synthesize_timed(speakable(text), voice)
    if not data or not cues:
        return None
    length = wav.duration_of(data, "backfill")
    if abs(length - (end - start)) > LENGTH_SLACK_MS:
        return None
    ln = Line("backfill", "host", 0, text)
    ln.cues = [w.as_cue() for w in cues]
    ln.duration_ms = length
    words, _ = tl.align(ln, start)
    return [[w.text, w.start_ms, w.end_ms] for w in words]


async def backfill(sid: uuid.UUID) -> str:
    async with sessionmaker()() as s:
        o = await s.get(Session, sid)
        if o is None:
            return "gone"
        speakers = [sp for sp in o.speakers if isinstance(sp, dict)]
    voice = str((speakers[0] if speakers else {}).get("voice_id", ""))
    rel = video.script_path(sid, STYLE)
    if not storage.exists(rel):
        return "no script"
    script = json.loads(storage.local_path(rel).read_text(encoding="utf-8"))
    lines = [ln for ln in script.get("lines", []) if isinstance(ln, dict)]
    todo = [ln for ln in lines if not ln.get("words")]
    if not todo:
        return "already timed"
    voice_client = speech.speech()
    timed = 0
    for ln in todo:
        got = await words_for(voice_client, voice, ln)
        if got is not None:
            ln["words"] = got
            timed += 1
    storage.put(rel, json.dumps(script, ensure_ascii=False).encode())
    return f"{timed} of {len(todo)} lines timed"


async def main(args: list[str]) -> None:
    if args:
        sids = [uuid.UUID(a) for a in args]
    else:
        async with sessionmaker()() as s:
            rows = await s.scalars(select(Session.id).where(Session.video.is_not(None)))
            sids = list(rows)
    for sid in sids:
        print(sid, await backfill(sid))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
