"""Document bytes in, Markdown out, verbatim.

This is the only place OpenNotebook turns an uploaded Word, PowerPoint, Excel or
PDF file into text, and everything it produces is the document's own words.
There is no model and no network anywhere in this package, and no step
rewrites, summarises or "cleans up" a sentence: grounding depends on the stored
text being exactly what the source says, so the converter may drop layout but
never wording.

The Office formats are read straight from their XML parts (see `docx`, `pptx`)
because only the raw parts say where a text box, a footnote or a tracked
insertion sits; spreadsheets go through openpyxl, which already knows cell
types and date formats; PDFs go through PDFium's text layer.

Every reader treats its input as hostile. Corrupt bytes come back as a
`ConvertError` a person can read, never a stray exception, and the zip
containers are read under a fixed decompression budget so a small upload
cannot expand into gigabytes.
"""

from __future__ import annotations

from . import docx, pdf, pptx, xlsx
from .kinds import ConvertError, InputKind, Problem
from .markdown import finish

__all__ = ["ConvertError", "InputKind", "Problem", "to_markdown"]

_READERS = {
    InputKind.WORD: docx.convert,
    InputKind.POWERPOINT: pptx.convert,
    InputKind.EXCEL: xlsx.convert,
    InputKind.PDF: pdf.convert,
}


def to_markdown(data: bytes, kind: InputKind) -> str:
    """Convert a document to Markdown.

    The output keeps every piece of the document's text in reading order, with
    structure (headings, lists, tables, slides, sheets) expressed as Markdown.
    A document that holds almost no text, such as a scanned PDF, still comes
    back with whatever little text there is: whether that is enough is the
    caller's decision, and callers make it by length.

    Raises `ConvertError` when the bytes cannot be read as `kind`.
    """
    try:
        md = _READERS[kind](data)
    except ConvertError:
        raise
    except Exception as exc:
        # The parsers underneath are third-party code fed untrusted bytes. An
        # exception from one of them that the readers did not foresee gets the
        # same "damaged" answer a parse error would, never its own text.
        raise ConvertError(kind, Problem.DAMAGED) from exc
    return finish(md)
