//! PDF to Markdown, through `pdf_oxide`'s text extraction.
//!
//! A PDF has no paragraphs, only lines placed on a page. `pdf_oxide` returns
//! each page's lines in reading order with a blank line where it sees a gap
//! between blocks; this module joins the lines of each block back into one
//! paragraph, since a paragraph split at every line end searches and reads
//! badly. Words are never changed, with one exception: a word the typesetter
//! hyphenated across a line end is put back together, and only when the
//! document itself shows that it is one word (see [`join_hyphenated`]).
//!
//! A page with no text layer, such as a scan, contributes nothing at all, not
//! even a page heading, so a scanned PDF comes back nearly empty and callers
//! can tell it apart by its length.

use std::collections::HashSet;

use pdf_oxide::document::PdfDocument;

use crate::{Error, InputKind};

const KIND: InputKind = InputKind::Pdf;

/// A line shorter than this share of the page's usual line length, ending a
/// sentence, is taken as the last line of its paragraph.
const SHORT_LINE: f64 = 0.7;

pub(crate) fn convert(bytes: &[u8]) -> Result<String, Error> {
    if !bytes.starts_with(b"%PDF")
        && !bytes[..bytes.len().min(1024)]
            .windows(5)
            .any(|w| w == b"%PDF-")
    {
        return Err(Error::NotValid(KIND));
    }
    let doc = PdfDocument::from_bytes(bytes.to_vec()).map_err(|_| Error::NotValid(KIND))?;
    let count = doc.page_count().map_err(|_| Error::Damaged(KIND))?;

    let mut pages = Vec::with_capacity(count);
    let mut failed = 0;
    for index in 0..count {
        match doc.extract_text(index) {
            Ok(text) => pages.push(text),
            // One unreadable page should not cost the rest of the document.
            Err(_) => failed += 1,
        }
    }
    if count > 0 && failed == count {
        return Err(if doc.is_encrypted() {
            Error::Encrypted(KIND)
        } else {
            Error::Damaged(KIND)
        });
    }

    let vocabulary = vocabulary(&pages);
    let paragraphs: Vec<String> = pages
        .iter()
        .flat_map(|page| paragraphs(page, &vocabulary))
        .collect();
    Ok(paragraphs.join("\n\n"))
}

/// Every word in the document, lower-cased, with its surrounding punctuation
/// removed. Used as evidence when deciding whether a hyphen at a line end
/// belongs to the word.
fn vocabulary(pages: &[String]) -> HashSet<String> {
    pages
        .iter()
        .flat_map(|p| p.split_whitespace())
        .map(word_core)
        .filter(|w| !w.is_empty())
        .collect()
}

fn word_core(word: &str) -> String {
    word.trim_matches(|c: char| !c.is_alphanumeric() && c != '-')
        .to_lowercase()
}

/// A page's text as paragraphs, each the lines of one block joined.
fn paragraphs(page: &str, vocabulary: &HashSet<String>) -> Vec<String> {
    let lines: Vec<&str> = page.lines().map(str::trim).collect();
    let usual = usual_length(&lines);

    let mut out = Vec::new();
    let mut current = String::new();
    // Lines joined into `current` so far.
    let mut joined = 0;
    let flush = |current: &mut String, out: &mut Vec<String>| {
        if !current.is_empty() {
            out.push(std::mem::take(current));
        }
    };
    for (i, line) in lines.iter().enumerate() {
        if current.is_empty() {
            joined = 0;
        }
        if line.is_empty() {
            flush(&mut current, &mut out);
            continue;
        }
        let line = crate::markdown::tidy_line(line);
        // A list item or a laid-out row starts a line of its own.
        if starts_item(&line) || looks_tabular(lines[i]) {
            flush(&mut current, &mut out);
        }
        // A short first line with no closing punctuation, followed by a line
        // that starts a sentence, is a title or label on a line of its own.
        if !current.is_empty()
            && joined == 1
            && is_label(&current, usual)
            && line.chars().next().is_some_and(char::is_uppercase)
        {
            flush(&mut current, &mut out);
        }
        if current.is_empty() {
            current = line.clone();
        } else {
            join_hyphenated(&mut current, &line, vocabulary);
        }
        joined += 1;
        let ends_paragraph = looks_tabular(lines[i])
            || (ends_sentence(&line) && (line.chars().count() as f64) < usual * SHORT_LINE);
        if ends_paragraph {
            flush(&mut current, &mut out);
        }
    }
    flush(&mut current, &mut out);
    out
}

