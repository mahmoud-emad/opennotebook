//! A question answered from a draft's sources, with every claim cited.
//!
//! A draft has no indexed collection until it is built, so this reads the
//! staged files themselves: `grounding::excerpts_from` picks the passages that
//! share the most words with the question, they are numbered, and the model is
//! told to put the number of a passage after each claim it rests on. What comes
//! back is checked, not trusted: a number that names no passage is removed, and
//! the rest are renumbered in the order they are first used, so the reader
//! sees `[1]` before `[2]`. See `docs/mindmap-spec.md` section 5.

use crate::error::ScriptError;
use crate::grounding;
use crate::mindmap::NamedDoc;

/// Passages given to the model. About 7,000 characters of material.
pub const PASSAGES: usize = 8;

/// One cited passage, numbered as the answer's text numbers it.
#[derive(Debug, Clone, PartialEq)]
pub struct Cited {
    /// The number in the answer, from 1.
    pub n: u32,
    /// Index into the documents the answer was asked over.
    pub doc: usize,
    pub excerpt: String,
}

#[derive(Debug, Clone)]
pub struct Answer {
    /// Markdown, with `[n]` markers that match `cited`.
    pub text: String,
    /// Only the passages the text cites, in order of first use.
    pub cited: Vec<Cited>,
}

const STAGE: &str = "answer";

/// Answer `question` from `docs`.
pub async fn answer(docs: &[NamedDoc], question: &str) -> Result<Answer, ScriptError> {
    let texts: Vec<String> = docs.iter().map(|d| d.text.clone()).collect();
    let passages = grounding::excerpts_from(&texts, question, PASSAGES);
    if passages.is_empty() {
        return Err(ScriptError::Empty { stage: STAGE });
    }
    let model = opennotebook_session::settings::agent_model().await;
    let rule = opennotebook_session::settings::language_rule(
        &opennotebook_session::settings::language().await,
    );
    let raw = crate::generate::send(
        &model,
        &system_prompt(&rule),
        &user_prompt(docs, &passages, question),
        STAGE,
        true,
    )
    .await?;
    let (text, order) = renumber(&raw, passages.len());
    let cited = order
        .into_iter()
        .enumerate()
        .map(|(i, p)| Cited {
            n: i as u32 + 1,
            doc: passages[p].0,
            excerpt: passages[p].1.clone(),
        })
        .collect();
    Ok(Answer { text, cited })
}

fn system_prompt(language_rule: &str) -> String {
    let mut s = String::from(
        "You answer questions about a person's source material, using only the numbered \
         passages given.\n\
         Rules:\n\
         - After each claim, put the number of the passage it rests on in square brackets, \
           like [2], or [2][5] for more than one. Every claim gets one.\n\
         - Use only what the passages say. When they do not cover the question, say so in \
           one sentence and stop.\n\
         - A few short paragraphs, or a short list when the answer is a list. No heading, \
           no preamble, no closing summary.",
    );
    if !language_rule.is_empty() {
        s.push_str(&format!(
            "\n\n{language_rule} Keep the [n] markers exactly as described."
        ));
    }
    s
}

fn user_prompt(docs: &[NamedDoc], passages: &[(usize, String)], question: &str) -> String {
    let mut s = String::from("Passages:\n\n");
    for (i, (doc, text)) in passages.iter().enumerate() {
        let title = docs.get(*doc).map_or("", |d| d.title.trim());
        s.push_str(&format!("[{}] ({title}) {}\n\n", i + 1, text.trim()));
    }
    s.push_str(&format!("Question: {}", question.trim()));
    s
}

