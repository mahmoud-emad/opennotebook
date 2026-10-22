"""The whiteboard's grounding check, over a motion page's manifest
(`docs/video-overview-spec.md` section 4.7, `whiteboard/ground.py`).

What a page declares it writes on screen must come from the narration it
is drawn over or from the sources the video is made from — the same
deterministic word-and-number check a board is held to, asked of the
manifest's labels instead of a scene's elements. The rules live with the
whiteboard's, so a word counted as said for one style is said for the
other.
"""

from opennotebook.build.whiteboard import ground as _board
from opennotebook.build.whiteboard.scene import Claim as _Claim

# What everything a page may write is called and checked as.
Said = _board.Said
FILLER = _board.FILLER

# A page's claims are the whiteboard's: text with the passages behind it.
Claim = _Claim


def unsaid(text: str, said: Said) -> list[str]:
    """The words and numbers of `text` that were not said."""
    return said.unsaid(text)


def counts(texts: list[str], said: Said) -> tuple[int, int]:
    """How many pieces of text a page writes, and how many were said."""
    return len(texts), sum(1 for t in texts if not said.unsaid(t))
