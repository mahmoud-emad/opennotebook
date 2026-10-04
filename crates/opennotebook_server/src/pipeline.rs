//! The prep pipeline: resources in, a `Ready` session out.
//!
//! This is §3's seven steps, joined for the first time. Sections 1, 3a and 3b
//! each built and tested their part; nothing until now called them in sequence.
//!
//! ```text
//! 1. ingest_resources()      seconds        §1
//! 2. outline                 one LLM call   §3a  ┐ generate_script
//! 3. script, per slide       one call each  §3a  ┘
//! 4. slides                  4 per call     §3b  ┐
//! 5. synthesis, per line     0.20-0.24x     §3b  │ build_session
//! 6. render validation       liveness only  §3b  │
//! 7. state -> Ready          once           §3b  ┘
//! ```
//!
//! Steps 4 to 7 already live together in `opennotebook_build::build_session`,
//! including the phase reporting, so this adds 1 to 3 in front of it and hands
//! over.

use std::path::{Path, PathBuf};

use opennotebook_build::BuildError;
use opennotebook_build::job::PrepJob;
use opennotebook_build::prep::{BUILD_PHASES, Placement};
use opennotebook_ingest::{SourceFile, ingest_resources};
use opennotebook_script::{ScriptSpec, generate_script};
use opennotebook_session::{Session, SessionState, Speaker, SpeakerId};

use crate::session::SessionPrepareReq;

/// The four dimensions a session extracts on. §0 fixes this set and says why:
/// of the seventeen available, these are the ones that describe what a document
/// *is* rather than what a work session *did*, plus `business` for pitch decks
/// and strategy memos.
///
/// It lives here, in the caller, on purpose. §0: "The parameter stays required
/// with no default. The studio passes the fixed set; it is not baked into the
/// function, so changing it is a change to a caller and not a code change in the
/// ingest path."
const DIMENSIONS: [&str; 4] = ["architecture", "technology", "product", "business"];

/// The retrieval namespace every session's collection lives in. Collections are
/// already namespaced by session sid, so one is enough; a per-user one becomes
/// the right answer the moment two people share an instance.
pub(crate) const WORKSPACE: &str = "opennotebook";

/// Every phase of a prep, in order, and the number a prep screen draws its bar
/// against.
///
/// The first two are this file's; the last four are `build_session`'s. They are
/// one list because they are one job: reporting only the last four left
/// `steps_total` at 0 through ingest and script generation, which is the job
/// row's way of saying "this job does not report" and is indistinguishable from a
/// job that reports nothing at all. §3 asks progress to be honest about which
/// stage is running, and for the first third of the wall clock it was not.
pub const PHASES: [&str; 5] = [
    "research",
    "ingest",
    "script",
    BUILD_PHASES[0],
    BUILD_PHASES[1],
];

/// Everything a prep writes to disk, under the studio's own var directory.
pub struct Layout {
    /// Session directories. Its basename becomes the memory collection.
    pub sessions_root: PathBuf,
    /// One directory per session for rendered WAVs.
    pub audio_root: PathBuf,
    /// One directory per session's deck, named after its sid. Studio-written
    /// slides sit in `<sid>/studio/`; decks built before the studio wrote its
    /// own slides keep the earlier `<sid>/session/<slide>/output/` layout,
    /// which is still served.
    pub decks_root: PathBuf,
}

impl Layout {
    pub fn in_data_dir() -> Self {
        let root = opennotebook_session::paths::data_dir();
        Self {
            sessions_root: root.join("sessions"),
            audio_root: root.join("audio"),
            decks_root: root.join("decks"),
        }
    }
}

