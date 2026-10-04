//! Retrieval from the session's own collection.
//!
//! The script is written from what was ingested, not from what the model
//! already knows. Both kinds of material are read, because they see different
//! things: `search` returns passages of the sources, `qa_search` the questions
//! and answers extracted from them.

use opennotebook_memory::Memory;

use crate::error::ScriptError;

/// What the two searches returned for one query.
#[derive(Debug, Clone, Default)]
pub struct Grounding {
    /// Passages from the indexed documents.
    pub passages: Vec<String>,
    /// Extracted question and answer pairs.
    pub qa: Vec<(String, String)>,
}

impl Grounding {
    pub fn is_empty(&self) -> bool {
        self.passages.is_empty() && self.qa.is_empty()
    }

    /// Both as one block of context for a prompt, labelled so the model can
    /// tell a source passage from an extracted answer.
    pub fn as_context(&self) -> String {
        let mut out = String::new();
        for p in &self.passages {
            out.push_str("SOURCE: ");
            out.push_str(p.trim());
            out.push_str("\n\n");
        }
        // One line per pair, not `Q:` and `A:` lines: a small model copied
        // those labels straight into the narration ("Bella: Q: What command…").
        for (q, a) in &self.qa {
            out.push_str("FACT: ");
            out.push_str(a.trim());
            out.push_str(" (answers: ");
            out.push_str(q.trim());
            out.push_str(")\n\n");
        }
        out
    }
}

/// Read both for one query, each in its own rank order.
pub async fn retrieve(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    query: &str,
    top_k: i64,
) -> Result<Grounding, ScriptError> {
    let k = top_k.max(1) as usize;
    let found = memory
        .search(workspace, collection, query, k)
        .await
        .map_err(|source| ScriptError::Memory {
            call: "search",
            source,
        })?;
    let ranked = memory
        .qa_search(workspace, collection, query, k)
        .await
        .map_err(|source| ScriptError::Memory {
            call: "qa_search",
            source,
        })?;

    let documents: Vec<String> = found
        .into_iter()
        .map(|h| h.text)
        .filter(|t| !t.trim().is_empty())
        .collect();

    Ok(Grounding {
        passages: excerpts(&documents, query, EXCERPTS_PER_HIT * k),
        qa: ranked
            .into_iter()
            .map(|h| (h.pair.question, h.pair.answer))
            .collect(),
    })
}

/// Excerpts kept per requested hit: a slide asking for four hits gets eight
/// excerpts, about 7,000 characters; the outline, asking for twelve, gets
/// twenty-four.
const EXCERPTS_PER_HIT: usize = 2;

/// The size an excerpt grows to before a new one starts.
pub(crate) const EXCERPT_CHARS: usize = 900;

/// The parts of the retrieved documents that are about `query`.
///
/// A retrieved passage can still carry navigation and boilerplate from a web
/// page, and grounding that sends all of it makes a slide call expensive and
/// its narration vague, the relevant sentences buried in menus.
///
/// So each passage is cut into paragraph-sized pieces, pieces that look like
/// navigation are dropped, and the pieces sharing the most words with the query
/// are kept — at most `keep` in all, in document order. A document with
/// nothing that matches contributes nothing. When no piece matches at all, the
/// first pieces of the best-ranked document stand in, so a query phrased
/// differently from the source still grounds on something.
pub fn excerpts(documents: &[String], query: &str, keep: usize) -> Vec<String> {
    excerpts_from(documents, query, keep)
        .into_iter()
        .map(|(_, text)| text)
        .collect()
}

