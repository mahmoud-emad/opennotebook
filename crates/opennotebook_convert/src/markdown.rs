//! Markdown output shared by the format readers: inline runs with emphasis
//! and links, block layout, and GFM tables.
//!
//! Text is written as it is, without backslash-escaping Markdown characters.
//! An escaped `af\_bella` renders the same but no longer matches the source
//! byte for byte, and the stored text is what grounding searches. The one
//! exception is a `|` inside a table cell, which would otherwise split the
//! cell and lose the table.

/// A stretch of text with one formatting.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct Span {
    pub text: String,
    pub bold: bool,
    pub italic: bool,
    pub link: Option<String>,
}

/// A hard line break inside a paragraph. Kept as a plain newline while spans
/// are collected and turned into Markdown's two-space break when rendered,
/// or into `<br>` inside a table cell.
pub(crate) const LINE_BREAK: char = '\n';

/// Render spans as inline Markdown.
pub(crate) fn inline(spans: &[Span]) -> String {
    let mut out = String::new();
    let mut i = 0;
    while i < spans.len() {
        match &spans[i].link {
            Some(url) => {
                // A link spans every consecutive run that points at the same
                // target, so a link whose words are partly bold stays one link.
                let mut j = i;
                while j < spans.len() && spans[j].link.as_deref() == Some(url) {
                    j += 1;
                }
                let label = emphasised(&spans[i..j]);
                let label_trimmed = label.trim();
                if label_trimmed.is_empty() {
                    out.push_str(&label);
                } else {
                    let lead = &label[..label.len() - label.trim_start().len()];
                    let trail = &label[label.trim_end().len()..];
                    out.push_str(lead);
                    out.push('[');
                    out.push_str(label_trimmed);
                    out.push_str("](");
                    out.push_str(&link_target(url));
                    out.push(')');
                    out.push_str(trail);
                }
                i = j;
            }
            None => {
                let mut j = i;
                while j < spans.len() && spans[j].link.is_none() {
                    j += 1;
                }
                out.push_str(&emphasised(&spans[i..j]));
                i = j;
            }
        }
    }
    out
}

/// A URL written so it cannot end the link early: spaces and parentheses
/// are percent-encoded, which every Markdown reader turns back into the
/// same address.
fn link_target(url: &str) -> String {
    url.trim()
        .replace(' ', "%20")
        .replace('(', "%28")
        .replace(')', "%29")
}

/// Spans with bold and italic markers, merging neighbours of the same style
/// first so `**a****b**` comes out as `**ab**`.
fn emphasised(spans: &[Span]) -> String {
    let mut merged: Vec<(String, bool, bool)> = Vec::new();
    for s in spans {
        // Whitespace carries no visible emphasis; treating it as unstyled
        // keeps markers from landing next to a space, where they stop working.
        let (bold, italic) = if s.text.trim().is_empty() {
            match merged.last() {
                Some((_, b, i)) => (*b, *i),
                None => (false, false),
            }
        } else {
            (s.bold, s.italic)
        };
        match merged.last_mut() {
            Some((text, b, i)) if *b == bold && *i == italic => text.push_str(&s.text),
            _ => merged.push((s.text.clone(), bold, italic)),
        }
    }
    let mut out = String::new();
    for (text, bold, italic) in merged {
        let marker = match (bold, italic) {
            (true, true) => "***",
            (true, false) => "**",
            (false, true) => "*",
            (false, false) => "",
        };
        if marker.is_empty() {
            out.push_str(&text);
            continue;
        }
        // Emphasis applies line by line, since a marker cannot reach across a
        // line break, and only to the words: the surrounding spaces go
        // outside, where they do not stop the marker from closing.
        let lines: Vec<String> = text
            .split(LINE_BREAK)
            .map(|line| {
                let core = line.trim();
                if core.is_empty() {
                    return line.to_string();
                }
                let lead = &line[..line.len() - line.trim_start().len()];
                let trail = &line[line.trim_end().len()..];
                format!("{lead}{marker}{core}{marker}{trail}")
            })
            .collect();
        out.push_str(&lines.join(&LINE_BREAK.to_string()));
    }
    out
}

/// Plain text out of spans, ignoring formatting and links, for places where
/// Markdown markup does not belong (headings, slide titles).
pub(crate) fn plain(spans: &[Span]) -> String {
    spans.iter().map(|s| s.text.as_str()).collect()
}

