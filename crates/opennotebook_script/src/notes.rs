//! A draft's sources as study notes: a written study guide of the key ideas,
//! every claim cited to the passage it rests on.
//!
//! The design and the evidence behind it are in `docs/study-notes-spec.md`.
//! In short, it is NotebookLM's study guide and briefing doc in one: an
//! overview, the key ideas, a glossary of key terms, a short-answer quiz with
//! its answers, and essay questions. The whole of every source goes to the
//! model as numbered passages, so a citation can point at any passage and not
//! only at the few a question would retrieve.
//!
//! What comes back is checked, not trusted, the way `cite` checks an answer:
//!
//! - a number that names no passage is removed;
//! - a citation whose claim shares no word with the passage it names is
//!   removed and counted, because a model that has lost track of its numbering
//!   cites confidently and wrongly;
//! - the rest are renumbered in reading order, so the reader meets `[1]` first.
//!
//! The model writes Markdown under fixed headings, and the sections are read
//! out of it here. A heading the model renames ("Key concepts" for "Key
//! ideas") is still found; a section it leaves out is simply empty.

use crate::error::ScriptError;
use crate::grounding;
use crate::mindmap::NamedDoc;

/// Above this much source text the sources are cut to excerpts. The same
/// ceiling as the mind map's: about 150k tokens, far past any real draft.
pub const WHOLE_TEXT_CHARS: usize = 600_000;

/// The fewest key ideas worth showing without asking once more.
pub const MIN_IDEAS: usize = 2;

const STAGE: &str = "study notes";

/// One key idea: a heading and what the sources say about it, in Markdown
/// with `[n]` markers.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Idea {
    pub heading: String,
    pub body: String,
}

/// One glossary entry.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Term {
    pub term: String,
    pub definition: String,
}

/// One short-answer question and the answer the sources give.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Question {
    pub question: String,
    pub answer: String,
}

/// One cited passage, numbered as the notes number it.
#[derive(Debug, Clone, PartialEq)]
pub struct Cited {
    /// The number in the notes, from 1.
    pub n: u32,
    /// Index into the documents the notes were made from.
    pub doc: usize,
    pub excerpt: String,
}

/// What a generation produced, with what it had to do to get there.
#[derive(Debug, Clone, Default)]
pub struct StudyNotes {
    pub title: String,
    pub overview: String,
    pub ideas: Vec<Idea>,
    pub glossary: Vec<Term>,
    pub quiz: Vec<Question>,
    pub essays: Vec<String>,
    /// Only the passages the notes cite, in order of first use.
    pub cited: Vec<Cited>,
    /// Citations removed: a number naming no passage, or a passage that
    /// shares no word with the claim citing it.
    pub dropped: u32,
    /// The sources were cut to excerpts to fit one call.
    pub excerpted: bool,
    /// The citation check did not run, because the notes are in another
    /// language than English and cannot be matched word for word.
    pub unchecked: bool,
    pub model: String,
}

impl StudyNotes {
    /// The notes as one Markdown document, `[n]` markers and all, with the
    /// cited sources listed at the end. The export, and what an agent reads.
    pub fn to_markdown(&self, source_title: impl Fn(usize) -> String) -> String {
        let mut out = format!("# {}\n\n", self.title);
        if !self.overview.is_empty() {
            out.push_str(&format!("## Overview\n\n{}\n\n", self.overview));
        }
        if !self.ideas.is_empty() {
            out.push_str("## Key ideas\n\n");
            for i in &self.ideas {
                out.push_str(&format!("### {}\n\n{}\n\n", i.heading, i.body));
            }
        }
        if !self.quiz.is_empty() {
            out.push_str("## Quiz\n\n");
            for (k, q) in self.quiz.iter().enumerate() {
                out.push_str(&format!("{}. {}\n", k + 1, q.question));
            }
            out.push_str("\n## Answer key\n\n");
            for (k, q) in self.quiz.iter().enumerate() {
                out.push_str(&format!("{}. {}\n", k + 1, q.answer));
            }
            out.push('\n');
        }
        if !self.essays.is_empty() {
            out.push_str("## Essay questions\n\n");
            for (k, e) in self.essays.iter().enumerate() {
                out.push_str(&format!("{}. {e}\n", k + 1));
            }
            out.push('\n');
        }
        if !self.glossary.is_empty() {
            out.push_str("## Glossary\n\n");
            for t in &self.glossary {
                out.push_str(&format!("- **{}**: {}\n", t.term, t.definition));
            }
            out.push('\n');
        }
        if !self.cited.is_empty() {
            out.push_str("## Sources\n\n");
            for c in &self.cited {
                let short: String = c.excerpt.split_whitespace().collect::<Vec<_>>().join(" ");
                let short: String = if short.chars().count() > 200 {
                    format!("{}…", short.chars().take(200).collect::<String>())
                } else {
                    short
                };
                out.push_str(&format!("{}. {}: \"{short}\"\n", c.n, source_title(c.doc)));
            }
        }
        out.trim_end().to_string() + "\n"
    }
}

