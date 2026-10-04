//! Web research for a prep: plan searches, read pages, write a cited report.
//!
//! A person who asks to understand "the Mochi paper" should not have to go and
//! find it first. With research on (the page's default), the prep job searches
//! and reads the web on the person's own words before ingest, and the report
//! is staged as one more source beside whatever the person added. Ingest, the
//! script and the deck then treat it like any other document.
//!
//! Four steps, each a plain call:
//!
//! 1. a model turns the topic into a few focused search queries;
//! 2. each query goes through [`crate::agent::web_search`];
//! 3. the best distinct pages are read with the same fetcher a pasted link uses;
//! 4. a model writes a report from those pages, citing each claim by number.
//!
//! The depth setting decides how wide it goes: `quick` reads a handful of
//! pages, `standard` about a dozen.
//!
//! It runs inside the prep job, not in the request that submits it, for the
//! same reason the rest of the prep does: a research run is minutes.
//!
//! The report is written into the session's resource directory rather than
//! handed to ingest in memory, so what the session was built from stays
//! inspectable on disk next to the person's own sources.

use std::path::Path;
use std::time::{Duration, Instant};

use opennotebook_ai::Message;

/// The name the report is staged under. One per session: a second run on the
/// same session replaces it rather than stacking a near-duplicate.
pub const REPORT_FILE: &str = "web_research.md";

/// How long a research run may take before the prep gives up on it. Past this
/// the prep carries on without it rather than hold the whole session hostage
/// to one search.
const DEADLINE: Duration = Duration::from_secs(15 * 60);

/// Characters of each page the report is written from. A dozen pages at this
/// size is well inside a small model's window.
const PAGE_CHARS: usize = 8_000;

/// What came back from a finished run.
pub struct Found {
    pub chars: usize,
    pub sources: usize,
}

/// How wide a run goes: queries planned, pages read.
fn breadth(quality: &str) -> (usize, usize) {
    match quality {
        "quick" => (3, 5),
        _ => (5, 12),
    }
}

fn say(progress: &Option<tokio::sync::mpsc::UnboundedSender<String>>, what: impl Into<String>) {
    if let Some(tx) = progress {
        let _ = tx.send(what.into());
    }
}

/// Research `topic` on the web and stage the report in `dir`.
///
/// An error is returned rather than swallowed, and the caller decides whether
/// it matters: with the person's own sources staged the prep can go on without
/// the web, with none it has nothing to narrate.
pub async fn gather(
    topic: &str,
    dir: &Path,
    quality: &str,
    progress: Option<tokio::sync::mpsc::UnboundedSender<String>>,
) -> anyhow::Result<Found> {
    tokio::time::timeout(DEADLINE, run(topic, dir, quality, &progress))
        .await
        .map_err(|_| {
            anyhow::anyhow!(
                "web research did not finish in {} minutes",
                DEADLINE.as_secs() / 60
            )
        })?
}

