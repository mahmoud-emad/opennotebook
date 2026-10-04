//! Building Office files in memory, part by part, so each test states exactly
//! the XML it is about.

use std::io::{Cursor, Write};

use zip::write::SimpleFileOptions;

/// A zip holding the given parts, deflated as Office writes them.
pub fn package(parts: &[(&str, &str)]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, body) in parts {
        zip.start_file(*name, SimpleFileOptions::default()).unwrap();
        zip.write_all(body.as_bytes()).unwrap();
    }
    zip.finish().unwrap().into_inner()
}

pub const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

/// A `.rels` part from `(id, type suffix, target)`; a target starting with
/// `http` is marked external.
pub fn rels(entries: &[(&str, &str, &str)]) -> String {
    let mut s = String::from(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">"#,
    );
    for (id, kind, target) in entries {
        let mode = if target.starts_with("http") {
            r#" TargetMode="External""#
        } else {
            ""
        };
        s.push_str(&format!(
            r#"<Relationship Id="{id}" Type="{REL}/{kind}" Target="{target}"{mode}/>"#
        ));
    }
    s.push_str("</Relationships>");
    s
}
