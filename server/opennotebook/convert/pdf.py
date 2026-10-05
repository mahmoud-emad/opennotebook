"""PDF to Markdown, through PDFium's text layer (pypdfium2).

A PDF has no paragraphs, only characters placed on a page. PDFium gives each
page's characters in content order with the line ends and word spaces it
infers; this module uses the characters' positions only to see where the
layout leaves a gap (lines further apart than the page's usual line spacing,
a wide gap between table columns) and then joins the lines of each block back
into one paragraph, since a paragraph split at every line end searches and
reads badly. Words are never changed, with one exception: a word the
typesetter hyphenated across a line end is put back together, and only when
the document itself shows that it is one word (see `join_hyphenated`).

A page with no text layer, such as a scan, contributes nothing at all, not even
a page heading, so a scanned PDF comes back nearly empty and callers can tell
it apart by its length.
"""

from __future__ import annotations

import ctypes
import itertools
import statistics
import threading
from collections.abc import Sequence
from typing import Any, NamedTuple

import pypdfium2
import pypdfium2.raw as pdfium_c

from .kinds import ConvertError, InputKind, Problem
from .markdown import tidy_line

KIND = InputKind.PDF

# A line shorter than this share of the page's usual line length, ending a
# sentence, is taken as the last line of its paragraph.
SHORT_LINE = 0.7

# Lines further apart than this multiple of the page's usual line pitch (the
# distance between baselines) have a gap between them: they belong to
# different blocks.
BLOCK_GAP = 1.4

# A space wider than this many times the font size separates columns of a
# laid-out row rather than two words.
COLUMN_GAP = 1.5

# How a column gap is written: wide enough for `looks_tabular` to see it.
COLUMN_SEPARATOR = "   "

# PDFium is not thread-safe, and the server may convert from several threads.
_PDFIUM = threading.Lock()

# Characters PDFium reports that are not text: NUL, and the markers it uses
# for a hyphen it believes is a line-end break (U+0002 and U+FFFE), which are
# written as the hyphen they stand for.
_HYPHEN_MARKERS = {0x02, 0xFFFE}


def convert(data: bytes) -> str:
    head = data[:1024]
    if not data.startswith(b"%PDF") and b"%PDF-" not in head:
        raise ConvertError(KIND, Problem.NOT_VALID)
    with _PDFIUM:
        pages = _extract(data)
    vocab = vocabulary(pages)
    return "\n\n".join(p for page in pages for p in paragraphs(page, vocab))


def _extract(data: bytes) -> list[str]:
    try:
        doc: Any = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as exc:
        if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise ConvertError(KIND, Problem.ENCRYPTED) from exc
        raise ConvertError(KIND, Problem.NOT_VALID) from exc
    try:
        count = len(doc)
        # A document with no pages is not one a person made; PDFium opens
        # some broken files this way.
        if count == 0:
            raise ConvertError(KIND, Problem.NOT_VALID)
        pages: list[str] = []
        failed = 0
        for index in range(count):
            try:
                pages.append(page_text(doc[index]))
            except pypdfium2.PdfiumError:
                # One unreadable page should not cost the rest of the document.
                failed += 1
        if failed == count:
            raise ConvertError(KIND, Problem.DAMAGED)
        return pages
    finally:
        doc.close()


def page_text(page: Any) -> str:
    """A page's text as lines, with a blank line where the layout leaves a gap
    between blocks and `COLUMN_SEPARATOR` where it leaves one between columns."""
    textpage: Any = page.get_textpage()
    try:
        return _layout(textpage)
    finally:
        textpage.close()
        page.close()


class Line(NamedTuple):
    text: str
    # The baseline of the line's first character, in page units from the
    # bottom, and its font size.
    baseline: float
    size: float


def _layout(textpage: Any) -> str:
    raw = textpage.raw
    n: int = textpage.count_chars()
    chars: list[str] = []
    for i in range(n):
        code: int = pdfium_c.FPDFText_GetUnicode(raw, i)
        if code in _HYPHEN_MARKERS:
            chars.append("-")
        elif code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            chars.append("")
        else:
            chars.append(chr(code))

    x, y = ctypes.c_double(), ctypes.c_double()

    def baseline(i: int) -> float | None:
        ok = pdfium_c.FPDFText_GetCharOrigin(raw, i, ctypes.byref(x), ctypes.byref(y))
        return float(y.value) if ok else None

    def edges(i: int) -> tuple[float, float] | None:
        try:
            left, _, right, _ = textpage.get_charbox(i)
        except pypdfium2.PdfiumError:
            return None
        return float(left), float(right)

    def column_gap(before: int, after: int) -> bool:
        a, b = edges(before), edges(after)
        size = float(pdfium_c.FPDFText_GetFontSize(raw, after))
        return a is not None and b is not None and size > 0 and b[0] - a[1] > size * COLUMN_GAP

    lines: list[Line] = []
    current: list[str] = []
    first: int | None = None
    last_visible: int | None = None
    pending_space = False

    def end_line() -> None:
        if first is not None:
            base = baseline(first)
            size = float(pdfium_c.FPDFText_GetFontSize(raw, first))
            lines.append(Line("".join(current), base if base is not None else 0.0, size))
        current.clear()

    for i, c in enumerate(chars):
        if c in ("\r", "\n"):
            # PDFium ends a line with "\r\n"; either one alone also counts.
            if c == "\n" or i + 1 >= n or chars[i + 1] != "\n":
                end_line()
                first, last_visible, pending_space = None, None, False
            continue
        if c.isspace() and c != "\t":
            pending_space = True
            continue
        if not c:
            continue
        if first is None:
            first = i
        elif pending_space and last_visible is not None:
            # The gap after a bullet marker is indentation, not a column.
            after_marker = last_visible == first and not chars[first].isalnum()
            wide = not after_marker and column_gap(last_visible, i)
            current.append(COLUMN_SEPARATOR if wide else " ")
        pending_space = False
        current.append(c)
        last_visible = i
    end_line()
    return "\n".join(_with_gaps(lines))