/// [`excerpts`], with the index of the document each piece came from, so an
/// answer can cite the source a passage was read in.
pub fn excerpts_from(documents: &[String], query: &str, keep: usize) -> Vec<(usize, String)> {
    let terms = terms(query);
    // (score, document rank, position, text)
    let mut pieces: Vec<(usize, usize, usize, String)> = Vec::new();
    for (rank, doc) in documents.iter().enumerate() {
        for (pos, piece) in pieces_of(doc).into_iter().enumerate() {
            let lower = piece.to_lowercase();
            let score = terms.iter().filter(|t| lower.contains(t.as_str())).count();
            pieces.push((score, rank, pos, piece));
        }
    }
    let best = pieces.iter().map(|p| p.0).max().unwrap_or(0);
    let mut chosen: Vec<(usize, usize, usize, String)> = if best == 0 {
        pieces.into_iter().filter(|p| p.1 == 0).take(keep).collect()
    } else {
        pieces.retain(|p| p.0 > 0);
        // Highest score first; earlier documents and earlier pieces win ties.
        pieces.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
        pieces.truncate(keep);
        pieces
    };
    chosen.sort_by(|a, b| a.1.cmp(&b.1).then(a.2.cmp(&b.2)));
    chosen.into_iter().map(|p| (p.1, p.3)).collect()
}

/// Every prose passage of every document, in order, with the index of the
/// document it came from. What a reader that cites the WHOLE of the sources
/// numbers, where [`excerpts_from`] keeps only the pieces about one question.
pub fn passages(documents: &[String]) -> Vec<(usize, String)> {
    documents
        .iter()
        .enumerate()
        .flat_map(|(i, d)| pieces_of(d).into_iter().map(move |p| (i, p)))
        .collect()
}

/// The words of a query worth matching: lowercased, three letters or more, and
/// not one of the words every sentence has.
pub fn terms(query: &str) -> Vec<String> {
    const COMMON: &[&str] = &[
        "the", "and", "for", "with", "how", "what", "are", "its", "from", "into", "that", "this",
        "why", "when", "does", "their", "your", "about",
    ];
    let mut out: Vec<String> = query
        .split(|c: char| !c.is_alphanumeric() && c != '_')
        .map(str::to_lowercase)
        .filter(|w| w.chars().count() >= 3 && !COMMON.contains(&w.as_str()))
        .collect();
    out.sort();
    out.dedup();
    out
}

/// A document as paragraph-sized pieces of prose, with navigation left out.
fn pieces_of(doc: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    for para in doc.split("\n\n").map(str::trim).filter(|p| !p.is_empty()) {
        if !is_prose(para) {
            continue;
        }
        if !current.is_empty() && current.chars().count() + para.chars().count() > EXCERPT_CHARS {
            out.push(std::mem::take(&mut current));
        }
        if !current.is_empty() {
            current.push_str("\n\n");
        }
        current.push_str(para);
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// Whether a paragraph reads as prose rather than as a menu.
///
/// A site's navigation converts to Markdown as runs of one- and two-word
/// paragraphs ("Courses", "Tutorials", "Interview Prep"). A heading is kept,
/// because it names what follows; a lone short line is not.
fn is_prose(para: &str) -> bool {
    if para.starts_with('#') {
        return true;
    }
    let words = para.split_whitespace().count();
    words >= 8
}

#[cfg(test)]
mod excerpt_tests {
    use super::*;

    #[test]
    fn a_menu_is_not_material() {
        let doc = "Courses\n\nTutorials\n\nInterview Prep\n\nThe scheduler picks the next \
                   process to run from the run queue each time a slice ends."
            .to_string();
        let got = excerpts(&[doc], "process scheduler", 6);
        assert_eq!(got.len(), 1);
        assert!(got[0].starts_with("The scheduler"));
    }

    #[test]
    fn only_the_pieces_about_the_topic_are_kept() {
        let para = |s: &str| {
            format!(
                "{s} {}",
                "filler words that pad the paragraph out ".repeat(30)
            )
        };
        let doc = [
            para("Zombie processes have exited but not been reaped."),
            para("The task_struct holds everything the kernel knows about a process."),
            para("Printers were once shared through spooling daemons."),
        ]
        .join("\n\n");
        let got = excerpts(&[doc], "The task_struct data", 1);
        assert_eq!(got.len(), 1);
        assert!(got[0].contains("task_struct holds"));
    }

    #[test]
    fn a_query_that_matches_nothing_still_grounds_on_the_best_document() {
        let docs = vec![
            "One two three four five six seven eight nine.".to_string(),
            "Ten eleven twelve thirteen fourteen fifteen sixteen seventeen.".to_string(),
        ];
        let got = excerpts(&docs, "quantum", 6);
        assert_eq!(got, vec![docs[0].clone()]);
    }
}
