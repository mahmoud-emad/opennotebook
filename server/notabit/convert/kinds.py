"""The document kinds the converter reads, and why one could not be read."""

from __future__ import annotations

from enum import Enum


class InputKind(Enum):
    """The document formats this package reads, named the way a person would
    name them rather than by extension, because the extension is only how a
    caller usually learns the kind."""

    WORD = "docx"
    EXCEL = "xlsx"
    POWERPOINT = "pptx"
    PDF = "pdf"

    @classmethod
    def from_extension(cls, ext: str) -> InputKind | None:
        """The kind a file extension names, with or without its dot and in any
        case, or `None` for anything this package cannot read. Only the
        XML-based Office formats are accepted: the older binary `.doc`, `.xls`
        and `.ppt` are a different format entirely and are refused here rather
        than failing later with a confusing message."""
        name = ext.lstrip(".").lower()
        for kind in cls:
            if kind.value == name:
                return kind
        return None

    @property
    def described(self) -> str:
        """How the kind reads in a sentence, e.g. "Word document"."""
        return _DESCRIBED[self]


_DESCRIBED = {
    InputKind.WORD: "Word document",
    InputKind.EXCEL: "Excel workbook",
    InputKind.POWERPOINT: "PowerPoint presentation",
    InputKind.PDF: "PDF",
}

# The program a person would open the file in to save a clean copy.
_APP = {
    InputKind.WORD: "Word",
    InputKind.EXCEL: "Excel",
    InputKind.POWERPOINT: "PowerPoint",
    InputKind.PDF: "a PDF viewer",
}


class Problem(Enum):
    """What went wrong, for callers that branch on it rather than show it."""

    # The bytes are not this kind of file at all: not a zip, not a PDF, or a
    # zip without the parts that make it a document.
    NOT_VALID = "not_valid"
    # The container opened but a part needed for the text is broken.
    DAMAGED = "damaged"
    # The file is password-protected, so its text cannot be read without the
    # password.
    ENCRYPTED = "encrypted"
    # The file would expand past the decompression budget, which is what a
    # zip bomb looks like and also what no real document needs.
    TOO_LARGE = "too_large"


class ConvertError(Exception):
    """Why a document could not be converted.

    The message is written for the person who uploaded the file, since the
    server shows it as it is: no library name, no parser internals, nothing a
    reader would have to decode, and always what to do next. The underlying
    library error is kept only as `__cause__`, for logs, because its text is
    neither stable nor meant for people.
    """

    def __init__(self, kind: InputKind, problem: Problem) -> None:
        self.kind = kind
        self.problem = problem
        super().__init__(self.message)

    @property
    def message(self) -> str:
        what = self.kind.described
        app = _APP[self.kind]
        match self.problem:
            case Problem.NOT_VALID:
                save_as = (
                    "export it to PDF again"
                    if self.kind is InputKind.PDF
                    else f"save it from {app} as .{self.kind.value}"
                )
                return (
                    f"This file is not a valid {what}. Check the file, {save_as}, "
                    "and upload it again."
                )
            case Problem.DAMAGED:
                return (
                    f"This {what} is damaged and its text could not be read. "
                    f"Open it in {app}, save a fresh copy, and upload that."
                )
            case Problem.ENCRYPTED:
                return (
                    f"This {what} is password-protected. Remove the password and upload it again."
                )
            case Problem.TOO_LARGE:
                return (
                    f"This {what} is too large to read once uncompressed. "
                    "Split it into smaller files and upload those."
                )

    def __str__(self) -> str:
        return self.message