/// Make study notes of `sources`.
///
/// `title_hint` names the notes when the model writes no title. `focus` is the
/// person's own words, or nothing.
pub async fn generate(
    sources: &[NamedDoc],
    title_hint: &str,
    focus: Option<&str>,
) -> Result<StudyNotes, ScriptError> {
    let model = opennotebook_session::settings::notes_model().await;
    let language = opennotebook_session::settings::language().await;
    let rule = opennotebook_session::settings::language_rule(&language);
    let check = rule.is_empty();

    let (passages, excerpted) = numbered(sources, title_hint);
    if passages.is_empty() {
        return Err(ScriptError::Empty { stage: STAGE });
    }
    let system = system_prompt(focus, &rule);
    let user = user_prompt(sources, &passages, focus);

    let mut best: Option<StudyNotes> = None;
    for attempt in 0..2 {
        let user = if attempt == 0 {
            user.clone()
        } else {
            format!(
                "{user}\n\nYour last notes were missing their sections. Write every section under \
                 its exact heading — ## Overview, ## Key ideas with a ### heading per idea, \
                 ## Quiz, ## Answer key, ## Essay questions, ## Glossary — and cite with [n]."
            )
        };
        // A reply cut at the token ceiling keeps the sections before the cut:
        // notes missing their essay questions beat no notes.
        let raw = match crate::generate::send(&model, &system, &user, STAGE, true).await {
            Ok(t) => t,
            Err(ScriptError::Truncated { text, .. }) if !text.trim().is_empty() => text,
            Err(e) => return Err(e),
        };
        let notes = read(&raw, &passages, title_hint, check);
        let enough = notes.ideas.len() >= MIN_IDEAS;
        if best.as_ref().is_none_or(|b| weight(&notes) > weight(b)) {
            best = Some(notes);
        }
        if enough {
            break;
        }
        let head: String = raw.chars().take(800).collect();
        eprintln!(
            "opennotebook notes: attempt {} came back thin; reply starts:\n{head}",
            attempt + 1
        );
    }

    match best {
        Some(mut n) if !n.ideas.is_empty() || !n.overview.is_empty() => {
            n.excerpted = excerpted;
            n.unchecked = !check;
            n.model = model;
            Ok(n)
        }
        _ => Err(ScriptError::Empty { stage: STAGE }),
    }
}

/// How much a set of notes holds, to keep the fuller of two attempts.
fn weight(n: &StudyNotes) -> usize {
    3 * n.ideas.len() + n.glossary.len() + n.quiz.len() + n.essays.len()
}

/// A reply, checked and read into its sections.
pub fn read(raw: &str, passages: &[(usize, String)], title_hint: &str, check: bool) -> StudyNotes {
    let (checked, unsupported) = if check {
        check_citations(raw, passages)
    } else {
        (raw.to_string(), 0)
    };
    let invalid = count_invalid(&checked, passages.len());
    let (text, order) = crate::cite::renumber(&checked, passages.len());
    let mut notes = parse_sections(&text, title_hint);
    notes.cited = order
        .into_iter()
        .enumerate()
        .map(|(i, p)| Cited {
            n: i as u32 + 1,
            doc: passages[p].0,
            excerpt: passages[p].1.clone(),
        })
        .collect();
    notes.dropped = unsupported + invalid;
    notes
}

