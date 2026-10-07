"""The bundled icons: Tabler's outline set (MIT), 5,184 line icons on a
24×24 grid, each with its tags. Line icons, not filled ones, because a
whiteboard draws a line along its length; a filled shape cannot be drawn on.

The scene writer is shown candidates found here for its concepts, so it
names icons that exist rather than ones it imagines.
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import gzip
import json
import re
from functools import cache
from importlib import resources
from typing import Any

import skia

from opennotebook.build.whiteboard.geometry import svg_path

# The icons' own grid.
SIZE = 24

WORD = re.compile(r"[a-z0-9]+")
# Words that name nothing to draw.
STOP = frozenset(
    WORD.findall(
        "a an the and or of to in on for with by is are was be it its this that as at from into "
        "over under your their our we you they he she his her them how why what when"
    )
)


@cache
def library() -> dict[str, dict[str, Any]]:
    ref = resources.files("opennotebook.build.whiteboard") / "assets" / "icons.json.gz"
    data: dict[str, dict[str, Any]] = json.loads(gzip.decompress(ref.read_bytes()))
    return data


@cache
def _index() -> dict[str, set[str]]:
    """Each word of an icon's name, tags and category, to the icons that
    have it."""
    out: dict[str, set[str]] = {}
    for name, v in library().items():
        words = set(WORD.findall(name.replace("-", " ")))
        for t in v.get("tags", []):
            words.update(WORD.findall(str(t).lower()))
        words.update(WORD.findall(str(v.get("category", "")).lower()))
        for w in words:
            out.setdefault(w, set()).add(name)
    return out


def _forms(w: str) -> set[str]:
    """A word and its singular, near enough: "companies", "leaves" (a leaf,
    not "leave", whose icon is a logout arrow), "boxes", "disks"."""
    out = {w}
    if w.endswith("ies") and len(w) > 4:
        out.add(w[:-3] + "y")
    elif w.endswith("ves") and len(w) > 4:
        out |= {w[:-3] + "f", w[:-3] + "fe"}
    elif w.endswith("es") and len(w) > 4 and w[-3] in "sxz":
        out.add(w[:-2])
    elif w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        out.add(w[:-1])
    return out


def _parts_of(w: str) -> list[str]:
    """A compound the icons do not know, as two words they do: "sunlight"
    is "sun" and "light"."""
    index = _index()
    if w in index or len(w) < 6:
        return [w]
    for i in range(3, len(w) - 2):
        a, b = w[:i], w[i:]
        if a in index and b in index:
            return [a, b]
    return [w]


def category(name: str) -> str:
    return str(library().get(name, {}).get("category", "")) or "Other"


def exists(name: str) -> bool:
    return name in library()


def search(query: str, limit: int = 8) -> list[str]:
    """The icons that best match a concept: a match on the icon's name
    counts more than one on its tags, and a shorter name is the plainer
    icon."""
    words = [p for w in WORD.findall(query.lower()) if w not in STOP for p in _parts_of(w)]
    if not words:
        return []
    index = _index()
    scores: dict[str, float] = {}
    matched: dict[str, set[str]] = {}
    for w in words:
        for cand in _forms(w):
            for name in index.get(cand, ()):
                parts = name.split("-")
                bonus = 3.0 if cand in parts else 1.0
                scores[name] = scores.get(name, 0.0) + bonus
                matched.setdefault(name, set()).add(w)

    # An icon named for the concept comes first ("web server" wants
    # `server`, not a brand tagged web and server), then one whose tags hold
    # more of the concept's words, then the plainer name.
    def named(n: str) -> int:
        parts = set(n.split("-"))
        return sum(1 for w in words if _forms(w) & parts)

    ranked = sorted(scores, key=lambda n: (-named(n), -len(matched[n]), -scores[n], len(n), n))
    return ranked[:limit]


def candidates(concepts: list[str], limit: int = 8) -> dict[str, list[str]]:
    return {c: search(c, limit) for c in concepts if c.strip()}


def path(name: str) -> skia.Path:
    """An icon's outline on its 24×24 grid."""
    return svg_path(list(library()[name]["d"]))
