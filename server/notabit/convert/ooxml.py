"""The zip container shared by `.docx`, `.pptx` and `.xlsx`: reading parts under a
decompression budget, and following relationships between them."""

from __future__ import annotations

import io
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from .kinds import ConvertError, InputKind, Problem
from .xmltree import El, Malformed, parse

# The most this package will decompress out of one file, across all the parts
# it reads. Real documents are nowhere near it; a zip bomb is far past it, and
# stopping at the budget is what keeps a 50 KB upload from becoming gigabytes
# of memory.
MAX_DECOMPRESSED = 200 * 1024 * 1024

# The signature of an OLE compound file. A password-protected Office file is
# one of these rather than a zip, as is the older binary format, and saying so
# is more useful than "not a valid document".
OLE_SIGNATURE = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])

_CHUNK = 1024 * 1024

# What reading a broken zip entry can raise: a bad header or checksum, a
# corrupt deflate stream, a compression method Python lacks, or an encrypted
# entry.
_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError, OSError)

# A document type declaration, in the encodings an XML part may use. Office
# never writes one, and it is the only way to declare the entities an
# expansion attack needs.
_DOCTYPE = [b"<!DOCTYPE", "<!DOCTYPE".encode("utf-16-le"), "<!DOCTYPE".encode("utf-16-be")]


@dataclass(frozen=True)
class Rel:
    """One relationship out of a `.rels` part."""

    # The relationship type URI. Matched on its last segment, since the strict
    # format uses different prefixes for the same types.
    kind: str
    # For an internal target, the part's full path in the zip; for an
    # external one, the target as written (usually a URL).
    target: str
    external: bool

    def is_(self, kind: str) -> bool:
        """Whether the type URI ends in this segment, e.g. `"hyperlink"`."""
        return self.kind.rsplit("/", 1)[-1] == kind


type Rels = dict[str, Rel]


class Package:
    def __init__(self, data: bytes, kind: InputKind) -> None:
        self.kind = kind
        if data.startswith(OLE_SIGNATURE):
            raise ConvertError(kind, Problem.ENCRYPTED)
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self._zip.infolist()
        except (*_ZIP_ERRORS, ValueError) as exc:
            raise ConvertError(kind, Problem.NOT_VALID) from exc
        # Lower-cased part name to its entry: part names are case-insensitive
        # in the packaging rules, and producers do not all agree on case.
        self._names: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            self._names.setdefault(info.filename.lower(), info)
        self.budget = MAX_DECOMPRESSED

    def has(self, part: str) -> bool:
        return part.lstrip("/").lower() in self._names

    def read_bytes(self, part: str) -> bytes | None:
        """Decompress one part, or `None` when the package has no such part.
        Every byte read counts against the budget, so a single oversized part
        fails as soon as it crosses it rather than after it has been inflated."""
        info = self._names.get(part.lstrip("/").lower())
        if info is None:
            return None
        out = bytearray()
        for chunk in self._chunks(info):
            out += chunk
        return bytes(out)

    def _chunks(self, info: zipfile.ZipInfo) -> Iterator[bytes]:
        """An entry's bytes a chunk at a time, charged to the budget as they
        come."""
        try:
            with self._zip.open(info) as f:
                while True:
                    chunk = f.read(min(_CHUNK, self.budget + 1))
                    if not chunk:
                        return
                    if len(chunk) > self.budget:
                        raise ConvertError(self.kind, Problem.TOO_LARGE)
                    self.budget -= len(chunk)
                    yield chunk
        except _ZIP_ERRORS as exc:
            raise ConvertError(self.kind, Problem.DAMAGED) from exc

    def read_xml(self, part: str) -> El | None:
        """Read and parse an XML part."""
        data = self.read_bytes(part)
        if data is None:
            return None
        try:
            return parse(data)
        except Malformed as exc:
            raise ConvertError(self.kind, Problem.DAMAGED) from exc

    def rels(self, part: str) -> Rels:
        """The relationships of a part (`""` for the package itself), with
        internal targets resolved to full part paths. A part with no `.rels`
        simply has no relationships."""
        cut = part.rfind("/") + 1
        folder, file = part[:cut], part[cut:]
        root = self.read_xml(f"{folder}_rels/{file}.rels")
        rels: Rels = {}
        if root is None:
            return rels
        for rel in root.elements():
            rid, kind, target = rel.attr("Id"), rel.attr("Type"), rel.attr("Target")
            if rid is None or kind is None or target is None:
                continue
            external = rel.attr("TargetMode") == "External"
            rels[rid] = Rel(kind, target if external else resolve(folder, target), external)
        return rels

    def main_part(self, conventional: str) -> str:
        """The package's main part, found the way the format intends (through
        the package relationships) and falling back to the conventional path
        for producers that leave the relationship out."""
        found = next(
            (
                r.target
                for r in self.rels("").values()
                if r.is_("officeDocument") and not r.external
            ),
            conventional,
        )
        if not self.has(found):
            raise ConvertError(self.kind, Problem.NOT_VALID)
        return found

    def check_budget(self, refuse_doctype: bool = False) -> None:
        """Decompress every entry once, counting against the budget, and throw
        the bytes away. Used before handing the archive to a library that does
        its own unbounded reading. With `refuse_doctype`, a part that declares
        a DTD is refused as damaged, since that library's XML parser would
        otherwise be the one to decide what its entities expand to."""
        for info in self._zip.infolist():
            tail = b""
            for chunk in self._chunks(info):
                if refuse_doctype:
                    window = tail + chunk
                    if any(d in window for d in _DOCTYPE):
                        raise ConvertError(self.kind, Problem.DAMAGED)
                    # Keep enough of the end to catch a declaration split
                    # across two chunks.
                    tail = window[-32:]

    def close(self) -> None:
        self._zip.close()


def resolve(folder: str, target: str) -> str:
    """Resolve a relationship target against the folder of the part that holds
    the relationship. Targets may be absolute (`/word/media/x.png`) or
    relative with `..` segments (`../slides/slide1.xml`)."""
    joined = target[1:] if target.startswith("/") else folder + target
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)
