"""Building Office files in memory, part by part, so each test states exactly
the XML it is about."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def package(parts: Sequence[tuple[str, str]]) -> bytes:
    """A zip holding the given parts, deflated as Office writes them."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts:
            zf.writestr(name, body)
    return buf.getvalue()


def rels(entries: Sequence[tuple[str, str, str]]) -> str:
    """A `.rels` part from `(id, type suffix, target)`; a target starting with
    `http` is marked external."""
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    ]
    for rid, kind, target in entries:
        mode = ' TargetMode="External"' if target.startswith("http") else ""
        out.append(f'<Relationship Id="{rid}" Type="{REL}/{kind}" Target="{target}"{mode}/>')
    out.append("</Relationships>")
    return "".join(out)
