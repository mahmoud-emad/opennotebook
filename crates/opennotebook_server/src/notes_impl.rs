//! The `notes` domain: study notes of a collection's sources, every claim
//! cited.
//!
//! The writing is `opennotebook_script::notes`; this file reads the
//! collection's sources, hands them over, resolves each citation to its
//! source, and keeps what comes back. See `docs/study-notes-spec.md`.
//!
//! # Where notes live
//!
//! `var/opennotebook/notes/<cid>/<id>.json`, one file per set, in the store
//! `filestore` keeps for maps and notes alike.

use std::path::PathBuf;
use std::time::Duration;

use async_trait::async_trait;

use crate::create;
use crate::filestore;
use crate::notes::{
    NoteCitation, NoteIdea, NoteQuestion, NoteTerm, NotesCreateInput, NotesCreateOutput,
    NotesDeleteInput, NotesDeleteOutput, NotesEstimateInput, NotesEstimateOutput, NotesGetInput,
    NotesGetOutput, NotesListAllInput, NotesListAllOutput, NotesListInput, NotesListOutput,
    NotesRetitleInput, NotesRetitleOutput, NotesServiceApi, StudyNotes, StudyNotesSummary,
};
use opennotebook_script::notes as sn;

pub struct NotesService;

type Ctx = opennotebook_api::RequestContext;
type RpcError = opennotebook_api::RpcError;

/// How long a set of notes may take. One call that reads up to about 150k
/// tokens and writes about 4k; the writing is what takes the time.
const CREATE_TIMEOUT: Duration = Duration::from_secs(120);

/// Tokens a typical set of notes is written in: about 2,500 words across the
/// overview, five ideas, ten questions and answers, five essays and twenty
/// terms, with their markers.
const OUTPUT_TOKENS: u64 = 4_000;

#[async_trait]
impl NotesServiceApi for NotesService {
    async fn notes_create(
        &self,
        _ctx: &Ctx,
        input: NotesCreateInput,
    ) -> Result<NotesCreateOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?.to_string();
        let docs = crate::sources_impl::read_docs(&sid, req.sources.as_deref(), "study")?;
        let hint = if docs.len() == 1 {
            docs[0].title.clone()
        } else {
            "Study notes".to_string()
        };
        let focus = req
            .focus
            .as_deref()
            .map(str::trim)
            .filter(|f| !f.is_empty())
            .map(str::to_string);

        let made =
            tokio::time::timeout(CREATE_TIMEOUT, sn::generate(&docs, &hint, focus.as_deref()))
                .await
                .map_err(|_| {
                    internal(format!(
                        "the model took longer than {}s to write the notes",
                        CREATE_TIMEOUT.as_secs()
                    ))
                })?
                .map_err(|e| internal(format!("the study notes could not be written: {e}")))?;

