"""A motion video: the model writes one HTML page that animates the whole
narration (`write.py`), the studio times it against the timeline
(`page.py`), holds it to what it declared (`lint.py`, `ground.py`), records
it frame by frame in a browser (`render.py`) and encodes the frames with
the episode's audio (`encode.py`).

The contract and the reasons for every choice are in
`docs/motion-style.md`; the pipeline it sits in is
`docs/video-overview-spec.md`.
"""

from opennotebook.build.motion import lint, page, render, scene, write

__all__ = ["lint", "page", "render", "scene", "write"]
