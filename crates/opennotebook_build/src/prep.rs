//! Preparing a session: slides and audio together, then the check, then the
//! state that says it is ready.
//!
//! Partial failure is not rolled back. Section 1 rolls its ingest back because
//! a half-imported collection is invisible: it looks present, answers queries,
//! and nothing on it records that it is incomplete. A session is the opposite.
//! It carries `state`, so an incomplete one can say so, and what it holds is
//! expensive: a model call per four slides and 4.4 to 5.7 seconds of synthesis
//! per line, all of which a rollback would throw away.
//!
//! So the rule here is narrower than section 1's and it is the whole guarantee:
//! `Ready` is written once, after validation, and on no other path. Everything
//! else is written as `Preparing` or `Failed`. The store is written before the
//! slides are drawn and again after, so a process that dies mid-prep leaves a
//! record of what exists rather than files nothing points at.
//!
//! Resuming from a `Failed` session is deliberately not built. The shape above
//! is what makes it possible later — the deck ref and the lines that already
//! have audio are all persisted — but phase 1 re-runs, and a resume that has
//! never been exercised is a guarantee nobody has watched hold.

use std::path::Path;

use futures_util::future::BoxFuture;
use opennotebook_session::{Aspect, DeckRef, Session, SessionState, SessionStore};

use crate::error::BuildError;
use crate::job::PrepJob;
use crate::kits::Kit;
use crate::narrate;
use crate::slides;
use crate::validate;

/// The phases THIS function reports, against the total the caller owns: the
/// slides and the voices (made at the same time, so reported as one), then the
/// check.
///
/// It is deliberately not the whole prep. §3's pipeline also ingests and
/// generates a script before any of this, and those took minutes during which
/// `steps_total` sat at 0 — the job row's way of saying "this job does not
/// report" — so the caller owns the total and this reports its own phases
/// against it, which is why `build_session` takes a `PrepJob`.
pub const BUILD_PHASES: [&str; 2] = ["deck", "validate"];

#[derive(Debug, Clone)]
pub struct PrepOutcome {
    pub session: Session,
    /// The style the deck was drawn in.
    pub style: String,
}

/// Where the session's artefacts go and how its slides are drawn.
pub struct Placement<'a> {
    /// The session's deck directory; slides are written under it.
    pub deck_dir: &'a Path,
    /// The style kit and how the style is described to the slide model.
    pub style: &'static Kit,
    pub style_label: &'a str,
    pub style_brief: &'a str,
    /// The model that writes the slides.
    pub slide_model: &'a str,
    /// A sentence fixing the language of anything the model adds, or empty.
    pub language_rule: &'a str,
    /// One WAV per line lands here.
    pub audio_dir: &'a Path,
    /// False for an audio overview: the narration is the whole of it, and no
    /// deck is written.
    pub write_slides: bool,
    /// Asked before every write of the session row. `false` means the thing
    /// this session was made for is gone, and the row is not written: see
    /// [`BuildError::Abandoned`]. `None` writes unconditionally.
    pub wanted: Option<Wanted<'a>>,
}