/// Every passage of every source, numbered from 1 by position in this list,
/// with the source it came from. Cut to the passages about the title when the
/// sources are too long to send whole; the second value says so.
pub fn numbered(sources: &[NamedDoc], query: &str) -> (Vec<(usize, String)>, bool) {
    let texts: Vec<String> = sources.iter().map(|s| s.text.clone()).collect();
    let total: usize = texts.iter().map(|t| t.chars().count()).sum();
    if total <= WHOLE_TEXT_CHARS {
        return (grounding::passages(&texts), false);
    }
    // An even share each, so a long source does not crowd out a short one.
    let keep = (WHOLE_TEXT_CHARS / sources.len().max(1) / 900).max(1);
    let mut out = Vec::new();
    for (i, t) in texts.iter().enumerate() {
        for p in grounding::excerpts(std::slice::from_ref(t), query, keep) {
            out.push((i, p));
        }
    }
    (out, true)
}

fn system_prompt(focus: Option<&str>, language_rule: &str) -> String {
    let focus_rule = if focus.is_some_and(|f| !f.trim().is_empty()) {
        "\n- A focus is given after the passages. Build the notes around it, and leave out \
         what does not bear on it."
    } else {
        ""
    };
    // The sections and their sizes are NotebookLM's study guide — short-answer
    // quiz, its answer key apart, essay questions, glossary of key terms — with
    // its briefing doc's overview and key ideas in front, because the tile
    // promises "the key ideas". The citation rules are SurfSense's, which are
    // the plainest of the open implementations read for the spec.
    let mut s = format!(
        "You write study notes from a person's source material: a study guide that first \
         teaches the key ideas, then lets them test themselves, the way a good teacher's \
         handout does.\n\n\
         The material comes as numbered passages, [1], [2] and so on. How to cite:\n\
         1. Put the passage's number in square brackets right after the claim it supports: [12].\n\
         2. Several passages for one claim: stack them, [12][40].\n\
         3. Copy the numbers exactly as shown. Never renumber them or make one up, and never \
            write a title, link or footnote instead.\n\
         4. Cite the passage that actually says it. If no passage backs a claim, leave the \
            claim out.\n\
         5. No list of references at the end: the numbers are the references.\n\n\
         Write Markdown: exactly these sections, in this order, under exactly these headings.\n\
         # <a title for the notes, at most 10 words>\n\
         ## Overview\n\
         Two to four sentences: what the material is about and why it matters. Cited.\n\
         ## Key ideas\n\
         Four to seven ideas, the most important first, each as:\n\
         ### <the idea, as a short phrase>\n\
         Two to five sentences, or a short list, explaining it with the material's own facts, \
         numbers and examples. Every claim cited.\n\
         ## Quiz\n\
         Ten short-answer questions, numbered 1 to 10, each answerable in two or three \
         sentences from the material. Test understanding — why, how, what follows — not only \
         recall, and spread them across the key ideas.\n\
         ## Answer key\n\
         Ten answers, numbered 1 to 10 to match the questions, two or three sentences each. \
         Every answer cited.\n\
         ## Essay questions\n\
         Five open questions, numbered, that make the reader connect several ideas. No answers.\n\
         ## Glossary\n\
         Fifteen to twenty key terms the material uses, one per line:\n\
         - **Term**: a one or two sentence definition, as the material uses it. Cited.\n\n\
         Rules:\n\
         - Use only what the passages say. Do not add facts from your own knowledge. If the \
           material is thin on something, say less rather than fill in.\n\
         - Plain, clear language for a learner. Define a term the first time it is used.\n\
         - The quiz and essays test the key ideas; they do not introduce new facts.\n\
         - Do not start every bullet with the same word.\n\
         - No preamble, no closing remarks, nothing outside the sections.{focus_rule}"
    );
    if !language_rule.is_empty() {
        s.push_str(&format!(
            "\n\n{language_rule} Keep the Markdown headings' structure and the [n] markers \
             exactly as described."
        ));
    }
    s
}

