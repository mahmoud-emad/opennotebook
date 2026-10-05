"""Bad input is an error a person can read, never a stray exception."""

from __future__ import annotations

import io
import zipfile

import pytest

from opennotebook.convert import ConvertError, InputKind, Problem, to_markdown

from .common import package, rels

KINDS = (InputKind.WORD, InputKind.EXCEL, InputKind.POWERPOINT, InputKind.PDF)


def assert_readable(err: ConvertError) -> None:
    text = str(err)
    assert text
    assert "{" not in text, text
    # A sentence for a person: it starts like one, ends like one, and says
    # what to do next.
    assert text.startswith("This "), text
    assert text.endswith("."), text
    assert ". " in text, text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a document at all",
        b"PK\x03\x04 truncated zip header",
        b"%PDF-1.7\n1 0 obj << /Type /Catalog >> broken",
    ],
)
def test_garbage_is_refused_for_every_kind(kind: InputKind, data: bytes) -> None:
    with pytest.raises(ConvertError) as caught:
        to_markdown(data, kind)
    assert_readable(caught.value)


def test_a_damaged_part_is_refused() -> None:
    data = package(
        [
            ("_rels/.rels", rels([("rId1", "officeDocument", "word/document.xml")])),
            ("word/document.xml", "<w:document><w:body><w:p>"),
        ]
    )
    with pytest.raises(ConvertError) as caught:
        to_markdown(data, InputKind.WORD)
    assert_readable(caught.value)
    assert caught.value.problem is Problem.DAMAGED
    assert str(caught.value).startswith(
        "This Word document is damaged and its text could not be read."
    )


def test_a_password_protected_file_says_so() -> None:
    # Encrypted Office files are OLE compound files, not zips.
    data = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]) + bytes(512)
    for kind in (InputKind.WORD, InputKind.EXCEL, InputKind.POWERPOINT):
        with pytest.raises(ConvertError) as caught:
            to_markdown(data, kind)
        assert "password-protected" in str(caught.value), str(caught.value)
        assert_readable(caught.value)


def test_a_zip_bomb_stops_at_the_budget() -> None:
    # 210 MB of spaces deflates to a few hundred kilobytes.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("word/document.xml", "w") as part:
            chunk = b" " * (1024 * 1024)
            for _ in range(210):
                part.write(chunk)
        zf.writestr("_rels/.rels", rels([("rId1", "officeDocument", "word/document.xml")]))
    data = buf.getvalue()
    assert len(data) < 2 * 1024 * 1024

    for kind in (InputKind.WORD, InputKind.EXCEL):
        with pytest.raises(ConvertError) as caught:
            to_markdown(data, kind)
        assert_readable(caught.value)
        assert "too large" in str(caught.value), f"{kind}: {caught.value}"


def test_a_declared_entity_is_refused() -> None:
    # Office parts never declare a DTD; one that does is either broken or an
    # entity-expansion attack, and either way its text is not read.
    doc = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "boom">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    data = package(
        [
            ("_rels/.rels", rels([("rId1", "officeDocument", "word/document.xml")])),
            ("word/document.xml", doc),
        ]
    )
    with pytest.raises(ConvertError) as caught:
        to_markdown(data, InputKind.WORD)
    assert caught.value.problem is Problem.DAMAGED
