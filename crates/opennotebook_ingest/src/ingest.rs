//! The one ingest path. Nothing else writes a session's retrieval store; see
//! `docs/phase1-spec.md` section 1.
//!
//! Every file is converted before anything is written. A batch with one
//! unreadable file writes nothing at all, and a failure after the first write
//! removes the collection again, so a refusal can never leave passages indexed
//! with no Q&A pairs beside them.

use std::path::{Path, PathBuf};
use std::time::Duration;

use opennotebook_convert::{InputKind, to_markdown};
use opennotebook_memory::{Doc, Memory, QaModel};

use crate::error::IngestError;
use crate::proof::{RetrievalProof, prove_retrievable};

/// Fewer visible characters than this from a parsed document means there was
/// nothing to parse: a scan or an image-only export with no text layer. The
/// converter returns whatever little text such a file has rather than
/// refusing it, so this floor is where that becomes a refusal. A real page of
/// text clears it many times over.
const MIN_EXTRACTED_BYTES: usize = 80;

/// How long the closing proof retries a search that fails outright. With an
/// embedding endpoint configured, the query is embedded at read time and that
/// endpoint can be briefly unavailable.
const PROOF_WAIT: Duration = Duration::from_secs(30);

/// One resource as handed in. `name` carries the extension, which is what picks
/// the parser.
#[derive(Debug, Clone)]
pub struct SourceFile {
    pub name: String,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone)]
pub struct IngestResult {
    /// The collection the sources were indexed under: the session directory's
    /// basename.
    pub collection: String,
    pub session_dir: PathBuf,
    pub converted: Vec<String>,
    pub proof: RetrievalProof,
}

struct Converted {
    stem: String,
    markdown: String,
}

/// Extensions whose bytes are already the document.
fn is_already_text(name: &str) -> bool {
    matches!(
        Path::new(name)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "md" | "markdown" | "txt" | "text"
    )
}

fn kind_of(name: &str) -> Result<InputKind, IngestError> {
    let extension = Path::new(name)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    InputKind::from_extension(&extension).ok_or_else(|| IngestError::UnsupportedFormat {
        name: name.to_string(),
        extension,
    })
}

/// Convert one file, verbatim. The converter has no model in it, so no model
/// ever sees the document.
fn convert(file: &SourceFile) -> Result<Converted, IngestError> {
    // Text that is already text. The converter handles docx, xlsx, pptx and pdf
    // and knows nothing else, so before this the studio could not ingest a note
    // somebody typed, a README, or a web page that had been reduced to prose —
    // the three things a person is most likely to hand it. Passing them through
    // is not a conversion and must not pretend to be one: there is no parser in
    // the path, and the bytes are the document.
    let markdown = if is_already_text(&file.name) {
        String::from_utf8(file.bytes.clone()).map_err(|e| IngestError::Convert {
            name: file.name.clone(),
            reason: format!("not valid UTF-8: {e}"),
        })?
    } else {
        let kind = kind_of(&file.name)?;
        to_markdown(&file.bytes, kind).map_err(|e| IngestError::Convert {
            name: file.name.clone(),
            reason: e.to_string(),
        })?
    };

    let visible = markdown.chars().filter(|c| !c.is_whitespace()).count();
    // The threshold catches ONE thing: a PDF or an office document that came
    // back nearly empty, which means a scan with no text layer. It is a floor on
    // what a PARSER produced, and its refusal says "so it is a scan or an
    // image-only export".
    //
    // Applied to text somebody typed it is wrong twice over: there is no parser,
    // so nothing can have failed to extract, and a short note is a legitimate
    // source rather than a broken file. A typed sentence is still a source; an
    // empty one is not.
    let floor = if is_already_text(&file.name) {
        1
    } else {
        MIN_EXTRACTED_BYTES
    };
    if visible < floor {
        return Err(IngestError::NoExtractableText {
            name: file.name.clone(),
            bytes: visible,
        });
    }

    let stem = Path::new(&file.name)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("document")
        .to_string();
    Ok(Converted { stem, markdown })
}

/// The longest line of the first document, capped to twelve words. Taken from
/// what was just written rather than from a constant, so the proof cannot pass
/// against material that is not there.
fn probe_phrase(docs: &[Converted]) -> String {
    docs.first()
        .and_then(|d| {
            d.markdown
                .lines()
                .map(str::trim)
                .filter(|l| !l.starts_with('#') && !l.starts_with('<'))
                .max_by_key(|l| l.split_whitespace().count())
        })
        .map(|line| {
            line.split_whitespace()
                .take(12)
                .collect::<Vec<_>>()
                .join(" ")
        })
        .unwrap_or_default()
}

