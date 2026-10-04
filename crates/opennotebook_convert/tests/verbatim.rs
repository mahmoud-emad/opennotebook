//! The converter's one promise: the document's sentences come out byte for
//! byte. The fixtures are a Word document and a PDF made from the same text.

use opennotebook_convert::{InputKind, to_markdown};

const SENTENCES: [&str; 3] = [
    "The narration engine attaches a spoken script to each slide.",
    "Barge-in cancels the running text-to-speech stream before answering.",
    "Kokoro exposes named voices such as af_bella and am_adam.",
];

fn assert_verbatim(md: &str) {
    for sentence in SENTENCES {
        assert!(md.contains(sentence), "missing {sentence:?} in:\n{md}");
    }
}

#[test]
fn word_text_is_verbatim() {
    let md = to_markdown(include_bytes!("fixtures/verbatim.docx"), InputKind::Word).unwrap();
    assert_verbatim(&md);
}

#[test]
fn pdf_text_is_verbatim() {
    let md = to_markdown(include_bytes!("fixtures/verbatim.pdf"), InputKind::Pdf).unwrap();
    assert_verbatim(&md);
}

#[test]
fn a_scan_is_not_an_error_and_has_no_filler() {
    // A PDF of page images: there is nothing to read, and the converter must
    // not invent page headings that would make it look like there was.
    let md = to_markdown(include_bytes!("fixtures/scanned.pdf"), InputKind::Pdf).unwrap();
    let visible = md.chars().filter(|c| !c.is_whitespace()).count();
    assert!(visible < 80, "{md:?}");
}

#[test]
fn extensions_name_kinds() {
    assert_eq!(InputKind::from_extension("DOCX"), Some(InputKind::Word));
    assert_eq!(InputKind::from_extension("xlsx"), Some(InputKind::Excel));
    assert_eq!(
        InputKind::from_extension("Pptx"),
        Some(InputKind::Powerpoint)
    );
    assert_eq!(InputKind::from_extension("pdf"), Some(InputKind::Pdf));
    assert_eq!(InputKind::from_extension("doc"), None);
    assert_eq!(InputKind::from_extension(""), None);
}