/// Collapse whitespace the way a reader sees it: tabs and runs of spaces to
/// one space, kept line by line.
pub(crate) fn tidy_line(line: &str) -> String {
    line.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Rendered inline text made ready to stand as a paragraph: whitespace tidied
/// within each line, empty lines dropped, and the remaining lines joined with
/// Markdown hard breaks.
pub(crate) fn paragraph_text(text: &str) -> String {
    text.split(LINE_BREAK)
        .map(tidy_line)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("  \n")
}

/// A heading or title on one line: breaks become spaces.
pub(crate) fn one_line(text: &str) -> String {
    tidy_line(&text.replace(LINE_BREAK, " "))
}

/// The output's building blocks. Separating the kinds lets list items sit on
/// consecutive lines while everything else is set off by a blank line.
#[derive(Debug, Clone)]
pub(crate) enum Block {
    Heading(usize, String),
    Paragraph(String),
    Item {
        depth: usize,
        ordered: Option<u32>,
        text: String,
    },
    Table(Vec<Vec<String>>),
}

pub(crate) fn render(blocks: &[Block]) -> String {
    let mut out = String::new();
    let mut prev_item = false;
    // The list depth actually written for the previous item. A list that
    // jumps from level 0 to level 3 is written one level deeper at a time,
    // because indentation past the parent's content would turn into a code
    // block.
    let mut last_depth: Option<usize> = None;
    for block in blocks {
        let is_item = matches!(block, Block::Item { .. });
        if !out.is_empty() {
            out.push_str(if is_item && prev_item { "\n" } else { "\n\n" });
        }
        if !is_item {
            last_depth = None;
        }
        match block {
            Block::Heading(level, text) => {
                out.push_str(&"#".repeat((*level).clamp(1, 6)));
                out.push(' ');
                out.push_str(text);
            }
            Block::Paragraph(text) => out.push_str(text),
            Block::Item {
                depth,
                ordered,
                text,
            } => {
                let depth = match last_depth {
                    Some(prev) => (*depth).min(prev + 1),
                    None => 0,
                };
                last_depth = Some(depth);
                // Four spaces per level clears the content column of both
                // `- ` and `10. ` parents.
                out.push_str(&"    ".repeat(depth));
                match ordered {
                    Some(n) => out.push_str(&format!("{n}. ")),
                    None => out.push_str("- "),
                }
                // Continuation lines of an item are indented to stay in it.
                let indent = format!("\n{}  ", "    ".repeat(depth));
                out.push_str(&text.replace('\n', &indent));
            }
            Block::Table(rows) => out.push_str(&table(rows)),
        }
        prev_item = is_item;
    }
    out
}

/// One table cell's text: breaks become `<br>`, whitespace is tidied, and
/// pipes are escaped so they stay inside the cell.
pub(crate) fn cell(text: &str) -> String {
    text.split(LINE_BREAK)
        .map(tidy_line)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("<br>")
        .replace('|', "\\|")
}

/// A GFM table, first row as the header. Rows are padded to the widest row,
/// since GFM needs every row to have the header's columns, and trailing
/// columns that are empty in every row are dropped.
pub(crate) fn table(rows: &[Vec<String>]) -> String {
    let width = rows
        .iter()
        .map(|r| r.iter().rposition(|c| !c.is_empty()).map_or(0, |i| i + 1))
        .max()
        .unwrap_or(0)
        .max(1);
    let line = |row: &[String]| {
        let mut s = String::from("|");
        for i in 0..width {
            let c = row.get(i).map(String::as_str).unwrap_or("");
            s.push(' ');
            s.push_str(c);
            s.push_str(" |");
        }
        s
    };
    let mut out = String::new();
    let empty = Vec::new();
    out.push_str(&line(rows.first().unwrap_or(&empty)));
    out.push('\n');
    out.push('|');
    out.push_str(&" --- |".repeat(width));
    for row in rows.iter().skip(1) {
        out.push('\n');
        out.push_str(&line(row));
    }
    out
}

/// The last pass over a converter's output: at most one blank line in a row,
/// no trailing spaces except a hard break's two, and one final newline.
pub(crate) fn finish(md: &str) -> String {
    let mut out = String::with_capacity(md.len());
    let mut blank = 0;
    for line in md.lines() {
        let trimmed = line.trim_end();
        if trimmed.is_empty() {
            blank += 1;
            continue;
        }
        if !out.is_empty() {
            out.push_str(if blank > 0 { "\n\n" } else { "\n" });
        }
        blank = 0;
        out.push_str(trimmed);
        if line.ends_with("  ") {
            out.push_str("  ");
        }
    }
    if !out.is_empty() {
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn span(text: &str, bold: bool, italic: bool) -> Span {
        Span {
            text: text.into(),
            bold,
            italic,
            link: None,
        }
    }

    #[test]
    fn emphasis_keeps_spaces_outside_the_markers() {
        let s = [
            span("plain ", false, false),
            span("bold ", true, false),
            span("both", true, true),
        ];
        assert_eq!(inline(&s), "plain **bold** ***both***");
    }

    #[test]
    fn a_link_wraps_its_runs() {
        let mut a = span("the ", false, false);
        a.link = Some("https://example.com/a b".into());
        let mut b = span("site", true, false);
        b.link = a.link.clone();
        assert_eq!(inline(&[a, b]), "[the **site**](https://example.com/a%20b)");
    }

    #[test]
    fn deep_items_never_skip_levels() {
        let blocks = [
            Block::Item {
                depth: 0,
                ordered: None,
                text: "a".into(),
            },
            Block::Item {
                depth: 3,
                ordered: None,
                text: "b".into(),
            },
        ];
        assert_eq!(render(&blocks), "- a\n    - b");
    }

    #[test]
    fn tables_pad_rows() {
        let rows = vec![vec!["a".into(), "b".into()], vec!["c".into()]];
        assert_eq!(table(&rows), "| a | b |\n| --- | --- |\n| c |  |");
    }
}