        let dir = crate::sources_impl::staging_of(&sid)?;
        let described: Vec<_> = docs
            .iter()
            .map(|d| crate::sources_impl::describe(&dir.join(&d.name), d.name.clone()))
            .collect();
        let markdown = made.to_markdown(|i| {
            described
                .get(i)
                .map(|s| s.title.clone())
                .unwrap_or_default()
        });
        let citations = made
            .cited
            .iter()
            .map(|c| {
                let src = &described[c.doc];
                NoteCitation {
                    n: c.n as _,
                    name: src.name.clone(),
                    title: src.title.clone(),
                    url: src.url.clone(),
                    excerpt: c.excerpt.clone(),
                }
            })
            .collect();
        let notes = StudyNotes {
            id: uuid::Uuid::new_v4().to_string(),
            sid: sid.clone(),
            title: made.title,
            focus: focus.unwrap_or_default(),
            sources: docs.iter().map(|d| d.name.clone()).collect(),
            excerpted: made.excerpted,
            model: made.model,
            created_ms: crate::collection::now_ms() as _,
            dropped: made.dropped as _,
            unchecked: made.unchecked,
            overview: made.overview,
            ideas: made
                .ideas
                .into_iter()
                .map(|i| NoteIdea {
                    heading: i.heading,
                    body: i.body,
                })
                .collect(),
            quiz: made
                .quiz
                .into_iter()
                .map(|q| NoteQuestion {
                    question: q.question,
                    answer: q.answer,
                })
                .collect(),
            essays: made.essays,
            glossary: made
                .glossary
                .into_iter()
                .map(|t| NoteTerm {
                    term: t.term,
                    definition: t.definition,
                })
                .collect(),
            citations,
            markdown,
        };
        // Kept only while the collection is; see `mindmap_create`.
        let kept =
            crate::collection::output_saved(&sid, async { filestore::save(&notes_root(), &notes) })
                .await
                .map_err(internal)?;
        if kept.is_none() {
            return Err(bad_request(format!(
                "collection `{sid}` was deleted while the notes were written; nothing was kept"
            )));
        }
        flat(&notes)
    }

    async fn notes_estimate(
        &self,
        _ctx: &Ctx,
        input: NotesEstimateInput,
    ) -> Result<NotesEstimateOutput, RpcError> {
        let docs = crate::sources_impl::read_docs(&input.sid, None, "study")?;
        let chars: u64 = docs.iter().map(|d| d.text.chars().count() as u64).sum();
        let input_tokens = tokens_in(chars);
        let model = opennotebook_session::settings::notes_model().await;
        let price = crate::estimate_live::price_of(&model).await;
        let one = price.map_or(0.0, |(p_in, p_out)| {
            input_tokens as f64 * p_in + OUTPUT_TOKENS as f64 * p_out
        });
        Ok(NotesEstimateOutput {
            sources: docs.len() as _,
            chars: chars as _,
            model,
            input_tokens: input_tokens as _,
            output_tokens: OUTPUT_TOKENS as _,
            cost_usd: one,
            cost_high_usd: 2.0 * one,
            priced: price.is_some(),
        })
    }

    async fn notes_list(
        &self,
        _ctx: &Ctx,
        input: NotesListInput,
    ) -> Result<NotesListOutput, RpcError> {
        let sid = create::safe_sid(&input.sid).map_err(bad_request)?;
        let notes = filestore::list(&notes_root(), sid)
            .into_iter()
            .map(summary)
            .collect();
        Ok(NotesListOutput { notes })
    }

    async fn notes_list_all(
        &self,
        _ctx: &Ctx,
        _input: NotesListAllInput,
    ) -> Result<NotesListAllOutput, RpcError> {
        let notes = filestore::list_all(&notes_root())
            .into_iter()
            .map(summary)
            .collect();
        Ok(NotesListAllOutput { notes })
    }

    async fn notes_get(
        &self,
        _ctx: &Ctx,
        input: NotesGetInput,
    ) -> Result<NotesGetOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        match filestore::load::<StudyNotes>(&notes_root(), sid, id) {
            Some(n) => flat(&n),
            None => Err(bad_request(format!(
                "collection `{sid}` has no study notes `{id}`"
            ))),
        }
    }

    async fn notes_delete(
        &self,
        _ctx: &Ctx,
        input: NotesDeleteInput,
    ) -> Result<NotesDeleteOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        let removed =
            filestore::remove(&filestore::path(&notes_root(), sid, id)).map_err(internal)?;
        Ok(NotesDeleteOutput { value: removed })
    }

    /// Rename a set of notes: the title only, as `mindmap_retitle` does. The
    /// notes' text and their Markdown export keep the heading they were
    /// written with.
    async fn notes_retitle(
        &self,
        _ctx: &Ctx,
        input: NotesRetitleInput,
    ) -> Result<NotesRetitleOutput, RpcError> {
        let req = input.req;
        let sid = create::safe_sid(&req.sid).map_err(bad_request)?;
        let id = safe_id(&req.id).map_err(bad_request)?;
        let title = req.title.trim();
        if title.is_empty() {
            return Err(bad_request(
                "a study notes title cannot be empty: nothing in the list would name them",
            ));
        }
        let renamed = filestore::retitle(&filestore::path(&notes_root(), sid, id), title)
            .map_err(internal)?;
        if renamed {
            crate::collection::touched(sid).await;
        }
        Ok(NotesRetitleOutput { value: renamed })
    }
}