fn user_prompt(sources: &[NamedDoc], passages: &[(usize, String)], focus: Option<&str>) -> String {
    let mut s = String::from("Passages:\n\n");
    for (i, (doc, text)) in passages.iter().enumerate() {
        let title = sources.get(*doc).map_or("", |d| d.title.trim());
        s.push_str(&format!("[{}] ({title}) {}\n\n", i + 1, text.trim()));
    }
    if let Some(f) = focus.map(str::trim).filter(|f| !f.is_empty()) {
        s.push_str(&format!("Focus: {f}\n"));
    }
    s
}

// ── the citation check ──────────────────────────────────────────────────────

/// The reply with every citation whose claim shares no word with its passage
/// removed, and how many were.
///
/// The claim is the text between the previous marker or sentence end and the
/// marker. A claim of fewer than two matchable words ("See [3].") is not
/// judged, because there is nothing to match. A word matches when it, or the
/// word without a final "s", appears in the passage. A number that names no
/// passage is left alone here: `renumber` removes those, and they are counted
/// apart.
pub fn check_citations(raw: &str, passages: &[(usize, String)]) -> (String, u32) {
    let lowered: Vec<String> = passages.iter().map(|(_, p)| p.to_lowercase()).collect();
    let mut dropped = 0u32;
    let mut out = String::with_capacity(raw.len());
    let mut claim_from = 0usize;
    let mut rest = raw;
    while let Some(open) = rest.find('[') {
        let (before, from) = rest.split_at(open);
        out.push_str(before);
        let Some(close) = from.find(']') else {
            out.push_str(from);
            rest = "";
            break;
        };
        let inner = &from[1..close];
        let numbers: Option<Vec<usize>> = (!inner.trim().is_empty())
            .then(|| {
                inner
                    .split(',')
                    .map(|p| p.trim().parse::<usize>().ok())
                    .collect::<Option<Vec<_>>>()
            })
            .flatten();
        let is_marker = numbers.is_some() && !from[close + 1..].starts_with('(');
        if !is_marker {
            out.push('[');
            rest = &from[1..];
            continue;
        }
        let claim = claim_before(&out[claim_from..]);
        let words = grounding::terms(claim);
        let kept: Vec<usize> = numbers
            .unwrap_or_default()
            .into_iter()
            .filter(|&n| {
                let Some(p) = n.checked_sub(1).and_then(|i| lowered.get(i)) else {
                    return true;
                };
                let ok = words.len() < 2 || supports(p, &words);
                if !ok {
                    dropped += 1;
                    // The service log is where a wrong drop shows up: the
                    // claim and the passage it named, side by side.
                    let head: String = p.chars().take(160).collect();
                    eprintln!(
                        "opennotebook notes: dropped [{n}] for \"{}\" — passage: \"{head}\"",
                        claim.trim()
                    );
                }
                ok
            })
            .collect();
        if kept.is_empty() {
            while out.ends_with(' ') {
                out.pop();
            }
        } else {
            let list = kept
                .iter()
                .map(usize::to_string)
                .collect::<Vec<_>>()
                .join(", ");
            out.push_str(&format!("[{list}]"));
        }
        // A run of markers, "[2][5]", shares one claim.
        if !from[close + 1..].starts_with('[') {
            claim_from = out.len();
        }
        rest = &from[close + 1..];
    }
    out.push_str(rest);
    (out, dropped)
}

/// The last sentence or line of `text`: the claim a marker at its end cites.
fn claim_before(text: &str) -> &str {
    let t = text.trim_end();
    let cut = t
        .char_indices()
        .rev()
        .find(|&(i, c)| {
            c == '\n'
                || (matches!(c, '.' | '!' | '?')
                    && i + 1 < t.len()
                    && t[i + 1..].starts_with(char::is_whitespace))
        })
        .map_or(0, |(i, c)| i + c.len_utf8());
    &t[cut..]
}

