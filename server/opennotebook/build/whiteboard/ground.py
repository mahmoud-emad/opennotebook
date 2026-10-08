"""Whether what is written on the board was said: every label and number of
a scene must come from its narration or from the source passages it was
given (docs/video-overview-spec.md, section 4.7).

Deterministic and free. A word counts as said when it, or its singular, is
in the narration or the passages; a number counts when the same value is
there, however it is written ("75%" and "75 percent", "1,000" and "1000").
Words that carry no meaning ("the", "of") are not checked. A label that
fails is named with the words that were not said, so the repair can put the
narration's own words in their place.
"""

import re
from dataclasses import dataclass

from opennotebook.build.whiteboard.scene import Scene

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Words that name nothing on a board.
FILLER = frozenset(
    WORD.findall(
        "a an the and or of to in on for with by is are was be it its this that as at from "
        "into over under your their our we you they he she his her them how why what when "
        "not no yes than then so very more most less each every all any some one "
        # A unit written out is the sign written as a word: "75 percent" is "75%".
        "percent per cent"
    )
)
# A label of joining marks alone ("+", "=", "→") says nothing to check.
MARKS = re.compile(r"^[\s+=→←↑↓\-–—/&?!.,:;()'\"]*$")


def _forms(w: str) -> set[str]:
    """A word and its other number, near enough: "leaves" and "leaf",
    "boxes" and "box", "disk" and "disks". `w` is in lower case."""
    out = {w, w + "s", w + "es"}
    if w.endswith("ies") and len(w) > 4:
        out.add(w[:-3] + "y")
    if w.endswith("ves") and len(w) > 4:
        out |= {w[:-3] + "f", w[:-3] + "fe"}
    if w.endswith("es") and len(w) > 3:
        out.add(w[:-2])
    if w.endswith("s") and len(w) > 3:
        out.add(w[:-1])
    if w.endswith("y"):
        out.add(w[:-1] + "ies")
    if w.endswith("f"):
        out.add(w[:-1] + "ves")
    return out


def _value(n: str) -> str:
    """A number as its value: "1,000" and "1000" are one, as are "3.0"
    and "3"."""
    digits = n.replace(",", "")
    try:
        f = float(digits)
    except ValueError:
        return n
    return str(int(f)) if f == int(f) else str(f)


@dataclass(frozen=True)
class Said:
    """Everything a scene may write: the words and numbers of its narration
    and its passages."""

    words: frozenset[str]
    numbers: frozenset[str]

    @classmethod
    def of(cls, texts: list[str]) -> Said:
        words: set[str] = set()
        numbers: set[str] = set()
        for t in texts:
            words.update(w.lower() for w in WORD.findall(t))
            numbers.update(_value(n) for n in NUMBER.findall(t))
        return cls(frozenset(words), frozenset(numbers))

    def unsaid(self, label: str) -> list[str]:
        """The words and numbers of `label` that were not said."""
        out: list[str] = []
        for n in NUMBER.findall(label):
            if _value(n) not in self.numbers:
                out.append(n)
        for w in WORD.findall(label):
            low = w.lower()
            if low in FILLER or len(low) < 2:
                continue
            if not (_forms(low) & self.words):
                out.append(w)
        return out


def written(sc: Scene) -> list[tuple[str, str]]:
    """Every piece of text the scene asserts with, with what it is: each
    element's label and figure. The title is a heading, not a claim ("Making
    Sugar" names what the scene is about in its own words), so it is not
    held to them; a real run had two scenes drawn plain over their titles
    alone."""
    out: list[tuple[str, str]] = []
    for e in sc.elements:
        for text in (e.label, e.text):
            if text.strip() and not MARKS.match(text):
                out.append((f"{e.kind} {e.id or '?'}", text))
    return out


def problems(sc: Scene, said: Said) -> list[str]:
    """Each piece of text the scene writes that was not said, with the
    words to replace."""
    found: list[str] = []
    for what, text in written(sc):
        missing = said.unsaid(text)
        if missing:
            found.append(
                f"{what} says {text!r}, but {', '.join(repr(m) for m in missing)} is not in the "
                "narration or the sources: use the narration's own words"
            )
    return found


def counts(sc: Scene, said: Said) -> tuple[int, int]:
    """How many pieces of text the scene writes, and how many are grounded."""
    texts = written(sc)
    return len(texts), sum(1 for _, t in texts if not said.unsaid(t))
