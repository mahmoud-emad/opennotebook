"""A YouTube video as a source: its captions, with a time mark every minute
or so, titled as YouTube titles it.

Captions are what the video's owner wrote, or YouTube's own when there are
none; written ones are preferred, in the language asked for first. Nothing
is downloaded but the captions. A video without any is refused with what to
do instead: upload its audio, which the studio transcribes (`media.py`).
"""

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
# A time mark starts a new paragraph at most this often.
MARK_EVERY_S = 60.0


class NoCaptions(Exception):
    """Why the video cannot be read, as a sentence."""


@dataclass
class Video:
    id: str
    title: str
    text: str


def video_id(url: str) -> str | None:
    """The video a YouTube link points to, or None for any other link."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    found: str | None = None
    if host in ("youtu.be", "www.youtu.be"):
        found = u.path.strip("/").split("/")[0]
    elif host in _HOSTS:
        if u.path == "/watch":
            found = (parse_qs(u.query).get("v") or [""])[0]
        else:
            parts = u.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
                found = parts[1]
    return found if found and _ID.fullmatch(found) else None


def _clock(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}:{m:02d}:{s % 60:02d}" if h else f"{m}:{s % 60:02d}"


def paragraphs(snippets: list[tuple[float, str]]) -> str:
    """Caption lines joined into paragraphs, each opening with its time
    (`[1:05]`), a new one about every minute."""
    out: list[str] = []
    words: list[str] = []
    start = 0.0
    for at, line in snippets:
        line = " ".join(line.replace("\n", " ").split())
        if not line:
            continue
        if words and at - start >= MARK_EVERY_S:
            out.append(f"[{_clock(start)}] " + " ".join(words))
            words = []
        if not words:
            start = at
        words.append(line)
    if words:
        out.append(f"[{_clock(start)}] " + " ".join(words))
    return "\n\n".join(out)


def _captions(vid: str, languages: tuple[str, ...]) -> list[tuple[float, str]]:
    """The video's captions as (start, line), by YouTube's own API."""
    api = YouTubeTranscriptApi()
    try:
        listed = api.list(vid)
        try:
            chosen = listed.find_manually_created_transcript(languages)
        except NoTranscriptFound:
            try:
                chosen = listed.find_transcript(languages)
            except NoTranscriptFound:
                chosen = next(iter(listed))
        return [(s.start, s.text) for s in chosen.fetch()]
    except StopIteration:
        raise NoCaptions(_NONE) from None
    except TranscriptsDisabled, NoTranscriptFound:
        raise NoCaptions(_NONE) from None
    except VideoUnavailable, VideoUnplayable:
        raise NoCaptions(
            "That video is not available: it may be private, removed or blocked in this region."
        ) from None
    except AgeRestricted:
        raise NoCaptions(
            "That video is age-restricted, so its captions cannot be read without signing in. "
            "Upload its audio instead."
        ) from None
    except RequestBlocked, IpBlocked:
        raise NoCaptions(
            "YouTube refused to answer this server, as it does for many cloud servers. Upload "
            "the video's audio instead, or try again from another network."
        ) from None
    except YouTubeTranscriptApiException:
        raise NoCaptions(
            "YouTube's captions for that video could not be read. Try again, or upload its "
            "audio instead."
        ) from None


_NONE = (
    "That video has no captions to read. Upload its audio or video file instead, and the "
    "studio will transcribe it."
)


async def title_of(http: httpx.AsyncClient, url: str) -> str:
    """The video's title from YouTube's oEmbed, or empty when it says none."""
    try:
        r = await http.get(
            "https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=15
        )
        if r.is_success:
            return " ".join(str((r.json() or {}).get("title") or "").split())
    except httpx.HTTPError, ValueError:
        pass
    return ""


async def read(http: httpx.AsyncClient, url: str, languages: tuple[str, ...] = ("en",)) -> Video:
    """A YouTube video's captions as text, or `NoCaptions` saying why not."""
    vid = video_id(url)
    if vid is None:
        raise NoCaptions("That is not a link to a YouTube video.")
    lines, title = await asyncio.gather(
        asyncio.to_thread(_captions, vid, languages), title_of(http, url)
    )
    text = paragraphs(lines)
    if not text.strip():
        raise NoCaptions(_NONE)
    return Video(vid, title, text)