/// Whether a passage carries enough of a claim's words to be what it cites:
/// two distinct words, or three in ten of a short claim's.
///
/// One shared word is not enough: "Mimi is a neural audio codec" matched a
/// passage about audio TOKENS on "audio" alone. A ratio alone is too much: a
/// long paraphrase — "Helium is a text LLM that serves as Moshi's backbone,
/// providing it with reasoning abilities…", fourteen words — shares four with
/// the passage that introduces Helium, under three in ten, and was dropped
/// from a correct citation. Measured on the Moshi paper, the ratio alone
/// dropped 22 citations, about half of them right ones.
fn supports(passage: &str, words: &[String]) -> bool {
    let hit = words.iter().filter(|w| mentions(passage, w)).count();
    hit >= 2 || (hit == 1 && words.len() <= 3)
}

fn mentions(passage: &str, word: &str) -> bool {
    passage.contains(word)
        || word
            .strip_suffix('s')
            .is_some_and(|w| w.len() >= 3 && passage.contains(w))
}

/// Citation numbers in `text` that name no passage: what `renumber` is about to
/// remove.
fn count_invalid(text: &str, passages: usize) -> u32 {
    let mut n = 0u32;
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let from = &rest[open..];
        let Some(close) = from.find(']') else { break };
        let inner = &from[1..close];
        let nums: Option<Vec<usize>> = inner
            .split(',')
            .map(|p| p.trim().parse::<usize>().ok())
            .collect();
        if let Some(nums) = nums
            && !inner.trim().is_empty()
            && !from[close + 1..].starts_with('(')
        {
            n += nums.iter().filter(|&&k| k == 0 || k > passages).count() as u32;
        }
        rest = &from[1..];
    }
    n
}

// ── reading the sections ────────────────────────────────────────────────────

#[derive(Clone, Copy, PartialEq, Debug)]
enum Section {
    None,
    Overview,
    Ideas,
    Glossary,
    Quiz,
    Answers,
    Essays,
}

/// Which section a `##` heading opens. Read loosely: a model that writes
/// "Key concepts", "Short-answer quiz" or "Glossary of key terms" means the
/// same sections. Answers are checked before the quiz, because "Quiz answer
/// key" names both, and "Short-answer questions" is the quiz, not its key.
fn section_of(heading: &str) -> Section {
    let h = heading.to_lowercase();
    let has = |words: &[&str]| words.iter().any(|w| h.contains(w));
    if has(&["answer"]) && !has(&["question", "short-answer", "short answer"]) {
        Section::Answers
    } else if has(&["essay", "discussion", "reflection"]) {
        Section::Essays
    } else if has(&[
        "quiz",
        "question",
        "self-test",
        "test yourself",
        "check your",
    ]) {
        Section::Quiz
    } else if has(&["glossary", "term", "vocabulary", "definition"]) {
        Section::Glossary
    } else if has(&["overview", "summary", "introduction", "at a glance"]) {
        Section::Overview
    } else if has(&["idea", "concept", "theme", "point", "takeaway", "insight"]) {
        Section::Ideas
    } else {
        Section::None
    }
}

/// A heading line's level and text: `## Quiz` is (2, "Quiz"). A bold line on
/// its own, `**Quiz**`, is read as level 2, which is how a model writes a
/// heading it has forgotten the hashes for.
fn heading(line: &str) -> Option<(usize, String)> {
    let t = line.trim();
    let hashes = t.chars().take_while(|&c| c == '#').count();
    if hashes > 0 && t[hashes..].starts_with(' ') {
        return Some((hashes, clean_heading(&t[hashes..])));
    }
    if t.len() > 4 && t.starts_with("**") && t.ends_with("**") && !t[2..t.len() - 2].contains("**")
    {
        return Some((2, clean_heading(&t[2..t.len() - 2])));
    }
    None
}

fn clean_heading(h: &str) -> String {
    h.trim()
        .trim_matches(|c: char| matches!(c, '*' | '_' | ':' | '#'))
        .trim()
        .to_string()
}