/// Run one prep to completion.
///
/// `prep_job_sid` is the job row this runs as. It is passed
/// through to `build_session`, which adopts it — the same row, never a second.
pub async fn run(
    req: &SessionPrepareReq,
    prep_job_sid: &str,
    layout: &Layout,
) -> anyhow::Result<Session> {
    // Adopted before any work, so `steps_total` is non-zero from the first poll
    // rather than from the deck stage minutes later.
    let mut job = PrepJob::adopt(prep_job_sid, PHASES.len() as u32).await?;

    // An output deleted between the click and this process starting — a prep
    // queued behind another waits for its turn — or one whose collection was,
    // has nothing left to build into. Asked before research, which would
    // spend a minute writing into a staging directory that is gone.
    let collection = req.collection.clone().filter(|c| !c.is_empty());
    let store = crate::collection::open_store()
        .await
        .map_err(|e| anyhow::anyhow!(e))?;
    if !crate::collection::output_still_wanted(&store, &req.sid, collection.as_deref()).await {
        return Err(BuildError::Abandoned {
            sid: req.sid.clone(),
        }
        .into());
    }

    // Web research first, so what it finds is read by ingest like any other
    // source. Always a phase, run or not, so the progress a page draws has the
    // same seven steps whichever way the switch was set.
    job.phase(PHASES[0]).await?;
    if let Some(topic) = req
        .research_topic
        .as_deref()
        .map(str::trim)
        .filter(|t| !t.is_empty())
    {
        let depth = opennotebook_session::settings::research_depth().await;
        match crate::research::gather(topic, Path::new(&req.resource_dir), &depth, None).await {
            Ok(found) => eprintln!(
                "opennotebook prep: web research staged, {} chars from {} sources",
                found.chars, found.sources
            ),
            // Not fatal while the person gave us something of their own: the
            // session is built from that and the log says what was missing.
            // With nothing of theirs it is — see the check below.
            Err(e) => eprintln!("opennotebook prep: web research did not arrive: {e:#}"),
        }
    }
    job.phase_done(PHASES[0]).await?;

    let files = read_resources(Path::new(&req.resource_dir))?;
    anyhow::ensure!(
        !files.is_empty(),
        "no resources to ingest in {}: nothing was added and web research found \
         nothing, and a session with no source material would be narrated from nothing",
        req.resource_dir
    );

    let memory = opennotebook_memory::from_settings()
        .await
        .map_err(|e| anyhow::anyhow!("the retrieval store: {e}"))?;
    let qa_model = opennotebook_memory::qa_model_from_settings()
        .await
        .map_err(|e| anyhow::anyhow!("the extraction model: {e}"))?;

    // §1. One function, two writes, both proved by round trip before it returns.
    job.phase(PHASES[1]).await?;
    let dimensions: Vec<String> = DIMENSIONS.iter().map(|d| d.to_string()).collect();
    let ingested = ingest_resources(
        &memory,
        &qa_model,
        WORKSPACE,
        &layout.sessions_root,
        &req.sid,
        &files,
        &dimensions,
    )
    .await?;

    job.phase_done(PHASES[1]).await?;

    // The session's own deck directory, named after its sid: the slides are
    // written into it and served from it.
    let deck_dir = layout.decks_root.join(&req.sid);
    std::fs::create_dir_all(&deck_dir)?;

    // §3a. Outline plus one speaker-tagged script per slide, grounded in the
    // collection ingest just proved retrievable.
    let audio = crate::session_impl::audio_spec(
        req.audio_format.as_deref(),
        req.audio_length.as_deref(),
        req.focus.as_deref(),
    );
    // The parts and voices, by the same rule the spending limit priced: an
    // audio overview's format decides its chapters and voices (Brief is one
    // host).
    let shape = crate::session_impl::prep_shape(req);
    let mut speakers = speakers(req);
    speakers.truncate(shape.speakers as usize);
    let mut spec = ScriptSpec::new(
        req.title.clone(),
        speakers.clone(),
        (req.sid.as_str(), opennotebook_build::slides::PRESENTATION),
    );
    spec.slide_count = shape.slides as usize;
    // Read once, here, so the script and the deck are made under the same
    // settings even if someone changes them while this runs.
    let policy = opennotebook_session::settings::cost_policy().await;
    let language = opennotebook_session::settings::language().await;
    spec.language = language.clone();
    // An audio overview's length is its format's, not the session setting.
    let minutes = audio
        .as_ref()
        .map_or(policy.session_minutes, |a| a.minutes());
    spec.audio = audio.clone();
    spec.slide_narration = opennotebook_script::budget::slide_narration(minutes, spec.slide_count);
    // No `[image: …]` lines: there is no image model any more. The slide
    // writer draws each slide's figure itself, from the slide's copy.
    spec.images_per_slide = Some(0);
    let style = style(req);
    let kit = opennotebook_build::kits::kit(style.id)
        .or_else(|| opennotebook_build::kits::kit(opennotebook_sdk::styles::DEFAULT_STYLE.id))
        .expect("the default style has a kit");
    eprintln!(
        "opennotebook prep: {} minutes over {} slides ({} characters a slide), {} style, slides on {}",
        minutes, spec.slide_count, spec.slide_narration, style.id, policy.slide_model
    );
    job.phase(PHASES[2]).await?;
    let slides = generate_script(&memory, WORKSPACE, &ingested.collection, &spec).await?;
    job.phase_done(PHASES[2]).await?;

    // The row has existed since the prep was accepted (see `session_prepare`),
    // and the gallery could rename or pin it while this ran. Those are the
    // person's, so they carry over rather than being reset by this write.
    let (title, pinned) = match store.get(&req.sid).await {
        Ok(Some(row)) => (row.title, row.pinned),
        _ => (req.title.clone(), false),
    };
    // `collection` is from the spec, not the row: the spec is what this prep
    // was asked to build from, and it cannot have been lost by a write in
    // between. Without it the finished output would leave its collection and
    // become one of its own.

    let session = Session {
        sid: req.sid.clone(),
        title,
        collection_name: ingested.collection.clone(),
        collection_sid: None,
        deck_ref: None,
        speakers,
        slides,
        state: SessionState::Preparing,
        prep_job_sid: Some(prep_job_sid.to_string()),
        failure: None,
        pinned,
        audio: audio.clone(),
        collection: collection.clone(),
        // Filled by `build_session` from the spend scope this runs in.
        spent_usd: None,
        style: audio.is_none().then(|| style.id.to_string()),
    };
    let audio_dir = layout.audio_root.join(&req.sid);
    let language_rule = opennotebook_session::settings::language_rule(&language);

    // Every row write asks first whether the output and its collection are
    // still there, so one deleted during the minutes below stays deleted: a
    // `session_delete` whose job stop did not land is caught here too.
    let (store_ref, sid, cid) = (&store, req.sid.as_str(), collection.as_deref());
    let wanted = move || {
        Box::pin(crate::collection::output_still_wanted(store_ref, sid, cid))
            as futures_util::future::BoxFuture<'_, bool>
    };

    // §3b. Slides and audio together, then the check, and `Ready` written once.
    let outcome = opennotebook_build::build_session(
        &store,
        session,
        Placement {
            deck_dir: &deck_dir,
            style: kit,
            style_label: style.label,
            style_brief: style.brief,
            slide_model: &policy.slide_model,
            language_rule: &language_rule,
            audio_dir: &audio_dir,
            write_slides: audio.is_none(),
            wanted: Some(&wanted),
        },
        &mut job,
    )
    .await?;

    Ok(outcome.session)
}

