//! Document bytes in, Markdown out, verbatim.
//!
//! This is the only place OpenNotebook turns an uploaded Word, PowerPoint,
//! Excel or PDF file into text, and everything it produces is the document's
//! own words. There is no model and no network anywhere in this crate, and no
//! step rewrites, summarises or "cleans up" a sentence: grounding depends on
//! the stored text being exactly what the source says, so the converter may
//! drop layout but never wording.
//!
//! The Office formats are read straight from their XML parts (see [`docx`],
//! [`pptx`]) because only the raw parts say where a text box, a footnote or a
//! tracked insertion sits; spreadsheets go through `calamine`, which already
//! knows cell types and date formats; PDFs go through `pdf_oxide`.
//!
//! Every reader treats its input as hostile. Corrupt bytes come back as an
//! [`Error`] a person can read, never a panic, and the zip containers are read
//! under a fixed decompression budget so a small upload cannot expand into
//! gigabytes.

mod docx;
mod markdown;
mod ooxml;
mod pdf;
mod pptx;
mod xlsx;
mod xml;

use std::fmt;

/// The document formats this crate reads, named the way a person would name
/// them rather than by extension, because the extension is only how a caller
/// usually learns the kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputKind {
    Word,
    Excel,
    Powerpoint,
    Pdf,
}

impl InputKind {
    /// The kind a file extension names, without its dot and in any case, or
    /// `None` for anything this crate cannot read. Only the XML-based Office
    /// formats are accepted: the older binary `.doc`, `.xls` and `.ppt` are a
    /// different format entirely and are refused here rather than failing
    /// later with a confusing message.
    pub fn from_extension(ext: &str) -> Option<Self> {
        match ext.trim_start_matches('.').to_ascii_lowercase().as_str() {
            "docx" => Some(Self::Word),
            "xlsx" => Some(Self::Excel),
            "pptx" => Some(Self::Powerpoint),
            "pdf" => Some(Self::Pdf),
            _ => None,
        }
    }

    /// How the kind reads in a sentence, e.g. "a Word document".
    fn described(self) -> &'static str {
        match self {
            Self::Word => "Word document",
            Self::Excel => "Excel workbook",
            Self::Powerpoint => "PowerPoint presentation",
            Self::Pdf => "PDF",
        }
    }
}

/// Why a document could not be converted.
///
/// The messages are written for the person who uploaded the file, since the
/// server shows them as they are: no crate name, no parser internals, nothing
/// a reader would have to decode. The underlying library error is deliberately
/// not carried, because its text is neither stable nor meant for people.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// The bytes are not this kind of file at all: not a zip, not a PDF, or a
    /// zip without the parts that make it a document.
    NotValid(InputKind),
    /// The container opened but a part needed for the text is broken.
    Damaged(InputKind),
    /// The file is password-protected, so its text cannot be read without
    /// the password.
    Encrypted(InputKind),
    /// The file would expand past the decompression budget, which is what a
    /// zip bomb looks like and also what no real document needs.
    TooLarge(InputKind),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotValid(kind) => write!(f, "this file is not a valid {}", kind.described()),
            Self::Damaged(kind) => write!(
                f,
                "this {} is damaged and its text could not be read",
                kind.described()
            ),
            Self::Encrypted(kind) => write!(
                f,
                "this {} is password-protected; remove the password and try again",
                kind.described()
            ),
            Self::TooLarge(kind) => write!(
                f,
                "this {} is too large to read once uncompressed",
                kind.described()
            ),
        }
    }
}

impl std::error::Error for Error {}

/// Convert a document to Markdown.
///
/// The output keeps every piece of the document's text in reading order, with
/// structure (headings, lists, tables, slides, sheets) expressed as Markdown.
/// A document that holds almost no text, such as a scanned PDF, still comes
/// back `Ok` with whatever little text there is: whether that is enough is
/// the caller's decision, and callers make it by length.
pub fn to_markdown(bytes: &[u8], kind: InputKind) -> Result<String, Error> {
    // The parsers underneath are third-party code fed untrusted bytes. A panic
    // in one of them must not take the server down with it, so it is turned
    // into the same "damaged" answer a parse error would give.
    let converted = std::panic::catch_unwind(|| match kind {
        InputKind::Word => docx::convert(bytes),
        InputKind::Powerpoint => pptx::convert(bytes),
        InputKind::Excel => xlsx::convert(bytes),
        InputKind::Pdf => pdf::convert(bytes),
    });
    match converted {
        Ok(result) => result.map(|md| markdown::finish(&md)),
        Err(_) => Err(Error::Damaged(kind)),
    }
}