async fn run(
    topic: &str,
    dir: &Path,
    quality: &str,
    progress: &Option<tokio::sync::mpsc::UnboundedSender<String>>,
) -> anyhow::Result<Found> {
    let started = Instant::now();
    let provider = opennotebook_session::ai::provider()
        .await
        .map_err(|e| anyhow::anyhow!(e))?;
    let model = opennotebook_session::settings::agent_model().await;
    let (n_queries, n_pages) = breadth(quality);

    say(progress, "Planning searches");
    let queries = plan(&provider, &model, topic, n_queries).await;

    // Every distinct url, in the order the searches ranked them, so the first
    // query's best results are read first.
    let mut urls: Vec<String> = Vec::new();
    for q in &queries {
        say(progress, format!("Searching: {q}"));
        match crate::agent::web_search(&provider, q).await {
            Ok(hits) => {
                for h in hits {
                    if !urls.contains(&h.url) {
                        urls.push(h.url);
                    }
                }
            }
            Err(e) => eprintln!("opennotebook research: search {q:?}: {e}"),
        }
    }
    anyhow::ensure!(
        !urls.is_empty(),
        "the web searches found nothing on {topic:?}"
    );

    let browser = crate::create::Browser::open()
        .await
        .map_err(|e| anyhow::anyhow!(e))?;
    let mut pages: Vec<(String, String, String)> = Vec::new();
    for url in urls {
        if pages.len() >= n_pages {
            break;
        }
        say(progress, format!("Reading {}", host(&url)));
        match browser.read(&url).await {
            Ok(p) if p.text.chars().filter(|c| !c.is_whitespace()).count() >= 200 => {
                let title = if p.title.trim().is_empty() {
                    host(&url)
                } else {
                    crate::create::one_line(&p.title)
                };
                pages.push((title, url, p.text.chars().take(PAGE_CHARS).collect()));
            }
            Ok(_) => {}
            Err(e) => eprintln!("opennotebook research: {url}: {e}"),
        }
    }
    anyhow::ensure!(
        !pages.is_empty(),
        "none of the pages found on {topic:?} could be read"
    );

    say(
        progress,
        format!("Writing the report from {} pages", pages.len()),
    );
    let report = write_report(&provider, topic, &pages).await?;
    let sources: Vec<(String, String)> = pages.into_iter().map(|(t, u, _)| (t, u)).collect();
    let body = document(topic, report.trim(), &sources);
    // Into the directory the caller made; never made here, so a collection
    // deleted during the minute research takes is not recreated by its report.
    std::fs::write(dir.join(REPORT_FILE), body.as_bytes())?;
    eprintln!(
        "opennotebook research: {topic:?}: {} sources, {} chars in {:?}",
        sources.len(),
        body.chars().count(),
        started.elapsed()
    );
    Ok(Found {
        chars: body.chars().count(),
        sources: sources.len(),
    })
}

/// Search queries for the topic. Falls back to the topic itself, so a planner
/// that answers badly costs breadth, not the run.
async fn plan(
    provider: &opennotebook_ai::Provider,
    model: &str,
    topic: &str,
    n: usize,
) -> Vec<String> {
    let asked = provider
        .completions()
        .model(model)
        .system(format!(
            "You plan web research. Given what a person wants to understand, reply with \
             {n} distinct, focused web search queries that together cover it: the primary \
             source first (the paper, the official docs, the project page), then explanations \
             and context. One query per line, no numbering, no quotes, nothing else."
        ))
        .user(topic.to_string())
        .max_tokens(300)
        .send()
        .await;
    let mut queries = match asked {
        Ok(r) => {
            opennotebook_session::spend::record("research_plan", model, r.usage.as_ref());
            parse_queries(&r.text, n)
        }
        Err(e) => {
            eprintln!("opennotebook research: planning failed: {e}");
            Vec::new()
        }
    };
    if queries.is_empty() {
        queries.push(topic.chars().take(200).collect());
    }
    queries
}

fn parse_queries(text: &str, n: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for line in text.lines() {
        let q = line
            .trim()
            .trim_start_matches(|c: char| c.is_ascii_digit() || matches!(c, '.' | ')' | '-' | '*'))
            .trim()
            .trim_matches('"')
            .trim();
        if q.len() >= 3 && !out.iter().any(|o| o.eq_ignore_ascii_case(q)) {
            out.push(q.to_string());
        }
    }
    out.truncate(n);
    out
}