fn style(req: &SessionPrepareReq) -> &'static opennotebook_sdk::styles::SlideStyle {
    req.style
        .as_deref()
        .and_then(opennotebook_sdk::styles::style)
        .unwrap_or(opennotebook_sdk::styles::DEFAULT_STYLE)
}

/// Speakers, with the studio's ids rather than the caller's ordering.
pub(crate) fn speakers(req: &SessionPrepareReq) -> Vec<Speaker> {
    req.speakers
        .iter()
        .map(|s| Speaker {
            speaker_id: SpeakerId(s.speaker_id.clone()),
            voice_id: s.voice_id.clone(),
            // A role word ("Host") is not a name; see `opennotebook_sdk::voices`.
            display_name: opennotebook_sdk::voices::display_name(&s.display_name, &s.voice_id),
            role: s.role.clone(),
        })
        .collect()
}

/// Read every file in the resource directory, non-recursively.
///
/// Non-recursive on purpose: `ingest_resources` refuses an extension it cannot
/// parse, so walking a tree would turn one stray file deep in a directory into a
/// refused prep. A flat directory is what an upload produces.
fn read_resources(dir: &Path) -> anyhow::Result<Vec<SourceFile>> {
    let mut out = Vec::new();
    let entries = std::fs::read_dir(dir)
        .map_err(|e| anyhow::anyhow!("resource dir {}: {e}", dir.display()))?;
    let mut paths: Vec<PathBuf> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .collect();
    // Deterministic order, so two preps over the same directory outline the same
    // material in the same sequence.
    paths.sort();
    for p in paths {
        let name = p
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        out.push(SourceFile {
            name,
            bytes: std::fs::read(&p)
                .map_err(|e| anyhow::anyhow!("resource {}: {e}", p.display()))?,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_four_dimensions_are_the_ones_section_0_locked() {
        // Not a tautology: §0 fixes this set and gives a page of reasoning for
        // it, including that `api` is deliberately dropped. A silent change here
        // would change what every session can answer questions about.
        assert_eq!(
            DIMENSIONS,
            ["architecture", "technology", "product", "business"]
        );
        assert!(!DIMENSIONS.contains(&"api"));
    }

    #[test]
    fn resources_are_read_in_a_stable_order() {
        let dir = std::env::temp_dir().join(format!("hs-res-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for n in ["c.md", "a.md", "b.md"] {
            std::fs::write(dir.join(n), b"x").unwrap();
        }
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("sub/deep.md"), b"y").unwrap();

        let got = read_resources(&dir).unwrap();
        let names: Vec<_> = got.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(names, ["a.md", "b.md", "c.md"], "sorted, and not recursive");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_resource_dir_is_an_error_not_an_empty_ingest() {
        // The failure this guards: an unreadable directory returning zero files,
        // which would ingest nothing and narrate a session from no material.
        let r = read_resources(Path::new("/nonexistent/resources"));
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("resource dir"));
    }
}
