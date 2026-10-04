//! These tests seed their own material through `ingest_resources`, which is the
//! only thing allowed to write the retrieval store.

mod common;

use std::time::Duration;

use common::{WORKSPACE, client, dimensions, qa, session};
use opennotebook_ingest::{IngestError, SourceFile, ingest_resources, prove_retrievable};

fn file(name: &str, bytes: &'static [u8]) -> SourceFile {
    SourceFile {
        name: name.to_string(),
        bytes: bytes.to_vec(),
    }
}

fn docx() -> SourceFile {
    file("narration.docx", include_bytes!("fixtures/verbatim.docx"))
}

fn pdf() -> SourceFile {
    file("narration.pdf", include_bytes!("fixtures/verbatim.pdf"))
}

/// An image-only PDF: a real page rasterised and rebuilt with no text layer.
/// `pdftotext` reads 0 bytes from it against 7085 from the document it came from.
fn scanned() -> SourceFile {
    file("scanned.pdf", include_bytes!("fixtures/scanned.pdf"))
}

#[tokio::test]
async fn a_pdf_and_a_docx_land_in_both_stores() {
    let (sid, root) = session("both");
    let out = ingest_resources(
        &client().await,
        &qa().await,
        WORKSPACE,
        &root,
        &sid,
        &[docx(), pdf()],
        &dimensions(),
    )
    .await
    .expect("ingest should succeed");

    assert_eq!(out.converted.len(), 2, "both files should convert");
    assert!(out.proof.search_hits > 0, "search door empty after ingest");
    assert!(out.proof.qa_pairs > 0, "q&a door empty after ingest");
    assert_eq!(
        out.collection, sid,
        "the collection is named after its directory"
    );
    // Both fixtures are `narration.*`, so both become `narration.md` and the
    // second replaces the first: one document's pairs, per dimension.
    assert_eq!(out.proof.qa_pairs, 2, "one pair per document and dimension");
    assert!(out.session_dir.join("narration.md").exists());
}

/// The refusal path. An image-only PDF has no text layer, and OCR into a
/// grounding store would be quoted back as if it were the document.
#[tokio::test]
async fn a_scan_is_refused_not_ocred() {
    let (sid, root) = session("scan");
    let err = ingest_resources(
        &client().await,
        &qa().await,
        WORKSPACE,
        &root,
        &sid,
        &[scanned()],
        &dimensions(),
    )
    .await
    .expect_err("a scan must be refused");

    match err {
        IngestError::NoExtractableText { ref name, bytes } => {
            assert_eq!(name, "scanned.pdf");
            assert!(bytes < 80, "expected under the 80 byte floor, got {bytes}");
        }
        other => panic!("expected NoExtractableText, got {other}"),
    }
}

/// Partial failure must not leave a collection that half exists. One bad file
/// among good ones refuses the whole ingest, and nothing reaches the store.
#[tokio::test]
async fn one_bad_file_refuses_the_whole_ingest() {
    let (sid, root) = session("part");
    let err = ingest_resources(
        &client().await,
        &qa().await,
        WORKSPACE,
        &root,
        &sid,
        &[docx(), scanned(), pdf()],
        &dimensions(),
    )
    .await
    .expect_err("a batch containing a scan must be refused");

    assert!(
        matches!(err, IngestError::NoExtractableText { .. }),
        "expected the scan to be the reason, got {err}"
    );

    // Nothing was written, so the proof cannot pass on this collection.
    let proof = prove_retrievable(
        &client().await,
        WORKSPACE,
        &sid,
        "narration engine",
        Duration::ZERO,
    )
    .await;
    assert!(
        matches!(proof, Err(IngestError::SearchDoorEmpty { .. })),
        "a refused ingest must leave no collection behind, got {proof:?}"
    );
}

#[tokio::test]
async fn an_unknown_extension_is_refused() {
    let (sid, root) = session("ext");
    let err = ingest_resources(
        &client().await,
        &qa().await,
        WORKSPACE,
        &root,
        &sid,
        &[file("notes.rtf", b"not an office format")],
        &dimensions(),
    )
    .await
    .expect_err("an unsupported extension must be refused");

    assert!(
        matches!(err, IngestError::UnsupportedFormat { .. }),
        "expected UnsupportedFormat, got {err}"
    );
}