/// Notes as the list shows them.
fn summary(n: StudyNotes) -> StudyNotesSummary {
    StudyNotesSummary {
        id: n.id,
        sid: n.sid,
        title: n.title,
        focus: n.focus,
        created_ms: n.created_ms,
        sources: n.sources,
        ideas: n.ideas.len() as _,
        questions: n.quiz.len() as _,
        terms: n.glossary.len() as _,
        headings: n.ideas.into_iter().map(|i| i.heading).collect(),
    }
}

/// Notes as a method's output: the macro flattens a returned struct into the
/// output type, so the conversion goes through the JSON both serialise to, as
/// `mindmap_impl::flat` does.
fn flat<T: serde::de::DeserializeOwned>(n: &StudyNotes) -> Result<T, RpcError> {
    serde_json::to_value(n)
        .and_then(serde_json::from_value)
        .map_err(|e| internal(format!("study notes output: {e}")))
}

/// Tokens into one call for `chars` of source text: the text at four
/// characters a token, capped where the writer switches to excerpts, plus
/// about 1,200 for the prompt and one passage label per 900 characters.
fn tokens_in(chars: u64) -> u64 {
    let sent = chars.min(sn::WHOLE_TEXT_CHARS as u64);
    sent.div_ceil(4) + 1_200 + sent.div_ceil(900) * 6
}

// ── the store ───────────────────────────────────────────────────────────────

pub(crate) fn notes_root() -> PathBuf {
    opennotebook_session::paths::data_dir().join("notes")
}

/// A notes id is a canonical UUID; anything else is refused before it reaches
/// a path, so an id can never walk out of its sid's directory.
fn safe_id(id: &str) -> Result<&str, String> {
    if uuid::Uuid::try_parse(id).is_ok_and(|u| u.hyphenated().to_string() == id) {
        Ok(id)
    } else {
        Err(format!("`{id}` is not a study notes id"))
    }
}

fn internal(e: impl std::fmt::Display) -> RpcError {
    RpcError::internal(e.to_string())
}