/// The report, written from the pages and citing them as `[n]`.
async fn write_report(
    provider: &opennotebook_ai::Provider,
    topic: &str,
    pages: &[(String, String, String)],
) -> anyhow::Result<String> {
    let model = opennotebook_session::settings::notes_model().await;
    let mut material = String::new();
    for (i, (title, url, text)) in pages.iter().enumerate() {
        material.push_str(&format!("[{}] {title} ({url})\n{text}\n\n", i + 1));
    }
    let language = opennotebook_session::settings::language_rule(
        &opennotebook_session::settings::language().await,
    );
    let resp = provider
        .completions()
        .model(&model)
        .message(Message::system(format!(
            "You write a research report for someone studying a topic, from numbered web \
             pages. Use only what the pages say. Cite every claim with the number of the page \
             it comes from, like [2]. Organise it under Markdown headings: what it is, how it \
             works, why it matters, and open questions or disagreements between sources. Be \
             thorough and specific: names, numbers, mechanisms. No preamble, and no list of \
             sources at the end; that is added for you. {language}"
        )))
        .user(format!("Topic: {topic}\n\nPages:\n\n{material}"))
        .max_tokens(6000)
        .send()
        .await
        .map_err(|e| anyhow::anyhow!("the report could not be written: {e}"))?;
    opennotebook_session::spend::record("research_report", &model, resp.usage.as_ref());
    anyhow::ensure!(!resp.text.trim().is_empty(), "the report came back empty");
    Ok(resp.text)
}

fn host(url: &str) -> String {
    url.split("://")
        .nth(1)
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or(url)
        .trim_start_matches("www.")
        .to_string()
}

/// The staged document: what it is, the report, and where it came from.
fn document(topic: &str, report: &str, sources: &[(String, String)]) -> String {
    let mut out = format!(
        "# Web research: {topic}\n\nSource: web research on the topic above, gathered \
         for this session.\n\n{report}\n"
    );
    if !sources.is_empty() {
        out.push_str("\n## Sources read\n\n");
        for (i, (title, url)) in sources.iter().enumerate() {
            out.push_str(&format!("{}. [{title}]({url})\n", i + 1));
        }
    }
    out
}

/// The topic to research, from what the person said.
///
/// Their own words, not a model's paraphrase: "help me understand the mochi
/// paper" is a better search brief than any title distilled from it. The title
/// leads when there is one, because it is the conversation's settled name for
/// the thing; the messages follow for the detail.
#[cfg(test)]
pub fn topic_from(title: &str, said: &[String]) -> String {
    let mut parts: Vec<&str> = Vec::new();
    if !title.trim().is_empty() {
        parts.push(title.trim());
    }
    parts.extend(said.iter().map(|s| s.trim()).filter(|s| !s.is_empty()));
    let joined = parts.join(" — ");
    joined.chars().take(600).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_staged_report_says_what_it_is_and_where_it_came_from() {
        let doc = document(
            "the mochi paper",
            "Mochi is a speech model.",
            &[("Moshi".into(), "https://arxiv.org/abs/2410.00037".into())],
        );
        assert!(doc.starts_with("# Web research: the mochi paper"));
        assert!(doc.contains("Mochi is a speech model."));
        assert!(doc.contains("1. [Moshi](https://arxiv.org/abs/2410.00037)"));
    }

    #[test]
    fn planned_queries_are_cleaned_deduplicated_and_capped() {
        let got = parse_queries(
            "1. Moshi paper arxiv\n- \"Moshi speech model\"\n\nmoshi paper ARXIV\n3) full duplex\nx\nmore",
            3,
        );
        assert_eq!(
            got,
            vec!["Moshi paper arxiv", "Moshi speech model", "full duplex"]
        );
    }

    #[test]
    fn depth_sets_the_breadth() {
        assert_eq!(breadth("quick"), (3, 5));
        assert_eq!(breadth("standard"), (5, 12));
        assert_eq!(host("https://www.arxiv.org/abs/1"), "arxiv.org");
    }

    #[test]
    fn a_topic_is_the_persons_own_words_and_bounded() {
        assert_eq!(
            topic_from("", &["Help me understand the mochi paper".into()]),
            "Help me understand the mochi paper"
        );
        assert_eq!(
            topic_from("Moshi", &["the paper".into(), " ".into()]),
            "Moshi — the paper"
        );
        let long = "x".repeat(2000);
        assert_eq!(topic_from("", &[long]).chars().count(), 600);
    }
}