/// The reply with its citation markers checked and renumbered, and the passage
/// index (from 0) behind each new number, in order.
///
/// A marker is `[n]` or `[n, m]`. A number outside `1..=passages` is removed,
/// and a marker left with no number is removed whole, together with the space
/// before it. A list marker comes out as separate markers, `[1][2]`, which is
/// the form the chat draws as chips. `[text](url)` is a link, not a marker,
/// and is left alone.
pub fn renumber(reply: &str, passages: usize) -> (String, Vec<usize>) {
    let mut order: Vec<usize> = Vec::new();
    let mut out = String::with_capacity(reply.len());
    let mut rest = reply;
    while let Some(open) = rest.find('[') {
        let (before, from) = rest.split_at(open);
        out.push_str(before);
        let Some(close) = from.find(']') else {
            out.push_str(from);
            rest = "";
            break;
        };
        let inner = &from[1..close];
        let is_marker = !inner.trim().is_empty()
            && inner
                .split(',')
                .all(|p| !p.trim().is_empty() && p.trim().chars().all(|c| c.is_ascii_digit()))
            && !from[close + 1..].starts_with('(');
        if !is_marker {
            out.push('[');
            rest = &from[1..];
            continue;
        }
        let mut new = String::new();
        for n in inner
            .split(',')
            .filter_map(|p| p.trim().parse::<usize>().ok())
        {
            if n == 0 || n > passages {
                continue;
            }
            let at = match order.iter().position(|&p| p == n - 1) {
                Some(i) => i,
                None => {
                    order.push(n - 1);
                    order.len() - 1
                }
            };
            let marker = format!("[{}]", at + 1);
            if !new.contains(&marker) {
                new.push_str(&marker);
            }
        }
        if new.is_empty() {
            while out.ends_with(' ') {
                out.pop();
            }
        }
        out.push_str(&new);
        rest = &from[close + 1..];
    }
    out.push_str(rest);
    (out.trim().to_string(), order)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markers_are_renumbered_in_order_of_first_use() {
        let (t, order) = renumber("Latency is 200 ms [5]. It streams [2]. Again [5].", 8);
        assert_eq!(t, "Latency is 200 ms [1]. It streams [2]. Again [1].");
        assert_eq!(order, [4, 1]);
    }

    #[test]
    fn a_number_past_the_passages_is_removed_with_its_space() {
        let (t, order) = renumber("A claim [9]. Another [1].", 8);
        assert_eq!(t, "A claim. Another [1].");
        assert_eq!(order, [0]);
        let (t, order) = renumber("Zero is not a passage [0].", 8);
        assert_eq!(t, "Zero is not a passage.");
        assert!(order.is_empty());
    }

    #[test]
    fn a_list_marker_becomes_separate_markers_and_drops_its_bad_numbers() {
        let (t, order) = renumber("Both say so [3, 9, 1].", 8);
        assert_eq!(t, "Both say so [1][2].");
        assert_eq!(order, [2, 0]);
        let (t, _) = renumber("Twice [2,2].", 8);
        assert_eq!(t, "Twice [1].");
    }

    #[test]
    fn links_and_ordinary_brackets_are_left_alone() {
        let raw = "See [the paper](https://x.org) and [note] and [1](https://y.org) [2].";
        let (t, order) = renumber(raw, 8);
        assert_eq!(
            t,
            "See [the paper](https://x.org) and [note] and [1](https://y.org) [1]."
        );
        assert_eq!(order, [1]);
    }

    #[test]
    fn an_unclosed_bracket_ends_the_text_unchanged() {
        let (t, order) = renumber("Cut off here [3", 8);
        assert_eq!(t, "Cut off here [3");
        assert!(order.is_empty());
    }

    #[test]
    fn an_uncited_reply_is_kept_whole() {
        let (t, order) = renumber("The sources do not cover this.", 8);
        assert_eq!(t, "The sources do not cover this.");
        assert!(order.is_empty());
    }

    #[test]
    fn passages_are_numbered_with_their_source_title() {
        let docs = vec![
            NamedDoc {
                name: "a.md".into(),
                title: "Moshi".into(),
                text: String::new(),
            },
            NamedDoc {
                name: "b.md".into(),
                title: "Mimi".into(),
                text: String::new(),
            },
        ];
        let p = user_prompt(
            &docs,
            &[(1, "codec".into()), (0, "latency".into())],
            " Why? ",
        );
        assert!(p.contains("[1] (Mimi) codec"));
        assert!(p.contains("[2] (Moshi) latency"));
        assert!(p.ends_with("Question: Why?"));
    }
}
