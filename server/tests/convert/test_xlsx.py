"""Excel workbooks built part by part, and how single values read."""

from __future__ import annotations

from openpyxl.utils.datetime import from_excel

from opennotebook.convert import InputKind, to_markdown
from opennotebook.convert.xlsx import MAX_ROWS, number, render, sheet_table

from .common import package, rels

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    + "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in (1, 2, 3)
    )
    + '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)

WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    '<sheet name="Voices" sheetId="1" r:id="rId1"/><sheet name="Empty" sheetId="2" r:id="rId2"/>'
    '<sheet name="Runs" sheetId="3" r:id="rId3"/></sheets></workbook>'
)

# Style 1 is a date (built-in format 14), style 2 a date and time (22).
STYLES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/>'
    '<xf numFmtId="22" applyNumberFormat="1"/></cellXfs></styleSheet>'
)


def sheet(rows: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )


def text(r: str, t: str) -> str:
    return f'<c r="{r}" t="inlineStr"><is><t>{t}</t></is></c>'


def num(r: str, v: str, style: int) -> str:
    return f'<c r="{r}" s="{style}"><v>{v}</v></c>'


def workbook(voices: str, runs: str) -> bytes:
    return package(
        [
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", rels([("rId1", "officeDocument", "xl/workbook.xml")])),
            ("xl/workbook.xml", WORKBOOK),
            (
                "xl/_rels/workbook.xml.rels",
                rels(
                    [
                        ("rId1", "worksheet", "worksheets/sheet1.xml"),
                        ("rId2", "worksheet", "worksheets/sheet2.xml"),
                        ("rId3", "worksheet", "worksheets/sheet3.xml"),
                        ("rId4", "styles", "styles.xml"),
                    ]
                ),
            ),
            ("xl/styles.xml", STYLES),
            ("xl/worksheets/sheet1.xml", sheet(voices)),
            ("xl/worksheets/sheet2.xml", sheet("")),
            ("xl/worksheets/sheet3.xml", sheet(runs)),
        ]
    )


def test_sheets_become_tables_with_readable_values() -> None:
    voices = (
        f'<row r="2">{text("B2", "Voice")}{text("C2", "Score")}'
        f"{text('D2', 'Recorded')}{text('E2', 'Note')}</row>"
        f'<row r="3">{text("B3", "af_bella")}{num("C3", "3.0000000000000004", 0)}'
        f"{num('D3', '45356', 1)}{text('E3', 'fast | clear')}</row>"
        f'<row r="4">{text("B4", "am_adam")}{num("C4", "0.30000000000000004", 0)}'
        f'{num("D4", "45356.5", 2)}<c r="E4" s="0"/></row>'
        '<row r="5"><c r="B5" s="0"/></row>'
    )
    runs = (
        f'<row r="1">{text("A1", "Run")}{text("B1", "Count")}</row>'
        f'<row r="2">{num("A2", "1", 0)}{num("B2", "1250", 0)}</row>'
    )
    md = to_markdown(workbook(voices, runs), InputKind.EXCEL)
    assert md == (
        "## Voices\n\n"
        "| Voice | Score | Recorded | Note |\n"
        "| --- | --- | --- | --- |\n"
        "| af_bella | 3 | 2024-03-05 | fast \\| clear |\n"
        "| am_adam | 0.3 | 2024-03-05 12:00 |  |\n\n"
        "## Runs\n\n"
        "| Run | Count |\n| --- | --- |\n| 1 | 1250 |\n"
    )


def test_a_long_sheet_says_how_much_was_left_out() -> None:
    rows = f'<row r="1">{text("A1", "n")}</row>' + "".join(
        f'<row r="{i}">{num(f"A{i}", str(i), 0)}</row>' for i in range(2, 2_012)
    )
    md = to_markdown(workbook(rows, ""), InputKind.EXCEL)
    assert "| 2001 |" in md, md
    assert "| 2002 |" not in md
    assert md.rstrip().endswith("10 more rows were left out of this sheet.")


def test_numbers_read_as_typed() -> None:
    assert number(3.0) == "3"
    assert number(0.1 + 0.2) == "0.3"
    assert number(3.0000000001) == "3.0000000001"
    assert number(-12.5) == "-12.5"
    assert number(1e20) == "100000000000000000000"


def test_dates_are_iso() -> None:
    assert render(from_excel(45_356.0)) == "2024-03-05"
    assert render(from_excel(45_356.5)) == "2024-03-05 12:00"
    assert render(from_excel(0.75)) == "18:00:00"


def test_long_sheets_are_capped() -> None:
    rows = [["n"], *([str(i)] for i in range(MAX_ROWS + 5))]
    table = sheet_table(rows)
    assert table is not None
    assert table.endswith("5 more rows were left out of this sheet.")
