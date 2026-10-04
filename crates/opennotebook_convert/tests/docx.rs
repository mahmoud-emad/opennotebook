//! Word documents built part by part, checking each structure the converter
//! promises to keep.

mod common;

use common::{package, rels};
use opennotebook_convert::{InputKind, to_markdown};

const W: &str = r#"xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" xmlns:v="urn:schemas-microsoft-com:vml""#;

const STYLES: &str = r#"<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Titre1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>
    <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
  <w:style w:type="character" w:styleId="Strong"><w:name w:val="Strong"/><w:rPr><w:b/></w:rPr></w:style>
</w:styles>"#;

const NUMBERING: &str = r#"<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="10">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="20">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>
</w:numbering>"#;

const FOOTNOTES: &str = r#"<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Measured on the reference laptop.</w:t></w:r></w:p></w:footnote>
</w:footnotes>"#;

fn p(inner: &str) -> String {
    format!("<w:p>{inner}</w:p>")
}

fn styled(style: &str, text: &str) -> String {
    format!(r#"<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"#)
}

fn item(num: u32, level: u32, text: &str) -> String {
    format!(
        r#"<w:p><w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"#
    )
}

fn run(text: &str) -> String {
    format!(r#"<w:r><w:t xml:space="preserve">{text}</w:t></w:r>"#)
}

fn docx(body: &str) -> Vec<u8> {
    docx_with(body, &[])
}

/// A document with extra parts, e.g. footnotes. Every note in a document is
/// written out, referenced or not, so notes are only added where a test
/// is about them.
fn docx_with(body: &str, extra: &[(&str, &str)]) -> Vec<u8> {
    let document = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}<w:sectPr/></w:body></w:document>"#
    );
    let mut parts = vec![
        (
            "_rels/.rels",
            rels(&[("rId1", "officeDocument", "word/document.xml")]),
        ),
        ("word/document.xml", document),
        (
            "word/_rels/document.xml.rels",
            rels(&[
                ("rId1", "styles", "styles.xml"),
                ("rId2", "numbering", "numbering.xml"),
                ("rId3", "footnotes", "footnotes.xml"),
                ("rId9", "hyperlink", "https://example.com/docs?a=1"),
            ]),
        ),
        ("word/styles.xml", STYLES.to_string()),
        ("word/numbering.xml", NUMBERING.to_string()),
    ];
    parts.extend(extra.iter().map(|(n, b)| (*n, b.to_string())));
    let parts: Vec<(&str, &str)> = parts.iter().map(|(n, b)| (*n, b.as_str())).collect();
    package(&parts)
}

fn convert(body: &str) -> String {
    to_markdown(&docx(body), InputKind::Word).unwrap()
}

#[test]
fn headings_follow_styles_by_name_not_id() {
    let md = convert(&format!(
        "{}{}{}{}",
        styled("Title", "The Plan"),
        // A localised id, recognised through the style's name.
        styled("Titre1", "Background"),
        styled("Heading2", "Detail"),
        p(&run("Body text."))
    ));
    assert_eq!(
        md,
        "# The Plan\n\n# Background\n\n## Detail\n\nBody text.\n"
    );
}

#[test]
fn emphasis_links_breaks_and_tabs() {
    let md = convert(&p(&format!(
        r#"{}<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">bold </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>italic</w:t></w:r><w:r><w:rPr><w:rStyle w:val="Strong"/></w:rPr><w:t xml:space="preserve"> strong</w:t></w:r>{}<w:hyperlink r:id="rId9"><w:r><w:t>the docs</w:t></w:r></w:hyperlink><w:r><w:br/><w:t>next</w:t><w:tab/><w:t>line</w:t></w:r>"#,
        run("Plain "),
        run(", see "),
    )));
    assert_eq!(
        md,
        "Plain **bold** *italic* **strong**, see [the docs](https://example.com/docs?a=1)  \nnext line\n"
    );
}

