"""Reading a duration out of a WAV header, and joining clips into one. A
port of `opennotebook_build/src/wav.rs`, with the episode's pauses from the
Rust server's episode route.

A duration only ever comes from here: Kokoro is not reproducible per render
(the same voice and text produced a 6 ms length difference across two
renders), so a duration carried over from a previous render is wrong by an
unbounded amount.
"""

from opennotebook.build.errors import NotWav
from opennotebook.domain.sessions import Part, in_order


def duration_of(data: bytes, name: str) -> int:
    """Milliseconds of a RIFF/WAVE buffer, from its `fmt ` and `data` chunks.

    Chunks are walked rather than assumed at fixed offsets: a WAV may carry
    `LIST`, `fact` or padding before `data`, and a reader that assumes byte 44
    silently mis-measures those.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise NotWav(name, "not a RIFF/WAVE buffer")
    byte_rate: int | None = None
    data_len: int | None = None
    at = 12
    while at + 8 <= len(data):
        cid = data[at : at + 4]
        size = int.from_bytes(data[at + 4 : at + 8], "little")
        body = at + 8
        if cid == b"fmt " and body + 16 <= len(data):
            byte_rate = int.from_bytes(data[body + 8 : body + 12], "little")
        elif cid == b"data":
            # The declared size can overrun a truncated file; trust the bytes
            # actually present, which is what a player will hear.
            data_len = min(size, len(data) - body)
            break
        at = body + size + (size & 1)  # chunks are word-aligned
    if byte_rate == 0:
        raise NotWav(name, "byte rate is zero")
    if byte_rate is None:
        raise NotWav(name, "no fmt chunk")
    if data_len is None:
        raise NotWav(name, "no data chunk")
    return data_len * 1000 // byte_rate


def _pcm_of(data: bytes) -> tuple[int, int, int, bytes] | None:
    """The format and PCM bytes of a WAV: (channels, sample rate, bits per
    sample, data). Walks the chunks the same way `duration_of` does."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt: tuple[int, int, int, int] | None = None
    at = 12
    while at + 8 <= len(data):
        cid = data[at : at + 4]
        size = int.from_bytes(data[at + 4 : at + 8], "little")
        body = at + 8
        if cid == b"fmt " and body + 16 <= len(data):

            def le16(o: int, body: int = body) -> int:
                return int.from_bytes(data[body + o : body + o + 2], "little")

            rate = int.from_bytes(data[body + 4 : body + 8], "little")
            fmt = (le16(0), le16(2), rate, le16(14))
        elif cid == b"data":
            if fmt is None:
                return None
            code, channels, rate, bits = fmt
            # PCM only: anything else cannot be joined by copying bytes.
            if code != 1:
                return None
            return channels, rate, bits, data[body : min(body + size, len(data))]
        at = body + size + (size & 1)
    return None


def join(clips: list[tuple[str, bytes, int]]) -> bytes:
    """Several WAVs as one, each with its own silence in front of it: an
    audio overview's lines as the one file a listener downloads. The first
    clip's gap is ignored.

    Every clip must share the first one's format; the speech client writes
    them all at 24 kHz mono 16-bit. A clip that does not parse, or differs,
    is an error naming it rather than a file that plays at the wrong speed.
    """
    fmt: tuple[int, int, int] | None = None
    out = bytearray()
    for k, (name, data, gap_ms) in enumerate(clips):
        found = _pcm_of(data)
        if found is None:
            raise NotWav(name, "not a PCM WAV")
        ch, rate, bits, pcm = found
        if fmt is None:
            fmt = (ch, rate, bits)
        elif fmt != (ch, rate, bits):
            raise NotWav(name, "its format differs from the first clip's")
        if k > 0:
            frame = ch * (bits // 8)
            out.extend(bytes(rate * gap_ms // 1000 * frame))
        out.extend(pcm)
    if fmt is None:
        raise NotWav("episode", "no clips to join")
    ch, rate, bits = fmt
    block = ch * (bits // 8)
    return (
        b"RIFF"
        + (36 + len(out)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + ch.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + (rate * block).to_bytes(4, "little")
        + block.to_bytes(2, "little")
        + bits.to_bytes(2, "little")
        + b"data"
        + len(out).to_bytes(4, "little")
        + bytes(out)
    )


# Silence before a line when the speaker changes, when the same speaker goes
# on, and when a new chapter starts. People leave about 200 ms between turns
# in conversation (Stivers et al., 2009); a chapter is a breath longer. None
# of the open podcast generators read for the spec leaves any gap at all.
GAP_TURN_MS = 220
GAP_SAME_MS = 320
GAP_CHAPTER_MS = 650


def gap_before(first_of_chapter: bool, same_speaker: bool) -> int:
    """The pause in front of a line: a new chapter, the same speaker going
    on, or the other speaker answering."""
    if first_of_chapter:
        return GAP_CHAPTER_MS
    return GAP_SAME_MS if same_speaker else GAP_TURN_MS


def episode_plan(parts: list[Part]) -> list[tuple[str, str, int]]:
    """Every voiced line in playing order as (line id, audio path, gap before
    it in ms). Raises `ValueError` when a line has no audio yet."""
    out: list[tuple[str, str, int]] = []
    prev: str | None = None
    for ci, part in enumerate(in_order(parts)):
        for li, line in enumerate(part.lines_in_order()):
            if line.audio_path is None:
                raise ValueError(f"line {line.line_id} is not voiced yet")
            gap = gap_before(ci > 0 and li == 0, prev == line.speaker_id)
            prev = line.speaker_id
            out.append((line.line_id, line.audio_path, gap))
    return out