/// Convert the files, write them into the session directory, index the same
/// text, extract Q&A pairs from it, and prove both serve it.
///
/// `dimensions` selects what extraction pulls out. It has no default on
/// purpose: which dimensions a session extracts on is a product decision, and
/// picking one silently would bury it in a helper.
pub async fn ingest_resources(
    memory: &Memory,
    qa: &QaModel,
    workspace: &str,
    sessions_root: &Path,
    session_sid: &str,
    files: &[SourceFile],
    dimensions: &[String],
) -> Result<IngestResult, IngestError> {
    // Everything converts before anything is written.
    let docs = files.iter().map(convert).collect::<Result<Vec<_>, _>>()?;

    let session_dir = sessions_root.join(session_sid);
    let io = |source| IngestError::SessionDir {
        path: session_dir.clone(),
        source,
    };
    std::fs::create_dir_all(&session_dir).map_err(io)?;
    for doc in &docs {
        std::fs::write(session_dir.join(format!("{}.md", doc.stem)), &doc.markdown).map_err(io)?;
    }

    let collection = session_dir
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(session_sid)
        .to_string();

    // Everything from the first write on is undone on failure, because
    // passages without their Q&A pairs, or pairs without passages, are the
    // half-written state this module exists to make impossible.
    match write_and_prove(memory, qa, workspace, &collection, &docs, dimensions).await {
        Ok(proof) => Ok(IngestResult {
            collection,
            session_dir,
            converted: docs.into_iter().map(|d| d.stem).collect(),
            proof,
        }),
        Err(e) => {
            // Best effort: the failure being returned is the one worth
            // reporting.
            let _ = memory.delete_collection(workspace, &collection).await;
            Err(e)
        }
    }
}

async fn write_and_prove(
    memory: &Memory,
    qa: &QaModel,
    workspace: &str,
    collection: &str,
    docs: &[Converted],
    dimensions: &[String],
) -> Result<RetrievalProof, IngestError> {
    let docs_in: Vec<Doc> = docs
        .iter()
        .map(|d| Doc {
            id: format!("{}.md", d.stem),
            text: d.markdown.clone(),
        })
        .collect();

    memory
        .index_add(workspace, collection, &docs_in)
        .await
        .map_err(|source| IngestError::Memory {
            call: "index_add",
            source,
        })?;

    memory
        .qa_extract(workspace, collection, &docs_in, dimensions, qa)
        .await
        .map_err(|source| IngestError::Memory {
            call: "qa_extract",
            source,
        })?;

    prove_retrievable(
        memory,
        workspace,
        collection,
        &probe_phrase(docs),
        PROOF_WAIT,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_and_markdown_pass_through_without_a_parser() {
        // Not `kind_of`: these deliberately have no InputKind, because there is
        // no conversion. The test is that `convert` accepts them and that the
        // bytes survive unchanged, which is what "verbatim" has to mean for the
        // grounding store.
        let body = "# Heading\n\nShort. Shorter than a parser floor would allow.";
        for name in ["note.md", "note.MARKDOWN", "note.txt", "note.Text"] {
            let out = convert(&SourceFile {
                name: name.to_string(),
                bytes: body.as_bytes().to_vec(),
            })
            .unwrap_or_else(|e| panic!("{name} refused: {e}"));
            assert_eq!(out.markdown, body, "{name} was altered on the way in");
        }
        // An empty note is still refused: the floor is one visible character,
        // not zero, because a source with nothing in it grounds nothing.
        assert!(matches!(
            convert(&SourceFile {
                name: "empty.md".into(),
                bytes: b"   \n\n  ".to_vec()
            }),
            Err(IngestError::NoExtractableText { .. })
        ));
        // Still refused, and still by name rather than by producing nothing.
        assert!(matches!(
            convert(&SourceFile {
                name: "a.rtf".into(),
                bytes: body.as_bytes().to_vec()
            }),
            Err(IngestError::UnsupportedFormat { .. })
        ));
    }

    #[test]
    fn extension_picks_the_parser_and_unknown_ones_are_refused() {
        assert!(matches!(kind_of("a.PDF"), Ok(InputKind::Pdf)));
        assert!(matches!(kind_of("a.docx"), Ok(InputKind::Word)));
        assert!(matches!(
            kind_of("a.rtf"),
            Err(IngestError::UnsupportedFormat { .. })
        ));
        assert!(matches!(
            kind_of("noextension"),
            Err(IngestError::UnsupportedFormat { .. })
        ));
    }

    #[test]
    fn the_probe_comes_from_the_document_and_skips_headings() {
        let docs = vec![Converted {
            stem: "d".into(),
            markdown:
                "# Heading\nshort\nthe narration engine attaches a spoken script to each slide\n"
                    .into(),
        }];
        let probe = probe_phrase(&docs);
        assert!(probe.starts_with("the narration engine"), "got {probe:?}");
        assert!(probe.split_whitespace().count() <= 12);
    }
}