/// A list item's text without its marker: `- x`, `* x`, `1. x`, `1) x`.
fn item(line: &str) -> Option<&str> {
    let t = line.trim_start();
    if let Some(r) = t
        .strip_prefix("- ")
        .or_else(|| t.strip_prefix("* "))
        .or_else(|| t.strip_prefix("• "))
    {
        return Some(r.trim());
    }
    let digits = t.chars().take_while(char::is_ascii_digit).count();
    if digits > 0 && digits <= 3 {
        let r = &t[digits..];
        if let Some(r) = r.strip_prefix(". ").or_else(|| r.strip_prefix(") ")) {
            return Some(r.trim());
        }
    }
    None
}

/// A glossary line as its term and definition: `**Term**: def`, `Term: def`,
/// `Term — def`.
fn term_of(text: &str) -> Option<Term> {
    let t = text.trim();
    let (term, def) = if let Some(r) = t.strip_prefix("**") {
        let end = r.find("**")?;
        let def = r[end + 2..].trim_start_matches([':', '-', '—', '–', ' ']);
        (r[..end].trim_end_matches(':'), def)
    } else if let Some((a, b)) = t.split_once(": ") {
        (a, b)
    } else if let Some((a, b)) = t.split_once(" — ").or_else(|| t.split_once(" - ")) {
        (a, b)
    } else {
        return None;
    };
    let term = term.trim().trim_matches('*').trim();
    let def = def.trim();
    (!term.is_empty() && !def.is_empty() && term.chars().count() <= 80).then(|| Term {
        term: term.to_string(),
        definition: def.to_string(),
    })
}

/// A quiz line's answer, when the model wrote it under its question:
/// `Answer: …` or `A: …`.
fn inline_answer(text: &str) -> Option<&str> {
    let t = text.trim().trim_start_matches(['*', '_']);
    for p in ["Answer:", "answer:", "A:", "Answer**:", "Answer:**"] {
        if let Some(r) = t.strip_prefix(p) {
            return Some(r.trim().trim_start_matches(['*', '_']).trim());
        }
    }
    None
}