/// The length of a typical full line on the page: the upper quartile, so a
/// page of short lines (a list, a title page) is not measured against prose.
fn usual_length(lines: &[&str]) -> f64 {
    let mut lengths: Vec<usize> = lines
        .iter()
        .filter(|l| !l.is_empty())
        .map(|l| l.chars().count())
        .collect();
    if lengths.is_empty() {
        return 0.0;
    }
    lengths.sort_unstable();
    lengths[lengths.len() * 3 / 4] as f64
}

/// A line that reads as a heading or label: short, and not ending like a
/// sentence or a clause.
fn is_label(line: &str, usual: f64) -> bool {
    (line.chars().count() as f64) < usual * SHORT_LINE
        && !line.ends_with(['.', ',', ';', ':', '!', '?', '-'])
}

fn ends_sentence(line: &str) -> bool {
    line.trim_end_matches(['"', '\'', ')', '”', '’', ']'])
        .ends_with(['.', '!', '?', ':'])
}

/// Bullets and numbered items, as a PDF writes them: the marker is text.
fn starts_item(line: &str) -> bool {
    if line.starts_with(['•', '◦', '▪', '‣', '●', '○', '■', '–', '—'])
        || line.starts_with("- ")
        || line.starts_with("* ")
    {
        return true;
    }
    let digits = line.chars().take_while(char::is_ascii_digit).count();
    (1..=3).contains(&digits) && {
        let rest = &line[digits..];
        rest.starts_with(". ") || rest.starts_with(") ")
    }
}

/// `pdf_oxide` writes table rows with the columns padded apart by runs of
/// spaces. Such a line is a row, and joining it to its neighbours would mix
/// cells of different rows.
fn looks_tabular(raw: &str) -> bool {
    raw.trim().contains("   ")
}

/// Append the next line to a paragraph. Lines are joined with a space, except
/// where the previous line ends in a hyphen inside a word:
///
/// - a soft hyphen (U+00AD) only ever marks a break point, so it goes;
/// - if the joined word appears elsewhere in the document without the hyphen
///   ("replication"), the hyphen was the typesetter's and goes;
/// - otherwise the hyphen stays and the halves are joined without a space
///   (a hyphen right after a letter is never followed by a space in prose),
///   which keeps a compound such as "well-known" intact and at worst leaves a
///   rare split word as "replica-tion", with no text lost.
fn join_hyphenated(current: &mut String, next: &str, vocabulary: &HashSet<String>) {
    if let Some(stem) = current.strip_suffix('\u{ad}') {
        let stem_len = stem.len();
        current.truncate(stem_len);
        current.push_str(next);
        return;
    }
    let hyphenated = current.ends_with('-')
        && current
            .chars()
            .rev()
            .nth(1)
            .is_some_and(char::is_alphabetic)
        && next.chars().next().is_some_and(char::is_alphanumeric);
    if !hyphenated {
        current.push(' ');
        current.push_str(next);
        return;
    }
    let head = current
        .rsplit(char::is_whitespace)
        .next()
        .unwrap_or_default()
        .trim_end_matches('-');
    let tail = next.split(char::is_whitespace).next().unwrap_or_default();
    let joined = word_core(&format!("{head}{tail}"));
    let continues_word = next.chars().next().is_some_and(char::is_lowercase);
    if continues_word && vocabulary.contains(&joined) {
        current.pop();
    }
    current.push_str(next);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vocab(words: &[&str]) -> HashSet<String> {
        words.iter().map(|w| w.to_string()).collect()
    }

    #[test]
    fn hyphens_go_only_with_evidence() {
        let v = vocab(&["replication"]);
        let mut s = "DNA replica-".to_string();
        join_hyphenated(&mut s, "tion starts", &v);
        assert_eq!(s, "DNA replication starts");

        let mut s = "a well-".to_string();
        join_hyphenated(&mut s, "known fact", &v);
        assert_eq!(s, "a well-known fact");

        let mut s = "Barge-".to_string();
        join_hyphenated(&mut s, "In", &v);
        assert_eq!(s, "Barge-In");
    }

    #[test]
    fn lines_join_into_paragraphs() {
        let page = "The first line of a paragraph that wraps\nonto a second line and ends here.\n\nA new paragraph.";
        let v = vocab(&[]);
        assert_eq!(
            paragraphs(page, &v),
            vec![
                "The first line of a paragraph that wraps onto a second line and ends here.",
                "A new paragraph."
            ]
        );
    }

    #[test]
    fn list_items_stay_apart() {
        let page = "Steps to follow when the text is long enough:\n1. first\n2. second";
        let out = paragraphs(page, &vocab(&[]));
        assert_eq!(out.len(), 3);
    }
}
