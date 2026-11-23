"""A small element tree over lxml, with namespaces resolved to fixed prefixes.

Office parts are walked in document order with a lot of looking around (a
paragraph's style, a cell's span, a shape's placeholder type), which is far
easier on a tree of plain objects than on lxml's text-and-tail model. The parts
are bounded by the zip budget, so holding one in memory is fine.

Names are stored as `prefix:local` with the prefix chosen from the namespace
URI, not copied from the file. Word itself always writes `w:`, but other
producers are free to bind the same namespace to `ns0:` or to a default
namespace, and the "strict" flavour of the format uses different URIs
altogether. Matching on a canonical prefix makes all of those read the same.

The parser treats every part as hostile: no DTD is loaded, no entity is
expanded, nothing is fetched from the network, and libxml2's default depth and
size limits stay on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import lxml.etree as etree

# Deeper nesting than this is not a real document. The walkers in this package
# recurse, so the limit is what keeps a crafted file from overflowing the
# stack. libxml2 stops at a shallower depth on its own; this is the backstop.
MAX_DEPTH = 400

_PREFIXES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main": "w",
    "http://purl.oclc.org/ooxml/wordprocessingml/main": "w",
    "http://schemas.openxmlformats.org/drawingml/2006/main": "a",
    "http://purl.oclc.org/ooxml/drawingml/main": "a",
    "http://schemas.openxmlformats.org/presentationml/2006/main": "p",
    "http://purl.oclc.org/ooxml/presentationml/main": "p",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships": "r",
    "http://purl.oclc.org/ooxml/officeDocument/relationships": "r",
    "http://schemas.openxmlformats.org/package/2006/relationships": "pr",
    "http://schemas.openxmlformats.org/markup-compatibility/2006": "mc",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape": "wps",
    "urn:schemas-microsoft-com:vml": "v",
    "http://schemas.openxmlformats.org/drawingml/2006/diagram": "dgm",
    "http://purl.oclc.org/ooxml/drawingml/diagram": "dgm",
    "http://schemas.openxmlformats.org/officeDocument/2006/math": "m",
    "http://purl.oclc.org/ooxml/officeDocument/math": "m",
}


class Malformed(Exception):
    """Why a part did not parse. The caller turns it into the document-level
    error, which knows the document kind."""


class El:
    """One element: its canonical name, attributes, and children, which are
    elements and runs of text in document order."""

    __slots__ = ("attrs", "children", "name")

    def __init__(self, name: str, attrs: dict[str, str] | None = None) -> None:
        self.name = name
        self.attrs: dict[str, str] = attrs if attrs is not None else {}
        self.children: list[El | str] = []

    def attr(self, name: str) -> str | None:
        """The value of an attribute by its canonical name, e.g. `w:val` or `Id`."""
        return self.attrs.get(name)

    def elements(self) -> Iterator[El]:
        """The element children, skipping text between them."""
        return (c for c in self.children if isinstance(c, El))

    def child(self, name: str) -> El | None:
        return next((e for e in self.elements() if e.name == name), None)

    def children_named(self, name: str) -> Iterator[El]:
        return (e for e in self.elements() if e.name == name)

    def path(self, *names: str) -> El | None:
        """Follow a path of child names, e.g. `("p:nvSpPr", "p:nvPr", "p:ph")`."""
        el: El | None = self
        for name in names:
            if el is None:
                return None
            el = el.child(name)
        return el

    def find(self, name: str) -> El | None:
        """The first descendant with this name, depth first, including `self`."""
        if self.name == name:
            return self
        for e in self.elements():
            found = e.find(name)
            if found is not None:
                return found
        return None

    def find_all(self, name: str, out: list[El] | None = None) -> list[El]:
        """Every descendant with this name, outermost first, not looking inside
        a match."""
        found: list[El] = [] if out is None else out
        for e in self.elements():
            if e.name == name:
                found.append(e)
            else:
                e.find_all(name, found)
        return found

    def own_text(self) -> str:
        """The text directly inside this element, e.g. the content of `<w:t>`."""
        return "".join(c for c in self.children if isinstance(c, str))


def uint(value: str | None) -> int | None:
    """An attribute holding a non-negative whole number, or `None` for anything
    else (absent, negative, or not a number), the way the format's unsigned
    integers parse."""
    if value is None:
        return None
    digits = value.removeprefix("+")
    return int(digits) if digits.isascii() and digits.isdigit() else None


def _parser() -> Any:
    # A parser per call: lxml parsers must not be shared between threads.
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        dtd_validation=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse(data: bytes) -> El:
    """Parse a whole part into its root element. The bytes go to lxml as they
    are, so the encoding declaration and any UTF-8 or UTF-16 byte order mark
    are honoured."""
    try:
        root = etree.fromstring(data, _parser())
    except (etree.LxmlError, ValueError) as exc:
        raise Malformed from exc
    return _convert(root, 0)


def _qualified(tag: str) -> str:
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        # Unknown namespaces keep their local name behind a `?:` so they can
        # never collide with a known one.
        return f"{_PREFIXES.get(uri, '?')}:{local}"
    # Attributes without a prefix (`Id`, `Target`, `lvl`) are in no namespace
    # by the XML rules, and match on their bare name.
    return tag


def _convert(node: Any, depth: int) -> El:
    if depth > MAX_DEPTH:
        raise Malformed
    el = El(_qualified(node.tag), {_qualified(k): v for k, v in node.attrib.items()})
    if node.text:
        _push_text(el, node.text)
    for child in node:
        if child.tag is etree.Entity:
            # An entity the parser did not expand is one declared in a DTD,
            # which no Office part has; reading it as text would lose words.
            raise Malformed
        if isinstance(child.tag, str):
            _push_child(el, _convert(child, depth + 1))
        if child.tail:
            _push_text(el, child.tail)
    return el


def _push_child(parent: El, el: El) -> None:
    """Attach a finished element to its parent, resolving markup compatibility
    on the way: an `mc:AlternateContent` holds the same content more than once
    (a modern rendering and a fallback), and keeping both would print every
    text box twice. The first `mc:Choice` wins, as it would in Word; the
    `mc:Fallback` is used only when there is no choice at all."""
    if el.name != "mc:AlternateContent":
        parent.children.append(el)
        return
    branch = el.child("mc:Choice") or el.child("mc:Fallback")
    if branch is None:
        return
    for c in branch.children:
        if isinstance(c, str):
            _push_text(parent, c)
        else:
            parent.children.append(c)


def _push_text(parent: El, text: str) -> None:
    # Joining neighbouring pieces keeps one text node per run of characters.
    if parent.children and isinstance(parent.children[-1], str):
        parent.children[-1] += text
    else:
        parent.children.append(text)
