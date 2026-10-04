//! PowerPoint decks built part by part.

mod common;

use common::{package, rels};
use opennotebook_convert::{InputKind, to_markdown};

const NS: &str = r#"xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main""#;

/// A shape; `ph` is the placeholder element, or empty for a free text box.
fn shape(ph: &str, paragraphs: &str) -> String {
    format!(
        r#"<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>{paragraphs}</p:txBody></p:sp>"#
    )
}

fn para(level: u32, text: &str) -> String {
    format!(r#"<a:p><a:pPr lvl="{level}"/><a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r></a:p>"#)
}

fn slide(shapes: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><p:sld {NS}><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>{shapes}</p:spTree></p:cSld></p:sld>"#
    )
}

fn title(text: &str) -> String {
    shape(r#"<p:ph type="title"/>"#, &para(0, text))
}

fn table() -> String {
    let cell = |t: &str| {
        format!("<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{t}</a:t></a:r></a:p></a:txBody></a:tc>")
    };
    format!(
        r#"<p:graphicFrame><p:nvGraphicFramePr/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tr>{}{}</a:tr><a:tr>{}<a:tc hMerge="1"><a:txBody><a:bodyPr/><a:p/></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"#,
        cell("Voice"),
        cell("Speed"),
        cell("af_bella | slow"),
    )
}

/// A deck whose slide parts are named against their order: `slide1.xml` is
/// shown second.
fn deck() -> Vec<u8> {
    let presentation = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><p:presentation {NS}><p:sldIdLst><p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>"#
    );
    let first = slide(&format!(
        "{}{}{}",
        title("Opening"),
        shape(
            r#"<p:ph idx="1"/>"#,
            &format!(
                "{}{}{}",
                para(0, "Narration"),
                para(1, "spoken script per slide"),
                para(0, "Barge-in")
            )
        ),
        shape(
            "",
            r#"<a:p><a:r><a:rPr b="1"/><a:t>Read</a:t></a:r><a:r><a:t xml:space="preserve"> the </a:t></a:r><a:r><a:rPr><a:hlinkClick r:id="rId7"/></a:rPr><a:t>guide</a:t></a:r></a:p>"#
        ),
    ));
    let second = slide(&format!(
        "{}{}{}",
        title("Voices"),
        table(),
        shape(r#"<p:ph type="sldNum" idx="12"/>"#, &para(0, "2")),
    ));
    let notes = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><p:notes {NS}><p:cSld><p:spTree>{}{}</p:spTree></p:cSld></p:notes>"#,
        shape(r#"<p:ph type="sldImg"/>"#, ""),
        shape(
            r#"<p:ph type="body" idx="1"/>"#,
            &format!(
                "{}{}",
                para(0, "Pause after the title."),
                para(0, "Then ask a question.")
            )
        ),
    );
    package(&[
        (
            "_rels/.rels",
            &rels(&[("rId1", "officeDocument", "ppt/presentation.xml")]),
        ),
        ("ppt/presentation.xml", &presentation),
        (
            "ppt/_rels/presentation.xml.rels",
            &rels(&[
                ("rId2", "slide", "slides/slide1.xml"),
                ("rId3", "slide", "slides/slide2.xml"),
            ]),
        ),
        ("ppt/slides/slide1.xml", &second),
        ("ppt/slides/slide2.xml", &first),
        (
            "ppt/slides/_rels/slide2.xml.rels",
            &rels(&[
                ("rId5", "notesSlide", "../notesSlides/notesSlide1.xml"),
                ("rId7", "hyperlink", "https://example.com/guide"),
            ]),
        ),
        ("ppt/notesSlides/notesSlide1.xml", &notes),
    ])
}

#[test]
fn slides_follow_the_presentation_order() {
    let md = to_markdown(&deck(), InputKind::Powerpoint).unwrap();
    let opening = md.find("## Slide 1: Opening").expect(&md);
    let voices = md.find("## Slide 2: Voices").expect(&md);
    assert!(opening < voices, "{md}");
}

#[test]
fn a_slide_keeps_bullets_tables_links_and_notes() {
    let md = to_markdown(&deck(), InputKind::Powerpoint).unwrap();
    assert_eq!(
        md,
        "## Slide 1: Opening\n\n\
         - Narration\n    - spoken script per slide\n- Barge-in\n\n\
         **Read** the [guide](https://example.com/guide)\n\n\
         Notes:\n\nPause after the title.\n\nThen ask a question.\n\n\
         ## Slide 2: Voices\n\n\
         | Voice | Speed |\n| --- | --- |\n| af_bella \\| slow |  |\n"
    );
}

#[test]
fn a_deck_without_a_presentation_part_is_refused() {
    let bytes = package(&[("ppt/slides/slide1.xml", &slide(&title("Lost")))]);
    let err = to_markdown(&bytes, InputKind::Powerpoint).unwrap_err();
    assert_eq!(
        err.to_string(),
        "this file is not a valid PowerPoint presentation"
    );
}
