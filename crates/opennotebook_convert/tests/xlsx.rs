//! Excel workbooks built part by part.

mod common;

use common::{package, rels};
use opennotebook_convert::{InputKind, to_markdown};

const CONTENT_TYPES: &str = r#"<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>"#;

const WORKBOOK: &str = r#"<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Voices" sheetId="1" r:id="rId1"/><sheet name="Empty" sheetId="2" r:id="rId2"/><sheet name="Runs" sheetId="3" r:id="rId3"/></sheets></workbook>"#;

/// Style 1 is a date (built-in format 14), style 2 a date and time (22).
const STYLES: &str = r#"<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/><xf numFmtId="22" applyNumberFormat="1"/></cellXfs></styleSheet>"#;

fn sheet(rows: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>"#
    )
}

fn text(r: &str, t: &str) -> String {
    format!(r#"<c r="{r}" t="inlineStr"><is><t>{t}</t></is></c>"#)
}

fn num(r: &str, v: &str, style: u32) -> String {
    format!(r#"<c r="{r}" s="{style}"><v>{v}</v></c>"#)
}

fn workbook(voices: &str, runs: &str) -> Vec<u8> {
    package(&[
        ("[Content_Types].xml", CONTENT_TYPES),
        (
            "_rels/.rels",
            &rels(&[("rId1", "officeDocument", "xl/workbook.xml")]),
        ),
        ("xl/workbook.xml", WORKBOOK),
        (
            "xl/_rels/workbook.xml.rels",
            &rels(&[
                ("rId1", "worksheet", "worksheets/sheet1.xml"),
                ("rId2", "worksheet", "worksheets/sheet2.xml"),
                ("rId3", "worksheet", "worksheets/sheet3.xml"),
                ("rId4", "styles", "styles.xml"),
            ]),
        ),
        ("xl/styles.xml", STYLES),
        ("xl/worksheets/sheet1.xml", &sheet(voices)),
        ("xl/worksheets/sheet2.xml", &sheet("")),
        ("xl/worksheets/sheet3.xml", &sheet(runs)),
    ])
}

#[test]
fn sheets_become_tables_with_readable_values() {
    let voices = format!(
        r#"<row r="2">{}{}{}{}</row><row r="3">{}{}{}{}</row><row r="4">{}{}{}<c r="E4" s="0"/></row><row r="5"><c r="B5" s="0"/></row>"#,
        text("B2", "Voice"),
        text("C2", "Score"),
        text("D2", "Recorded"),
        text("E2", "Note"),
        text("B3", "af_bella"),
        num("C3", "3.0000000000000004", 0),
        num("D3", "45356", 1),
        text("E3", "fast | clear"),
        text("B4", "am_adam"),
        num("C4", "0.30000000000000004", 0),
        num("D4", "45356.5", 2),
    );
    let runs = format!(
        r#"<row r="1">{}{}</row><row r="2">{}{}</row>"#,
        text("A1", "Run"),
        text("B1", "Count"),
        num("A2", "1", 0),
        num("B2", "1250", 0),
    );
    let md = to_markdown(&workbook(&voices, &runs), InputKind::Excel).unwrap();
    assert_eq!(
        md,
        "## Voices\n\n\
         | Voice | Score | Recorded | Note |\n\
         | --- | --- | --- | --- |\n\
         | af_bella | 3 | 2024-03-05 | fast \\| clear |\n\
         | am_adam | 0.3 | 2024-03-05 12:00 |  |\n\n\
         ## Runs\n\n\
         | Run | Count |\n| --- | --- |\n| 1 | 1250 |\n"
    );
}

#[test]
fn a_long_sheet_says_how_much_was_left_out() {
    let mut rows = format!(r#"<row r="1">{}</row>"#, text("A1", "n"));
    for i in 2..=2_011 {
        rows.push_str(&format!(
            r#"<row r="{i}">{}</row>"#,
            num(&format!("A{i}"), &i.to_string(), 0)
        ));
    }
    let md = to_markdown(&workbook(&rows, ""), InputKind::Excel).unwrap();
    assert!(md.contains("| 2001 |"), "{md}");
    assert!(!md.contains("| 2002 |"));
    assert!(
        md.trim_end()
            .ends_with("10 more rows were left out of this sheet.")
    );
}
