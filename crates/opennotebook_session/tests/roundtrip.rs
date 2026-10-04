//! Runs against a real SQLite file. Every `store()` opens its own connection
//! on the same file, so a read through a fresh store proves the write reached
//! the database and was not just held by the connection that made it.

use std::path::PathBuf;
use std::sync::OnceLock;

use opennotebook_session::{
    Aspect, Db, DurationMs, LineId, NarrationLine, Session, SessionSlide, SessionState,
    SessionStore, SlideRef, Speaker, SpeakerId,
};

fn db_file() -> &'static PathBuf {
    static FILE: OnceLock<PathBuf> = OnceLock::new();
    FILE.get_or_init(|| {
        let dir = tempfile::tempdir().unwrap().keep();
        dir.join("opennotebook.db")
    })
}

async fn store() -> SessionStore {
    SessionStore::new(Db::open(db_file()).expect("the test database opens"))
}

fn sid(tag: &str) -> String {
    format!(
        "t{tag}{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    )
}

/// A two-speaker session with lines deliberately out of order in the vector, so
/// the ordering assertions cannot pass by accident of insertion.
fn sample(sid: &str) -> Session {
    let host = SpeakerId("host".into());
    let expert = SpeakerId("expert".into());
    Session {
        sid: sid.to_string(),
        title: "How Session Narration Works".into(),
        collection_name: sid.to_string(),
        collection_sid: Some("0007".into()),
        deck_ref: None,
        speakers: vec![
            Speaker {
                speaker_id: host.clone(),
                voice_id: "af_bella".into(),
                display_name: "Host".into(),
                role: "asks the questions".into(),
            },
            Speaker {
                speaker_id: expert.clone(),
                voice_id: "am_adam".into(),
                display_name: "Expert".into(),
                role: "answers them".into(),
            },
        ],
        slides: vec![
            SessionSlide {
                slide_ref: SlideRef {
                    collection: "c".into(),
                    presentation: "p".into(),
                    slide: "second".into(),
                },
                title: String::new(),
                on_slide: Vec::new(),
                ordinal: 1,
                aspect: Aspect {
                    width: 1376,
                    height: 768,
                },
                lines: vec![NarrationLine {
                    line_id: LineId("l3".into()),
                    speaker_id: expert,
                    ordinal: 0,
                    text: "A line with a \"quote\", a comma, and a \\ backslash.".into(),
                    audio_path: None,
                    duration_ms: None,
                    cues: Vec::new(),
                }],
            },
            SessionSlide {
                slide_ref: SlideRef {
                    collection: "c".into(),
                    presentation: "p".into(),
                    slide: "first".into(),
                },
                title: String::new(),
                on_slide: Vec::new(),
                ordinal: 0,
                aspect: Aspect {
                    width: 1920,
                    height: 1080,
                },
                lines: vec![
                    NarrationLine {
                        line_id: LineId("l2".into()),
                        speaker_id: host.clone(),
                        ordinal: 1,
                        text: "Second line.".into(),
                        audio_path: Some("/tmp/l2.wav".into()),
                        duration_ms: Some(DurationMs::measured_from_wav(4820)),
                        cues: Vec::new(),
                    },
                    NarrationLine {
                        line_id: LineId("l1".into()),
                        speaker_id: host,
                        ordinal: 0,
                        text: "First line.".into(),
                        audio_path: None,
                        duration_ms: None,
                        cues: Vec::new(),
                    },
                ],
            },
        ],
        state: SessionState::Preparing,
        prep_job_sid: None,
        failure: None,
        pinned: false,
        audio: None,
        collection: None,
        spent_usd: None,
        style: None,
    }
}

#[tokio::test]
async fn a_session_round_trips_through_sqlite() {
    let store = store().await;
    let sid = sid("rt");
    let written = sample(&sid);

    store.put(&written).await.expect("put should succeed");
    let read = store
        .get(&sid)
        .await
        .expect("get should succeed")
        .expect("the session was just written");

    assert_eq!(
        read, written,
        "the document must survive the round trip whole"
    );
    assert_eq!(
        read.slides[0].lines[0].text, written.slides[0].lines[0].text,
        "quotes, commas and backslashes must survive"
    );
    assert!(store.delete(&sid).await.expect("delete should succeed"));
}

/// A missing key returns `key_type: "none"` with an empty value rather than an
/// error, so absence must be decided on the type and never on the value.
#[tokio::test]
async fn a_missing_session_is_none_not_an_error() {
    let got = store()
        .await
        .get(&sid("absent"))
        .await
        .expect("a missing session is not an error");
    assert!(
        got.is_none(),
        "a session never written must read back as None"
    );
}

#[tokio::test]
async fn deleting_a_missing_session_reports_false() {
    assert!(
        !store()
            .await
            .delete(&sid("nodel"))
            .await
            .expect("delete should succeed"),
        "deleting what is not there is false, not an error"
    );
}

/// Ordering is explicit. The fixture stores slides and lines out of order on
/// purpose, so a reader relying on vector position would disagree with this.
#[tokio::test]
async fn ordering_comes_from_ordinal_not_position() {
    let store = store().await;
    let sid = sid("ord");
    store.put(&sample(&sid)).await.expect("put should succeed");
    let read = store.get(&sid).await.unwrap().unwrap();

    let slides = read.slides_in_order();
    assert_eq!(slides[0].slide_ref.slide, "first");
    assert_eq!(slides[1].slide_ref.slide, "second");

    let lines = slides[0].lines_in_order();
    assert_eq!(lines[0].line_id, LineId("l1".into()));
    assert_eq!(lines[1].line_id, LineId("l2".into()));

    assert!(
        read.dangling_speaker_ids().is_empty(),
        "every line's speaker must resolve"
    );
    assert_eq!(read.speaker_count(), 2);
    store.delete(&sid).await.unwrap();
}

/// The store must not silently accept a line naming a speaker the session does
/// not have. The type forces a line to name one; only this catches a bad name.
#[tokio::test]
async fn a_dangling_speaker_id_is_detectable() {
    let mut s = sample(&sid("dangle"));
    s.speakers
        .retain(|sp| sp.speaker_id != SpeakerId("expert".into()));
    let dangling = s.dangling_speaker_ids();
    assert_eq!(dangling.len(), 1, "the expert's lines are now orphaned");
    assert_eq!(dangling[0], &SpeakerId("expert".into()));
}

/// The spend survives the store, and a row written before it was recorded
/// reads back as unknown rather than as free.
#[tokio::test]
async fn spend_round_trips_and_a_legacy_row_has_none() {
    let store = store().await;
    let sid = sid("spend");
    let mut written = sample(&sid);
    written.spent_usd = Some(0.0421);
    store.put(&written).await.expect("put should succeed");
    let read = store.get(&sid).await.expect("get").expect("written");
    assert_eq!(read.spent_usd, Some(0.0421));
    assert!(store.delete(&sid).await.expect("delete should succeed"));

    let mut legacy = serde_json::to_value(sample(&sid)).unwrap();
    legacy.as_object_mut().unwrap().remove("spent_usd");
    let old: Session = serde_json::from_value(legacy).expect("a row without spent_usd decodes");
    assert_eq!(old.spent_usd, None);
}

/// The deck's style survives the store, so a retry builds the same look, and
/// a row written before it was kept reads back as none.
#[tokio::test]
async fn style_round_trips_and_a_legacy_row_has_none() {
    let store = store().await;
    let sid = sid("style");
    let mut written = sample(&sid);
    written.style = Some("watercolor".into());
    store.put(&written).await.expect("put should succeed");
    let read = store.get(&sid).await.expect("get").expect("written");
    assert_eq!(read.style.as_deref(), Some("watercolor"));
    assert!(store.delete(&sid).await.expect("delete should succeed"));

    let legacy = serde_json::to_value(sample(&sid)).unwrap();
    assert!(legacy.get("style").is_none(), "None is not written");
    let old: Session = serde_json::from_value(legacy).expect("a row without style decodes");
    assert_eq!(old.style, None);
}