def _with_gaps(lines: Sequence[Line]) -> list[str]:
    """The lines' text, with an empty line wherever the layout leaves a gap
    between blocks."""
    pitches = [
        prev.baseline - line.baseline
        for prev, line in itertools.pairwise(lines)
        if prev.baseline - line.baseline > 0
    ]
    usual = statistics.median(pitches) if pitches else 0.0
    out: list[str] = []
    for index, line in enumerate(lines):
        if index > 0:
            prev = lines[index - 1]
            pitch = prev.baseline - line.baseline
            # A line above the previous one starts a new column or region.
            new_region = pitch < -max(prev.size, line.size) / 2
            if new_region or (usual > 0 and pitch > usual * BLOCK_GAP):
                out.append("")
        out.append(line.text)
    return out


def vocabulary(pages: Sequence[str]) -> set[str]:
    """Every word in the document, lower-cased, with its surrounding punctuation
    removed. Used as evidence when deciding whether a hyphen at a line end
    belongs to the word."""
    words = (word_core(w) for p in pages for w in p.split())
    return {w for w in words if w}


def _keep(c: str) -> bool:
    return c.isalnum() or c == "-"


def word_core(word: str) -> str:
    start, end = 0, len(word)
    while start < end and not _keep(word[start]):
        start += 1
    while end > start and not _keep(word[end - 1]):
        end -= 1
    return word[start:end].lower()


def paragraphs(page: str, vocab: set[str]) -> list[str]:
    """A page's text as paragraphs, each the lines of one block joined."""
    lines = [line.strip() for line in page.split("\n")]
    usual = usual_length(lines)

    out: list[str] = []
    current = ""
    # Lines joined into `current` so far.
    joined = 0

    def flush() -> None:
        nonlocal current
        if current:
            out.append(current)
            current = ""

    for raw in lines:
        if not current:
            joined = 0
        if not raw:
            flush()
            continue
        line = tidy_line(raw)
        # A list item or a laid-out row starts a line of its own.
        if starts_item(line) or looks_tabular(raw):
            flush()
        # A short first line with no closing punctuation, followed by a line
        # that starts a sentence, is a title or label on a line of its own.
        if current and joined == 1 and is_label(current, usual) and line[0].isupper():
            flush()
        current = line if not current else join_hyphenated(current, line, vocab)
        joined += 1
        if looks_tabular(raw) or (ends_sentence(line) and len(line) < usual * SHORT_LINE):
            flush()
    flush()
    return out


def usual_length(lines: Sequence[str]) -> float:
    """The length of a typical full line on the page: the upper quartile, so a
    page of short lines (a list, a title page) is not measured against prose."""
    lengths = sorted(len(line) for line in lines if line)
    if not lengths:
        return 0.0
    return float(lengths[len(lengths) * 3 // 4])


def is_label(line: str, usual: float) -> bool:
    """A line that reads as a heading or label: short, and not ending like a
    sentence or a clause."""
    return len(line) < usual * SHORT_LINE and not line.endswith((".", ",", ";", ":", "!", "?", "-"))


def ends_sentence(line: str) -> bool:
    return line.rstrip("\"')”’]").endswith((".", "!", "?", ":"))


def starts_item(line: str) -> bool:
    """Bullets and numbered items, as a PDF writes them: the marker is text."""
    if line.startswith(("•", "◦", "▪", "‣", "●", "○", "■", "–", "—", "- ", "* ")):
        return True
    digits = 0
    while digits < len(line) and line[digits] in "0123456789":
        digits += 1
    return 1 <= digits <= 3 and line[digits:].startswith((". ", ") "))


def looks_tabular(raw: str) -> bool:
    """Table rows are written with their columns set apart by
    `COLUMN_SEPARATOR`. Such a line is a row, and joining it to its neighbours
    would mix cells of different rows."""
    return "   " in raw.strip()


def join_hyphenated(current: str, nxt: str, vocab: set[str]) -> str:
    """Append the next line to a paragraph. Lines are joined with a space,
    except where the previous line ends in a hyphen inside a word:

    - a soft hyphen (U+00AD) only ever marks a break point, so it goes;
    - if the joined word appears elsewhere in the document without the hyphen
      ("replication"), the hyphen was the typesetter's and goes;
    - otherwise the hyphen stays and the halves are joined without a space (a
      hyphen right after a letter is never followed by a space in prose),
      which keeps a compound such as "well-known" intact and at worst leaves a
      rare split word as "replica-tion", with no text lost.
    """
    if current.endswith("­"):
        return current[:-1] + nxt
    hyphenated = (
        current.endswith("-") and len(current) >= 2 and current[-2].isalpha() and nxt[:1].isalnum()
    )
    if not hyphenated:
        return f"{current} {nxt}"
    head = current.rsplit(None, 1)[-1].rstrip("-") if current.strip() else ""
    tail = nxt.split(None, 1)[0] if nxt.strip() else ""
    continues_word = nxt[:1].islower()
    if continues_word and word_core(head + tail) in vocab:
        current = current[:-1]
    return current + nxt