#[test]
fn lists_nest_and_tell_bullets_from_numbers() {
    let md = convert(&format!(
        "{}{}{}{}{}{}{}",
        p(&run("Intro.")),
        item(1, 0, "apples"),
        item(1, 1, "green"),
        item(1, 0, "pears"),
        item(2, 0, "first"),
        item(2, 1, "detail"),
        item(2, 0, "second"),
    ));
    assert_eq!(
        md,
        "Intro.\n\n- apples\n    - green\n- pears\n1. first\n    1. detail\n2. second\n"
    );
}

#[test]
fn a_list_style_makes_list_items() {
    let md = convert(&styled("ListBullet", "styled item"));
    assert_eq!(md, "- styled item\n");
}

#[test]
fn tables_keep_merges_empty_cells_and_pipes() {
    let cell = |props: &str, text: &str| {
        format!(
            r#"<w:tc><w:tcPr>{props}</w:tcPr><w:p>{}</w:p></w:tc>"#,
            if text.is_empty() {
                String::new()
            } else {
                run(text)
            }
        )
    };
    let table = format!(
        "<w:tbl><w:tr>{}{}{}</w:tr><w:tr>{}{}</w:tr><w:tr>{}{}{}</w:tr></w:tbl>",
        cell("", "Name"),
        cell("", "Choice"),
        cell("", "Notes"),
        cell(r#"<w:vMerge w:val="restart"/>"#, "Ada"),
        cell(r#"<w:gridSpan w:val="2"/>"#, "a | b"),
        cell("<w:vMerge/>", ""),
        cell("", ""),
        cell("", "last"),
    );
    let md = convert(&table);
    assert_eq!(
        md,
        "| Name | Choice | Notes |\n| --- | --- | --- |\n| Ada | a \\| b |  |\n|  |  | last |\n"
    );
}

#[test]
fn footnotes_are_appended_and_referenced() {
    let body = p(&format!(
        r#"{}<w:r><w:footnoteReference w:id="1"/></w:r>"#,
        run("It runs at 40 ms.")
    ));
    let bytes = docx_with(&body, &[("word/footnotes.xml", FOOTNOTES)]);
    let md = to_markdown(&bytes, InputKind::Word).unwrap();
    assert_eq!(
        md,
        "It runs at 40 ms.[^1]\n\n[^1]: Measured on the reference laptop.\n"
    );
}

#[test]
fn text_boxes_are_read_once() {
    // Word writes a text box twice: a DrawingML shape and a VML fallback.
    let box_text = |t: &str| format!("<w:txbxContent>{}</w:txbxContent>", p(&run(t)));
    let body = p(&format!(
        r#"{}<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>{}</wps:txbx></wps:wsp></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>{}</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"#,
        run("Anchor paragraph."),
        box_text("Inside the box."),
        box_text("Inside the box."),
    ));
    let md = convert(&body);
    assert_eq!(md, "Anchor paragraph.\n\nInside the box.\n");
}

#[test]
fn tracked_changes_read_as_accepted() {
    let md = convert(&p(&format!(
        r#"{}<w:del><w:r><w:delText>old </w:delText></w:r></w:del><w:ins><w:r><w:t xml:space="preserve">new </w:t></w:r></w:ins>{}"#,
        run("The "),
        run("wording.")
    )));
    assert_eq!(md, "The new wording.\n");
}

#[test]
fn fields_keep_their_result() {
    let md = convert(&p(&format!(
        r#"{}<w:fldSimple w:instr=" DATE "><w:r><w:t>4 October</w:t></w:r></w:fldSimple><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.org" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>{}<w:r><w:fldChar w:fldCharType="end"/></w:r>"#,
        run("Dated "),
        run(" example")
    )));
    assert_eq!(md, "Dated 4 October [example](https://example.org)\n");
}

#[test]
fn markdown_characters_are_not_escaped() {
    let md = convert(&p(&run(
        "voices af_bella and am_adam cost 5 * 3 &amp; more",
    )));
    assert_eq!(md, "voices af_bella and am_adam cost 5 * 3 & more\n");
}

#[test]
fn a_zip_without_a_document_is_not_a_word_file() {
    let bytes = package(&[("hello.txt", "hi")]);
    let err = to_markdown(&bytes, InputKind::Word).unwrap_err();
    assert_eq!(err.to_string(), "this file is not a valid Word document");
}