/// Whether the session being built is still wanted. A closure rather than a
/// flag because the answer can change during the minutes a build takes, and
/// the store that knows it is the caller's.
pub type Wanted<'a> = &'a (dyn Fn() -> BoxFuture<'a, bool> + Send + Sync);

/// Build the slides and the audio the session's script describes.
///
/// `session` arrives from the script slice with its slides and speakers filled
/// in and no media. It comes back with a written deck, an audio path and a
/// measured duration per line, and a state. `job` is the job row this
/// prep runs as, already carrying the phases that ran before this one.
pub async fn build_session(
    store: &SessionStore,
    mut session: Session,
    place: Placement<'_>,
    job: &mut PrepJob,
) -> Result<PrepOutcome, BuildError> {
    session.prep_job_sid = Some(job.sid().to_string());
    session.state = SessionState::Preparing;
    persist(store, &session, &place).await?;

    let built = run(store, &mut session, &place, job).await;
    // Abandoned is not a failure to record: there is no row to record it on,
    // and writing one would undo the delete. The job is left alone too — it
    // was cancelled by whoever deleted the session, or ends when this
    // process exits cleanly.
    if let Err(e @ BuildError::Abandoned { .. }) = built {
        return Err(e);
    }

    // `Ready` is written here and nowhere else in this crate.
    session.state = match &built {
        Ok(_) => SessionState::Ready,
        Err(_) => SessionState::Failed,
    };
    session.failure = built.as_ref().err().map(|e| match job.sid() {
        "" => e.to_string(),
        sid => format!("{e} (prep job {sid})"),
    });
    // Everything the prep paid for is in by now: the script before this was
    // called, the slides inside it. Outside a spend scope this stays as it was.
    if let Some(spent) = opennotebook_session::spend::current() {
        session.spent_usd = spent.known_usd();
    }
    let stored = persist(store, &session, &place).await;
    if let Err(e @ BuildError::Abandoned { .. }) = stored {
        return Err(e);
    }
    let _ = job.finish(built.as_ref().map(|_| ())).await;

    built?;
    stored?;
    Ok(PrepOutcome {
        session,
        style: place.style.id.to_string(),
    })
}

async fn run(
    store: &SessionStore,
    session: &mut Session,
    place: &Placement<'_>,
    job: &mut PrepJob,
) -> Result<(), BuildError> {
    job.phase(BUILD_PHASES[0]).await?;
    let collection = session.sid.clone();
    let title = session.title.clone();
    let plan = slides::DeckPlan {
        title: &title,
        style: place.style,
        style_label: place.style_label,
        style_brief: place.style_brief,
        model: place.slide_model,
        language_rule: place.language_rule,
        deck_dir: place.deck_dir,
        collection: &collection,
    };

    // The slides and the voices need nothing from each other: the slides are
    // drawn from the script's copy, the audio from its lines. So they run at
    // the same time, and a build takes as long as the slower of the two
    // rather than both.
    let deck_slides = session.slides.clone();
    let speakers = session.speakers.clone();
    let voice = narrate::connect().await?;
    if !place.write_slides {
        narrate::synthesise_all(&voice, &speakers, &mut session.slides, place.audio_dir).await?;
        eprintln!(
            "opennotebook prep: audio overview narrated, {} chapters, {} lines",
            session.slides.len(),
            session.slides.iter().map(|s| s.lines.len()).sum::<usize>()
        );
        job.phase_done(BUILD_PHASES[0]).await?;
        persist(store, session, place).await?;
        job.phase(BUILD_PHASES[1]).await?;
        validate::narration_is_playable(session)?;
        job.phase_done(BUILD_PHASES[1]).await?;
        return Ok(());
    }
    let (deck, voiced) = tokio::join!(
        slides::write_deck(&plan, &deck_slides),
        narrate::synthesise_all(&voice, &speakers, &mut session.slides, place.audio_dir),
    );
    let deck = deck?;
    voiced?;
    eprintln!(
        "opennotebook prep: {} slides written on {} ({} drawn plain), {}k characters in, {}k out",
        deck.refs.len(),
        place.slide_model,
        deck.fallbacks,
        deck.chars_in / 1000,
        deck.chars_out / 1000
    );
    for r in deck.refs {
        if let Some(s) = session
            .slides
            .iter_mut()
            .find(|s| s.slide_ref.slide == r.slide)
        {
            s.slide_ref = r;
            // The kit fixes every slide at 1920x1080; there is nothing to measure.
            s.aspect = Aspect {
                width: 1920,
                height: 1080,
            };
        }
    }
    session.deck_ref = Some(DeckRef {
        collection: collection.clone(),
        presentation: slides::PRESENTATION.to_string(),
    });
    job.phase_done(BUILD_PHASES[0]).await?;
    persist(store, session, place).await?;

    job.phase(BUILD_PHASES[1]).await?;
    validate::narration_is_playable(session)?;
    validate::slides_are_written(session, place.deck_dir)?;
    job.phase_done(BUILD_PHASES[1]).await?;
    Ok(())
}

async fn persist(
    store: &SessionStore,
    session: &Session,
    place: &Placement<'_>,
) -> Result<(), BuildError> {
    if let Some(wanted) = place.wanted
        && !wanted().await
    {
        return Err(BuildError::Abandoned {
            sid: session.sid.clone(),
        });
    }
    store
        .put(session)
        .await
        .map_err(|source| BuildError::Store { source })
}

pub fn phases() -> &'static [&'static str] {
    &BUILD_PHASES
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_phase_names_are_what_steps_total_counts() {
        assert_eq!(phases().len(), BUILD_PHASES.len());
        assert!(
            !BUILD_PHASES.is_empty(),
            "steps_total of zero means `does not report`, not `no phases`"
        );
    }

    /// A session whose collection was deleted writes nothing at all: not the
    /// first `Preparing` row, not a `Failed` one, and the job is not touched.
    /// The store and the job row are checked afterwards: no session row, and
    /// the job row exactly as it was seeded.
    #[tokio::test]
    async fn an_abandoned_session_writes_nothing() {
        let dir = std::env::temp_dir().join(format!("hs_abandon_{}", std::process::id()));
        let db = opennotebook_session::Db::open_in_memory().unwrap();
        db.call("seed", |c| {
            c.execute(
                "INSERT INTO jobs (id, sid, status, created_ms, updated_ms)
                 VALUES ('job1', 's1', 'pending', 1, 1)",
                [],
            )
        })
        .await
        .unwrap();
        let store = SessionStore::new(db.clone());
        let mut job = PrepJob::detached(db.clone(), "job1", 4);
        let session: Session = serde_json::from_value(serde_json::json!({
            "sid": "s1", "title": "t", "collection_name": "s1", "collection_sid": null,
            "deck_ref": null, "speakers": [], "slides": [], "state": "preparing",
            "prep_job_sid": null, "collection": "c1"
        }))
        .unwrap();
        let asked = std::sync::atomic::AtomicUsize::new(0);
        let gone = || {
            asked.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Box::pin(async { false }) as BoxFuture<'_, bool>
        };
        let kit = crate::kits::kit(opennotebook_sdk::styles::DEFAULT_STYLE.id).unwrap();
        let got = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            build_session(
                &store,
                session,
                Placement {
                    deck_dir: &dir,
                    style: kit,
                    style_label: "",
                    style_brief: "",
                    slide_model: "",
                    language_rule: "",
                    audio_dir: &dir,
                    write_slides: true,
                    wanted: Some(&gone),
                },
                &mut job,
            ),
        )
        .await
        .expect("an abandoned build makes no call");
        assert!(
            matches!(got, Err(BuildError::Abandoned { ref sid }) if sid == "s1"),
            "{:?}",
            got.err()
        );
        assert_eq!(asked.load(std::sync::atomic::Ordering::SeqCst), 1);
        assert!(!dir.exists(), "nothing was drawn or voiced");
        assert!(store.list().await.unwrap().is_empty(), "no session row");
        let job_row = crate::job::get_on(&db, "job1").await.unwrap().unwrap();
        assert_eq!(
            (job_row.status.as_str(), job_row.steps_total),
            ("pending", 0),
            "the job row was not touched"
        );
    }
}