fn bad_request(e: impl std::fmt::Display) -> RpcError {
    RpcError::invalid_params(e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("hs_notes_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    fn sample(id: &str, sid: &str, created_ms: u64) -> StudyNotes {
        StudyNotes {
            id: id.into(),
            sid: sid.into(),
            title: "Moshi".into(),
            focus: String::new(),
            sources: vec!["a.md".into()],
            excerpted: false,
            model: "m".into(),
            created_ms: created_ms as _,
            dropped: 0,
            unchecked: false,
            overview: "Moshi is a dialogue model [1].".into(),
            ideas: vec![NoteIdea {
                heading: "Latency".into(),
                body: "200 ms [1].".into(),
            }],
            quiz: vec![NoteQuestion {
                question: "Why?".into(),
                answer: "Because [1].".into(),
            }],
            essays: vec!["Discuss.".into()],
            glossary: vec![NoteTerm {
                term: "Mimi".into(),
                definition: "A codec [1].".into(),
            }],
            citations: vec![NoteCitation {
                n: 1,
                name: "a.md".into(),
                title: "Moshi".into(),
                url: String::new(),
                excerpt: "Moshi …".into(),
            }],
            markdown: "# Moshi\n".into(),
        }
    }

    const A: &str = "6f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11";
    const B: &str = "7f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11";
    const C: &str = "8f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11";

    /// The build ingests every file in a collection's staging directory, so notes
    /// stored anywhere under it would become a source of the session.
    #[test]
    fn notes_are_never_stored_where_the_build_reads_sources() {
        let staging = create::staging_dir("s1790947153376");
        let notes = notes_root().join("s1790947153376");
        assert!(
            !notes.starts_with(&staging),
            "{notes:?} is inside {staging:?}"
        );
        assert!(!staging.starts_with(&notes));
    }

    #[test]
    fn saved_notes_read_back_whole_and_list_newest_first() {
        let root = temp_root("roundtrip");
        filestore::save(&root, &sample(A, "s1", 100)).unwrap();
        filestore::save(&root, &sample(C, "s1", 300)).unwrap();
        filestore::save(&root, &sample(B, "s2", 200)).unwrap();
        std::fs::write(root.join("s1/broken.json"), b"{ not json").unwrap();
        let back = filestore::load::<StudyNotes>(&root, "s1", A).unwrap();
        assert_eq!(
            serde_json::to_value(&back).unwrap(),
            serde_json::to_value(sample(A, "s1", 100)).unwrap()
        );
        let ids: Vec<String> = filestore::list::<StudyNotes>(&root, "s1")
            .into_iter()
            .map(|n| n.id)
            .collect();
        assert_eq!(ids, [C, A]);
        let all: Vec<String> = filestore::list_all::<StudyNotes>(&root)
            .into_iter()
            .map(|n| n.id)
            .collect();
        assert_eq!(all, [C, B, A]);
        assert!(
            !std::fs::read_dir(root.join("s1"))
                .unwrap()
                .flatten()
                .any(|e| e.path().extension().is_some_and(|x| x == "tmp")),
            "a temporary file was left behind"
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn an_id_cannot_leave_its_directory() {
        assert!(safe_id(A).is_ok());
        for bad in [
            "",
            "../x",
            "m123",
            "6F1C2A3E-8B4D-4C1F-9A2E-0D3B5C7E9F11",
            "a/b",
        ] {
            assert!(safe_id(bad).is_err(), "{bad}");
        }
    }

    /// Both outputs are the notes field for field; a field added to one and
    /// not the other fails here.
    #[test]
    fn a_rename_leaves_the_notes_text_alone() {
        let root = temp_root("retitle");
        filestore::save(&root, &sample(A, "s1", 100)).unwrap();
        let path = filestore::path(&root, "s1", A);
        assert!(filestore::retitle(&path, "Moshi, briefly").unwrap());
        let back = filestore::load::<StudyNotes>(&root, "s1", A).unwrap();
        assert_eq!(back.title, "Moshi, briefly");
        let mut want = sample(A, "s1", 100);
        want.title = "Moshi, briefly".into();
        assert_eq!(
            serde_json::to_value(&back).unwrap(),
            serde_json::to_value(&want).unwrap(),
            "only the title changed; the export keeps its heading"
        );
        assert!(!filestore::retitle(&filestore::path(&root, "s1", B), "x").unwrap());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn notes_flatten_into_both_outputs() {
        let n = sample(A, "s1", 100);
        let got: NotesGetOutput = flat(&n).unwrap();
        assert_eq!(
            serde_json::to_value(&got).unwrap(),
            serde_json::to_value(&n).unwrap()
        );
        let made: NotesCreateOutput = flat(&n).unwrap();
        assert_eq!(made.citations[0].n, 1);
    }

    #[test]
    fn a_summary_counts_the_sections() {
        let s = summary(sample(A, "s1", 100));
        assert_eq!((s.ideas, s.questions, s.terms), (1, 1, 1));
        assert_eq!(s.headings, ["Latency"]);
    }

    #[test]
    fn the_estimate_counts_the_text_sent_not_the_text_staged() {
        // The 189,870 byte collection: about 47.5k tokens of text, the prompt, and
        // a label for each of about 211 passages.
        assert_eq!(tokens_in(189_870), 47_468 + 1_200 + 211 * 6);
        let cap = tokens_in(sn::WHOLE_TEXT_CHARS as u64);
        assert_eq!(tokens_in(5_000_000), cap);
    }
}
