//! Bad input is an error a person can read, never a panic.

mod common;

use std::io::{Cursor, Write};

use opennotebook_convert::{Error, InputKind, to_markdown};
use zip::write::SimpleFileOptions;

const KINDS: [InputKind; 4] = [
    InputKind::Word,
    InputKind::Excel,
    InputKind::Powerpoint,
    InputKind::Pdf,
];

fn assert_readable(err: &Error) {
    let text = err.to_string();
    assert!(!text.contains('{'), "{text}");
    assert!(!text.is_empty());
    assert!(text.starts_with("this "), "{text}");
}

#[test]
fn garbage_is_refused_for_every_kind() {
    let inputs: [&[u8]; 4] = [
        b"",
        b"not a document at all",
        b"PK\x03\x04 truncated zip header",
        b"%PDF-1.7\n1 0 obj << /Type /Catalog >> broken",
    ];
    for kind in KINDS {
        for bytes in inputs {
            match to_markdown(bytes, kind) {
                Err(err) => assert_readable(&err),
                Ok(md) => panic!("{kind:?} accepted {bytes:?} as {md:?}"),
            }
        }
    }
}

#[test]
fn a_damaged_part_is_refused() {
    let bytes = common::package(&[
        (
            "_rels/.rels",
            &common::rels(&[("rId1", "officeDocument", "word/document.xml")]),
        ),
        ("word/document.xml", "<w:document><w:body><w:p>"),
    ]);
    let err = to_markdown(&bytes, InputKind::Word).unwrap_err();
    assert_readable(&err);
    assert_eq!(
        err.to_string(),
        "this Word document is damaged and its text could not be read"
    );
}

#[test]
fn a_password_protected_file_says_so() {
    // Encrypted Office files are OLE compound files, not zips.
    let mut bytes = vec![0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1];
    bytes.extend_from_slice(&[0; 512]);
    for kind in [InputKind::Word, InputKind::Excel, InputKind::Powerpoint] {
        let err = to_markdown(&bytes, kind).unwrap_err();
        assert!(err.to_string().contains("password-protected"), "{err}");
    }
}

#[test]
fn a_zip_bomb_stops_at_the_budget() {
    // 210 MB of zeros deflates to a few hundred kilobytes.
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    zip.start_file("word/document.xml", SimpleFileOptions::default())
        .unwrap();
    let chunk = vec![b' '; 1024 * 1024];
    for _ in 0..210 {
        zip.write_all(&chunk).unwrap();
    }
    zip.start_file("_rels/.rels", SimpleFileOptions::default())
        .unwrap();
    zip.write_all(common::rels(&[("rId1", "officeDocument", "word/document.xml")]).as_bytes())
        .unwrap();
    let bytes = zip.finish().unwrap().into_inner();
    assert!(bytes.len() < 2 * 1024 * 1024);

    for kind in [InputKind::Word, InputKind::Excel] {
        let err = to_markdown(&bytes, kind).unwrap_err();
        assert_readable(&err);
        assert!(err.to_string().contains("too large"), "{kind:?}: {err}");
    }
}