/// The notes' sections, read out of the Markdown.
pub fn parse_sections(text: &str, title_hint: &str) -> StudyNotes {
    let mut n = StudyNotes::default();
    let mut section = Section::None;
    let mut overview: Vec<String> = Vec::new();
    let mut answers: Vec<String> = Vec::new();
    for line in text.lines() {
        if let Some((level, h)) = heading(line) {
            if level == 1 {
                if n.title.is_empty() && !h.is_empty() {
                    n.title = h;
                }
                continue;
            }
            if level >= 3 && section == Section::Ideas {
                n.ideas.push(Idea {
                    heading: h,
                    body: String::new(),
                });
                continue;
            }
            let s = section_of(&h);
            if s != Section::None {
                section = s;
                continue;
            }
            // An unknown `##` heading inside the key ideas is an idea a model
            // wrote one level too high.
            if section == Section::Ideas {
                n.ideas.push(Idea {
                    heading: h,
                    body: String::new(),
                });
            }
            continue;
        }
        let t = line.trim();
        match section {
            Section::None => {}
            Section::Overview => {
                if !t.is_empty() {
                    overview.push(t.to_string());
                }
            }
            Section::Ideas => {
                if let Some(idea) = n.ideas.last_mut() {
                    if !idea.body.is_empty() || !t.is_empty() {
                        idea.body.push_str(line.trim_end());
                        idea.body.push('\n');
                    }
                } else if !t.is_empty() {
                    // Body text before any idea heading: an overview the
                    // model put in the wrong place, kept rather than lost.
                    overview.push(t.to_string());
                }
            }
            Section::Glossary => {
                if let Some(term) = item(line)
                    .and_then(term_of)
                    .or_else(|| term_of(t).filter(|_| !t.is_empty()))
                {
                    n.glossary.push(term);
                }
            }
            Section::Quiz => {
                if let Some(q) = item(line) {
                    if let Some(a) = inline_answer(q) {
                        if let Some(last) = n.quiz.last_mut() {
                            last.answer = a.to_string();
                        }
                    } else {
                        n.quiz.push(Question {
                            question: q.to_string(),
                            answer: String::new(),
                        });
                    }
                } else if let Some(a) = inline_answer(t)
                    && let Some(last) = n.quiz.last_mut()
                {
                    last.answer = a.to_string();
                }
            }
            Section::Answers => {
                if let Some(a) = item(line) {
                    answers.push(inline_answer(a).unwrap_or(a).to_string());
                } else if !t.is_empty()
                    && let Some(last) = answers.last_mut()
                {
                    last.push(' ');
                    last.push_str(t);
                }
            }
            Section::Essays => {
                if let Some(e) = item(line) {
                    n.essays.push(e.to_string());
                }
            }
        }
    }
    // The answer key, matched to the questions by position.
    for (q, a) in n.quiz.iter_mut().zip(answers) {
        if q.answer.is_empty() {
            q.answer = a;
        }
    }
    // Alphabetical, as a glossary is read; the same term twice keeps the first.
    n.glossary.sort_by_key(|t| t.term.to_lowercase());
    n.glossary
        .dedup_by(|a, b| a.term.eq_ignore_ascii_case(&b.term));
    n.overview = overview.join(" ");
    for i in &mut n.ideas {
        i.body = i.body.trim().to_string();
    }
    n.ideas
        .retain(|i| !i.heading.is_empty() && !i.body.is_empty());
    if n.title.is_empty() {
        n.title = title_hint.to_string();
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(texts: &[&str]) -> Vec<(usize, String)> {
        texts.iter().map(|t| (0, t.to_string())).collect()
    }

    const REPLY: &str = "\
# Moshi: Real-Time Spoken Dialogue

## Overview
Moshi is a speech-text foundation model for full-duplex dialogue [1].

## Key Ideas
### Speech in, speech out
Moshi skips the text pipeline [1]. Its latency is 200 ms in practice [2].

### Inner Monologue
- Text tokens are predicted before audio tokens [3].
- This improves linguistic quality [3].

## Glossary of Key Terms
- **Full-duplex**: listening and speaking at once [1].
- Mimi: a neural audio codec [2]

## Quiz
1. Why does Moshi skip text?
2. What is the Inner Monologue?
   Answer: Predicting text tokens before audio tokens [3].

## Quiz Answer Key
1. Because a pipeline adds latency and loses non-linguistic information [1].
2. ignored, the inline answer wins

## Essay Questions
1. How do latency and linguistic quality trade off in Moshi?
";

    const PASSAGES: [&str; 3] = [
        "Moshi is a speech-text foundation model and full-duplex spoken dialogue framework. \
         It skips the text pipeline, whose latency and loss of non-linguistic information it avoids.",
        "Mimi is a neural audio codec. Moshi has a practical latency of 200 ms.",
        "Inner Monologue predicts time-aligned text tokens before audio tokens, improving the \
         linguistic quality of generated speech.",
    ];

    #[test]
    fn a_reply_reads_into_its_sections() {
        let n = read(REPLY, &p(&PASSAGES), "hint", true);
        assert_eq!(n.title, "Moshi: Real-Time Spoken Dialogue");
        assert!(n.overview.starts_with("Moshi is a speech-text"));
        assert_eq!(n.ideas.len(), 2);
        assert_eq!(n.ideas[1].heading, "Inner Monologue");
        assert!(n.ideas[1].body.contains("- Text tokens"));
        assert_eq!(n.glossary.len(), 2);
        assert_eq!(n.glossary[0].term, "Full-duplex");
        assert_eq!(n.glossary[1].term, "Mimi");
        assert_eq!(n.quiz.len(), 2);
        assert!(n.quiz[0].answer.starts_with("Because a pipeline"));
        assert!(n.quiz[1].answer.starts_with("Predicting text tokens"));
        assert_eq!(n.essays.len(), 1);
        assert_eq!(n.dropped, 0);
        // Three passages cited, numbered in reading order.
        assert_eq!(n.cited.len(), 3);
        assert_eq!(n.cited[0].n, 1);
    }

    #[test]
    fn citations_are_renumbered_in_reading_order() {
        let n = read(
            "## Overview\nLatency is 200 ms [2]. It skips text [1].\n",
            &p(&PASSAGES),
            "hint",
            true,
        );
        assert_eq!(n.overview, "Latency is 200 ms [1]. It skips text [2].");
        assert!(n.cited[0].excerpt.contains("200 ms"));
    }

    #[test]
    fn a_citation_to_a_passage_that_never_says_it_is_dropped_and_counted() {
        let (t, dropped) = check_citations(
            "Mimi is a neural audio codec [3]. Latency is 200 ms [2].",
            &p(&PASSAGES),
        );
        assert_eq!(t, "Mimi is a neural audio codec. Latency is 200 ms [2].");
        assert_eq!(dropped, 1);
        // One of two numbers wrong: only that one goes.
        let (t, dropped) = check_citations("Mimi is a neural audio codec [3, 2].", &p(&PASSAGES));
        assert_eq!(t, "Mimi is a neural audio codec [2].");
        assert_eq!(dropped, 1);
    }

    #[test]
    fn a_number_past_the_passages_is_removed_and_counted() {
        let n = read(
            "## Overview\nIt skips text [9]. And [1].\n",
            &p(&PASSAGES),
            "h",
            true,
        );
        assert_eq!(n.overview, "It skips text. And [1].");
        assert_eq!(n.dropped, 1);
    }

    #[test]
    fn a_run_of_markers_shares_one_claim() {
        let (t, dropped) = check_citations(
            "Moshi has a practical latency of 200 ms [2][1].",
            &p(&PASSAGES),
        );
        assert_eq!(t, "Moshi has a practical latency of 200 ms [2][1].");
        assert_eq!(dropped, 0);
    }

    #[test]
    fn plurals_match_their_passage() {
        // "codecs" against a passage that says "codec".
        let (_, dropped) = check_citations("Mimi codecs compress audio [2].", &p(&PASSAGES));
        assert_eq!(dropped, 0);
    }

    #[test]
    fn headings_without_hashes_and_renamed_sections_are_still_found() {
        let n = parse_sections(
            "**Summary**\nA short summary.\n**Key Concepts**\n### One\nBody one.\n### Two\nBody two.\n\
             **Short-Answer Questions**\n1) First?\n2) Second?\n**Answers**\n1) A1.\n2) A2.\n",
            "Hint",
        );
        assert_eq!(n.title, "Hint");
        assert_eq!(n.overview, "A short summary.");
        assert_eq!(n.ideas.len(), 2);
        assert_eq!(n.quiz.len(), 2);
        assert_eq!(n.quiz[1].answer, "A2.");
    }

    #[test]
    fn an_idea_written_one_level_too_high_is_still_an_idea() {
        let n = parse_sections(
            "## Key ideas\n## Latency\nIt is low.\n### Codec\nMimi.\n## Glossary\n- **X**: y\n",
            "h",
        );
        let heads: Vec<&str> = n.ideas.iter().map(|i| i.heading.as_str()).collect();
        assert_eq!(heads, ["Latency", "Codec"]);
        assert_eq!(n.glossary.len(), 1);
    }

    #[test]
    fn the_markdown_export_carries_every_section_and_its_sources() {
        let n = read(REPLY, &p(&PASSAGES), "hint", true);
        let md = n.to_markdown(|_| "Moshi paper".to_string());
        for h in [
            "# Moshi: Real-Time Spoken Dialogue",
            "## Overview",
            "### Inner Monologue",
            "- **Full-duplex**:",
            "## Quiz",
            "## Answer key",
            "## Essay questions",
            "## Sources",
            "1. Moshi paper: \"Moshi is a speech-text",
        ] {
            assert!(md.contains(h), "{h} missing from\n{md}");
        }
    }

    #[test]
    fn every_passage_of_a_short_source_is_numbered() {
        let docs = vec![NamedDoc {
            name: "a.md".into(),
            title: "A".into(),
            text: "First paragraph of real prose, long enough to be kept as a passage here.\n\n\
                   Second paragraph of real prose, also long enough to count as a passage."
                .into(),
        }];
        let (ps, cut) = numbered(&docs, "A");
        assert!(!cut);
        assert!(!ps.is_empty());
        let prompt = user_prompt(&docs, &ps, Some(" latency "));
        assert!(prompt.contains("[1] (A) First paragraph"));
        assert!(prompt.ends_with("Focus: latency\n"));
    }
}
