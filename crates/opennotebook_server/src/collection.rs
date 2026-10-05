//! Collections: one set of sources and everything made from them.
//!
//! A collection's id (cid) is the staging id its sources are added under, so
//! the sources (`staging/<cid>`), the mind maps (`mindmaps/<cid>`) and the
//! notes (`notes/<cid>`) were always keyed by it. A deck or an audio overview
//! is a session row of its own, with a fresh sid, whose `collection` names the
//! cid. The row stored here holds only what none of those can say: a title,
//! whether the studio may rename it, when it was touched, a pin, and the sids
//! of its outputs so one collection is read without scanning every session.
//!
//! The list is SYNTHESISED rather than read from the rows alone. Everything
//! made before collections existed has no row: a session is its own
//! collection under its own sid, and a staging directory with sources and no
//! session is a draft. Writing rows for all of those in a migration would be a
//! second record of facts the disk already holds, so [`synthesize`] folds rows,
//! sessions, staged sources, maps and notes into one list each time, and a row
//! is written for a legacy collection only once somebody edits it or adds to
//! it ([`ensure`], built by the same rule the list uses, so adopting a legacy
//! collection changes nothing a list shows).
//!
//! # A deleted collection stays deleted
//!
//! Some work under a cid is slow — a page load, a minute of research, a map,
//! a prep of many minutes — and the person may delete the collection while it
//! runs. What marks a collection as there is its row or its staging directory
//! ([`live`]), which every new collection has and `collection_delete` removes.
//! Every late write asks first: sources are written only into a staging
//! directory that still exists (`create::stage` never makes one), a map or a
//! set of notes is saved only through [`output_saved`], and a prep asks
//! [`output_still_wanted`] before each write of its row. Bookkeeping that only
//! records a change ([`touched`]) never creates a row.
//!
//! Every read-modify-write of a row in this process holds that cid's lock
//! ([`lock`]), so a rename, a pin, a touch and a delete cannot lose each
//! other's writes or slip a row back in behind a delete.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::future::Future;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex as StdMutex};
use std::time::Duration;

use opennotebook_session::{Collection, CoverSpec, Session, SessionState, SessionStore};

use crate::create;
use crate::filestore::{self, Stored};
use crate::session::{CollectionSummary, SessionSummary};

/// How long naming a collection may take before the heuristic title is used.
/// It runs in the background, so this bounds a task, not a request.
const NAME_TIMEOUT: Duration = Duration::from_secs(30);

/// How much of each source the naming call reads: its title and the opening
/// of its text. A name says what the sources are about together, and that is
/// in their openings, not their bodies.
const NAME_OPENING_CHARS: usize = 500;
const NAME_MAX_SOURCES: usize = 12;

/// The most of one file read for its opening: enough lines to find 500
/// characters of text past the heading, and never a whole large source.
const NAME_READ_BYTES: u64 = 64 * 1024;

pub(crate) fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or_default()
}

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

/// The session store: the one way this service opens it.
pub(crate) async fn open_store() -> Result<SessionStore, String> {
    SessionStore::open().map_err(|e| format!("the database is unavailable: {e}"))
}

// ── ids ─────────────────────────────────────────────────────────────────────

/// The last millisecond a sid was minted at, process-wide. Two mints in the
/// same millisecond — two outputs started by one click, or an agent calling
/// twice — would otherwise both see the id free and both take it.
static LAST_MINTED: AtomicU64 = AtomicU64::new(0);

/// A fresh `s<epoch ms>` id, the shape every studio id has, so its creation
/// time reads back out of it (`session_impl::created_ms`).
///
/// It must be free as a session row, as a collection row AND as a staging
/// directory: a deck's sid and a collection's cid share one namespace on disk
/// (`decks/<sid>`, `staging/<cid>`, `prep/<sid>.json`), and an output that took
/// a cid would read that collection's sources as its own.
pub(crate) async fn mint(store: &SessionStore) -> Result<String, String> {
    let collections = store.collections();
    let collections = &collections;
    mint_free(|sid| async move {
        Ok(create::staging_dir(&sid).exists()
            || store.get(&sid).await.map_err(err)?.is_some()
            || collections.get(&sid).await.map_err(err)?.is_some())
    })
    .await
}

/// The first `s<ms>` from now on that `taken` says is free.
async fn mint_free<F, Fut>(mut taken: F) -> Result<String, String>
where
    F: FnMut(String) -> Fut,
    Fut: Future<Output = Result<bool, String>>,
{
    let mut ms = next_ms(now_ms());
    loop {
        let sid = format!("s{ms}");
        if !taken(sid.clone()).await? {
            return Ok(sid);
        }
        ms = next_ms(ms + 1);
    }
}

/// `want`, or one past the last minted when that is not later.
fn next_ms(want: u64) -> u64 {
    let mut prev = LAST_MINTED.load(Ordering::Relaxed);
    loop {
        let next = want.max(prev + 1);
        match LAST_MINTED.compare_exchange(prev, next, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return next,
            Err(seen) => prev = seen,
        }
    }
}

// ── the row ─────────────────────────────────────────────────────────────────

/// One lock per cid, process-wide; see the module note. An entry nobody holds
/// is dropped the next time any lock is taken, so the map stays as small as
/// the number of collections being edited at once.
static LOCKS: LazyLock<StdMutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>> =
    LazyLock::new(Default::default);

pub(crate) async fn lock(cid: &str) -> tokio::sync::OwnedMutexGuard<()> {
    let m = {
        let mut map = LOCKS.lock().unwrap_or_else(|e| e.into_inner());
        map.retain(|_, m| Arc::strong_count(m) > 1);
        map.entry(cid.to_string()).or_default().clone()
    };
    m.lock_owned().await
}

/// How many empty collections a person may have before a new one is refused.
///
/// The New collection button made one per click, so a few clicks left a row
/// of untitled, empty cards on the home page. Past this many, the empty ones
/// are where the next sources should go.
pub(crate) const MAX_EMPTY_COLLECTIONS: usize = 5;

/// A collection with nothing in it: no sources and nothing made.
pub(crate) fn is_empty(c: &CollectionSummary) -> bool {
    c.sources == 0 && c.decks == 0 && c.audios == 0 && c.maps == 0 && c.notes == 0
}

/// Why another collection may not be started, when it may not.
pub(crate) fn refuse_another(all: &[CollectionSummary]) -> Option<String> {
    let empty = all.iter().filter(|c| is_empty(c)).count();
    (empty >= MAX_EMPTY_COLLECTIONS).then(|| {
        format!(
            "You already have {empty} empty collections. Add sources to one of them, \
             or delete the ones you don't need, before starting another."
        )
    })
}

/// Start a collection: mint its cid, make its staging directory, write its row.
/// Refused while [`MAX_EMPTY_COLLECTIONS`] empty ones already exist, whoever
/// asks: the page, the chat or an agent.
///
/// `title` empty leaves the naming to the studio.
pub(crate) async fn create(title: &str) -> Result<Collection, String> {
    let store = open_store().await?;
    if let Some(why) = refuse_another(&synthesize(&gather(&store).await?)) {
        return Err(why);
    }
    let cid = mint(&store).await?;
    create::staging_dir_made(&cid)?;
    let mut c = Collection::new(&cid, now_ms());
    let title = create::one_line(title);
    if !title.is_empty() {
        c.title = title;
        c.title_auto = false;
    }
    store.collections().put(&c).await.map_err(err)?;
    Ok(c)
}

/// Whether a collection is there: its row or its staging directory. Every
/// collection made since collections exist has both, and `collection_delete`
/// removes both, so this is what every late write asks.
pub(crate) async fn live(store: &SessionStore, cid: &str) -> Result<bool, String> {
    if create::staging_dir(cid).is_dir() {
        return Ok(true);
    }
    Ok(store.collections().get(cid).await.map_err(err)?.is_some())
}

/// [`live`], or anything at all made under the cid before collections: maps,
/// notes, or a session that is its own collection. What a rename or a pin may
/// adopt into a row.
pub(crate) async fn exists(store: &SessionStore, cid: &str) -> Result<bool, String> {
    if live(store, cid).await?
        || crate::mindmap_impl::maps_root().join(cid).is_dir()
        || crate::notes_impl::notes_root().join(cid).is_dir()
    {
        return Ok(true);
    }
    Ok(store
        .get(cid)
        .await
        .map_err(err)?
        .is_some_and(|s| s.collection_id() == cid))
}

/// The collection's row, written first if it has none. Call with its lock.
///
/// A cid with no row is a legacy one: a draft's staging directory or a session
/// made before collections. Its row is built by the rule the list titles it
/// by ([`summarize`]), so adopting it changes nothing anybody sees: the same
/// title, pin, birth and last change. Its outputs are indexed from one scan.
async fn ensure(store: &SessionStore, cid: &str) -> Result<Collection, String> {
    let collections = store.collections();
    if let Some(c) = collections.get(cid).await.map_err(err)? {
        return Ok(c);
    }
    let sessions: Vec<Session> = store
        .list()
        .await
        .map_err(err)?
        .into_iter()
        .filter(|s| s.collection_id() == cid)
        .collect();
    let sids = sessions.iter().map(|s| s.sid.clone()).collect();
    let g = Gathered {
        rows: Vec::new(),
        sessions,
        staged: staged_of(cid).into_iter().collect(),
        maps: made(filestore::list::<crate::mindmap::MindMap>(
            &crate::mindmap_impl::maps_root(),
            cid,
        )),
        notes: made(filestore::list::<crate::notes::StudyNotes>(
            &crate::notes_impl::notes_root(),
            cid,
        )),
        covers: false,
    };
    let c = adopted(&summarize(cid, &g), sids, now_ms());
    collections.put(&c).await.map_err(err)?;
    Ok(c)
}

/// The row a legacy collection is adopted with: what the list already shows.
fn adopted(s: &CollectionSummary, outputs: Vec<String>, now: u64) -> Collection {
    let born = match s.created_ms {
        ms if ms > 0 => ms as u64,
        _ => now,
    };
    Collection {
        cid: s.cid.clone(),
        title: s.title.clone(),
        title_auto: s.title_auto,
        created_ms: born,
        updated_ms: (s.updated_ms as u64).max(born),
        pinned: s.pinned,
        titled_from: String::new(),
        outputs: Some(outputs),
        cover: None,
        cover_from: String::new(),
    }
}

/// Record that something in the collection changed: its row's `updated_ms`,
/// if it has a row. Never creates one: a change landing after a delete must
/// not bring the collection back, and one with no row is dated by its files.
///
/// For a caller whose own work already succeeded and must not fail because the
/// bookkeeping did: the reason goes to the log.
pub(crate) async fn touched(cid: &str) {
    let r = async {
        let store = open_store().await?;
        let _held = lock(cid).await;
        bump(&store, cid).await
    }
    .await;
    if let Err(e) = r {
        eprintln!("opennotebook collection `{cid}`: could not record the change: {e}");
    }
}

/// A prep wrote its output's last row, Ready or Failed: the collection
/// changed. Only while that output is still wanted ([`output_still_wanted`]),
/// and only on an existing row ([`touched`]), so an output or collection
/// deleted meanwhile is not brought back. The prep runs in its own process,
/// where the cid's lock does not reach; a delete stops the prep before it
/// removes anything, which is what keeps the two apart.
///
/// A ready output is new content for the cover, but the cover is not designed
/// here. The prep is a short-lived process on its way out, and a model call
/// made from it was seen to outlast the design timeout while the same call from
/// the server took a second; a failed design also records the content it was
/// tried on, so the cover would then count as current and never be retried.
/// The server designs it instead: the next `collection_list` or
/// `collection_get` finds the cover older than what the collection holds
/// ([`stale_covers`]) and queues it.
pub(crate) async fn output_finished(store: &SessionStore, session: &Session) {
    let cid = session.collection_id();
    if output_still_wanted(store, &session.sid, Some(cid)).await {
        touched(cid).await;
    }
}

async fn bump(store: &SessionStore, cid: &str) -> Result<(), String> {
    let collections = store.collections();
    if let Some(mut c) = collections.get(cid).await.map_err(err)? {
        c.updated_ms = now_ms();
        collections.put(&c).await.map_err(err)?;
    }
    Ok(())
}

/// Save something made from a collection's sources — a map, a set of notes —
/// only while the collection is still there, and record the change.
///
/// `Ok(None)` when it was deleted while the thing was being made: nothing is
/// saved, because a map file under the cid would list the collection again.
/// `save` is not run at all then; it runs under the cid's lock otherwise, so
/// a delete cannot fall between the check and the write.
pub(crate) async fn output_saved<T>(
    cid: &str,
    save: impl Future<Output = Result<T, String>>,
) -> Result<Option<T>, String> {
    let store = open_store().await?;
    let _held = lock(cid).await;
    if !live(&store, cid).await? {
        eprintln!("opennotebook collection `{cid}`: deleted while an output was made; not kept");
        return Ok(None);
    }
    let out = save.await?;
    bump(&store, cid).await?;
    spawn_refresh(cid);
    Ok(Some(out))
}

/// Add a deck or audio overview to its collection: index its sid on the row
/// and write its placeholder row, under the cid's lock, only while the
/// collection is there. `false` when it was deleted, and nothing is written.
///
/// Making an output is adding to the collection, so a legacy one with no row
/// gets one here ([`ensure`]): the index lives on it.
pub(crate) async fn output_added(
    store: &SessionStore,
    cid: &str,
    placeholder: &Session,
) -> Result<bool, String> {
    let _held = lock(cid).await;
    if !live(store, cid).await? {
        return Ok(false);
    }
    let mut c = ensure(store, cid).await?;
    if let Some(outputs) = &mut c.outputs
        && !outputs.contains(&placeholder.sid)
    {
        outputs.push(placeholder.sid.clone());
    }
    c.updated_ms = now_ms();
    store.collections().put(&c).await.map_err(err)?;
    store.put(placeholder).await.map_err(err)?;
    Ok(true)
}

/// Take a deleted output's sid off its collection's index. A row with no
/// index, or no row, has nothing to update.
pub(crate) async fn output_removed(
    store: &SessionStore,
    cid: &str,
    sid: &str,
) -> Result<(), String> {
    let _held = lock(cid).await;
    let collections = store.collections();
    if let Some(mut c) = collections.get(cid).await.map_err(err)?
        && let Some(outputs) = &mut c.outputs
        && outputs.iter().any(|s| s == sid)
    {
        outputs.retain(|s| s != sid);
        collections.put(&c).await.map_err(err)?;
    }
    Ok(())
}

/// The source set changed: record it, then name the collection again if the
/// studio names it and design its cover again, in the background
/// ([`spawn_refresh`]). Never waits on the model.
///
/// Adding a source is adding to the collection, so a cid with no row gets one
/// ([`ensure`]) — including a cid an agent picked that had nothing under it,
/// whose staging directory the add just made. Not when the staging directory
/// is gone: the collection was deleted while the source was being read, and
/// the add is dropped with it.
pub(crate) async fn sources_changed(cid: &str) {
    let r = async {
        let store = open_store().await?;
        let _held = lock(cid).await;
        if !create::staging_dir(cid).is_dir() {
            return Ok(None);
        }
        let mut c = ensure(&store, cid).await?;
        c.updated_ms = now_ms();
        store.collections().put(&c).await.map_err(err)?;
        Ok::<_, String>(Some(c))
    }
    .await;
    match r {
        Ok(Some(_)) => spawn_refresh(cid),
        Ok(None) => eprintln!(
            "opennotebook collection `{cid}`: deleted while a source was added; not recorded"
        ),
        Err(e) => eprintln!("opennotebook collection `{cid}`: could not record the change: {e}"),
    }
}

/// Rename a collection; an empty title hands the naming back to the studio,
/// which names it again in the background. `false` when nothing is under the
/// cid. Adopting a legacy collection to rename it keeps everything else it
/// had, its last change included.
pub(crate) async fn retitle(store: &SessionStore, cid: &str, title: &str) -> Result<bool, String> {
    let auto = {
        let _held = lock(cid).await;
        if !exists(store, cid).await? {
            return Ok(false);
        }
        let mut c = ensure(store, cid).await?;
        let title = create::one_line(title);
        c.title_auto = title.is_empty();
        c.title = title;
        c.titled_from.clear();
        store.collections().put(&c).await.map_err(err)?;
        c.title_auto
    };
    if auto {
        spawn_refresh(cid);
    }
    Ok(true)
}

/// Pin or unpin a collection. `false` when nothing is under the cid.
pub(crate) async fn pin(store: &SessionStore, cid: &str, pinned: bool) -> Result<bool, String> {
    let _held = lock(cid).await;
    if !exists(store, cid).await? {
        return Ok(false);
    }
    let mut c = ensure(store, cid).await?;
    c.pinned = pinned;
    store.collections().put(&c).await.map_err(err)?;
    Ok(true)
}

// ── naming and covers, in the background ────────────────────────────────────

/// The cids being refreshed now, each with whether it changed again while it
/// was. A change during a run does not start a second run beside it; it asks
/// the running one to go round once more when it is done.
static RUNS: LazyLock<StdMutex<HashMap<String, bool>>> = LazyLock::new(Default::default);

/// Claim the run of `cid`: true when this caller should run it, false when a
/// run is already going, which is told to run again.
fn run_claim(running: &mut HashMap<String, bool>, cid: &str) -> bool {
    match running.get_mut(cid) {
        Some(again) => {
            *again = true;
            false
        }
        None => {
            running.insert(cid.to_string(), false);
            true
        }
    }
}

/// A run of `cid` finished: true when it must go round again.
fn run_done(running: &mut HashMap<String, bool>, cid: &str) -> bool {
    match running.get_mut(cid) {
        Some(again) if *again => {
            *again = false;
            true
        }
        _ => {
            running.remove(cid);
            false
        }
    }
}

fn runs() -> std::sync::MutexGuard<'static, HashMap<String, bool>> {
    RUNS.lock().unwrap_or_else(|e| e.into_inner())
}

/// Name the collection, then design its cover, merged per cid. Each step skips
/// itself when what it was made from has not changed, so a run costs a model
/// call only for what is new.
async fn refresh(cid: String) {
    if !run_claim(&mut runs(), &cid) {
        return;
    }
    loop {
        if let Err(e) = name(&cid).await {
            eprintln!("opennotebook collection `{cid}`: not named: {e}");
        }
        if let Err(e) = redraw(&cid, false).await {
            eprintln!("opennotebook collection `{cid}`: cover not designed: {e}");
        }
        if !run_done(&mut runs(), &cid) {
            break;
        }
    }
}

pub(crate) fn spawn_refresh(cid: &str) {
    tokio::spawn(refresh(cid.to_string()));
}

/// Collections the list found with no cover for what they hold now, waiting
/// for one worker. Small on purpose: a first list of many legacy collections
/// designs a few covers at a time, one after another, and the rest are found
/// again by a later list.
static BACKLOG: LazyLock<StdMutex<(VecDeque<String>, bool)>> = LazyLock::new(Default::default);
const BACKLOG_MAX: usize = 3;

fn backlog() -> std::sync::MutexGuard<'static, (VecDeque<String>, bool)> {
    BACKLOG.lock().unwrap_or_else(|e| e.into_inner())
}

/// Queue covers to design, a few at most, with one worker draining them.
pub(crate) fn enqueue_covers(cids: Vec<String>) {
    let start = {
        let mut b = backlog();
        for cid in cids {
            if b.0.len() >= BACKLOG_MAX {
                break;
            }
            if !b.0.contains(&cid) {
                b.0.push_back(cid);
            }
        }
        let start = !b.1 && !b.0.is_empty();
        b.1 |= start;
        start
    };
    if start {
        tokio::spawn(async {
            loop {
                let next = {
                    let mut b = backlog();
                    let next = b.0.pop_front();
                    b.1 = next.is_some();
                    next
                };
                match next {
                    Some(cid) => refresh(cid).await,
                    None => break,
                }
            }
        });
    }
}

/// Name a collection from its sources, if the studio still names it and the
/// sources changed since it last did.
///
/// The model is asked first and the heuristic answers whenever it cannot: a
/// collection with sources is never left untitled because a model was down.
/// The result is dropped when the sources changed while the model thought:
/// the call that changed them asked for another run, and an older answer
/// landing last would name the collection after a set it no longer has.
async fn name(cid: &str) -> Result<(), String> {
    // Off in Settings: collections stay untitled until someone names them.
    if !opennotebook_session::settings::auto_name().await {
        return Ok(());
    }
    let names = create::staged_names(cid);
    if names.is_empty() {
        return Ok(());
    }
    let signature = names.join("\n");
    let store = open_store().await?;
    let collections = store.collections();
    let Some(c) = collections.get(cid).await.map_err(err)? else {
        return Ok(());
    };
    if !c.title_auto || (c.titled_from == signature && !c.title.is_empty()) {
        return Ok(());
    }
    let digest = Digest::of_sources(cid, &names);
    let fallback = heuristic_title(&digest.sources);
    let title = match tokio::time::timeout(NAME_TIMEOUT, model_title(&digest)).await {
        Ok(Ok(t)) => t,
        Ok(Err(e)) => {
            eprintln!("opennotebook collection `{cid}`: naming from the heuristic: {e}");
            fallback
        }
        Err(_) => fallback,
    };
    if title.is_empty() || create::staged_names(cid) != names {
        return Ok(());
    }
    // Re-read under the lock right before the write: the person may have
    // typed a title, pinned it, or deleted it while the model was asked.
    let _held = lock(cid).await;
    let Some(mut now) = collections.get(cid).await.map_err(err)? else {
        return Ok(());
    };
    if !now.title_auto {
        return Ok(());
    }
    now.title = title;
    now.titled_from = signature;
    collections.put(&now).await.map_err(err)
}

/// One source as the naming call reads it.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Opening {
    pub title: String,
    pub text: String,
}

/// The title and opening of the first [`NAME_MAX_SOURCES`] sources, each read
/// only as far as its opening goes.
fn openings(cid: &str, names: &[String]) -> Vec<Opening> {
    let dir = create::staging_dir(cid);
    names
        .iter()
        .take(NAME_MAX_SOURCES)
        .filter_map(|name| {
            let path = dir.join(name);
            let file = std::fs::File::open(&path).ok()?;
            let text = opening(std::io::Read::take(
                std::io::BufReader::new(file),
                NAME_READ_BYTES,
            ));
            Some(Opening {
                title: crate::sources_impl::heading(&path, name).0,
                text,
            })
        })
        .filter(|o| !o.text.is_empty() || !o.title.is_empty())
        .collect()
}

/// The first source's own title, clipped on a word: the same rule a draft's
/// title has always had (`create::clip_title`).
pub(crate) fn heuristic_title(read: &[Opening]) -> String {
    read.first()
        .map(|o| create::clip_title(&o.title))
        .unwrap_or_default()
}

async fn model_title(digest: &Digest) -> Result<String, String> {
    use opennotebook_ai::Message;

    let provider = opennotebook_session::ai::provider().await?;
    let model = opennotebook_session::settings::agent_model().await;
    let language = opennotebook_session::settings::language_rule(
        &opennotebook_session::settings::language().await,
    );
    let system = format!(
        "You name a collection of sources a person is studying. Reply with the name only: \
         3 to 7 words saying what the sources are about together, in title case, with no \
         quotes, no trailing punctuation and nothing before or after it. {language}"
    );
    let resp = provider
        .completions()
        .model(&model)
        .message(Message::system(system))
        .user(digest.sources_text())
        .send()
        .await
        .map_err(|e| format!("the model could not name it: {e}"))?;
    opennotebook_session::spend::record("title", &model, resp.usage.as_ref());
    clean_title(&resp.text).ok_or_else(|| format!("the model's name was unusable: {:?}", resp.text))
}

/// The opening of a staged source, past the heading and the `Source:` or
/// `File:` line that `create` writes, which the prompt already carries. Reads
/// lines only until it has [`NAME_OPENING_CHARS`].
fn opening(r: impl BufRead) -> String {
    let mut words: Vec<String> = Vec::new();
    let mut len = 0;
    for line in r.lines().map_while(Result::ok) {
        if line.starts_with("# ") || line.starts_with("Source: ") || line.starts_with("File: ") {
            continue;
        }
        for w in line.split_whitespace() {
            len += w.chars().count() + 1;
            words.push(w.to_string());
        }
        if len > NAME_OPENING_CHARS {
            break;
        }
    }
    words.join(" ").chars().take(NAME_OPENING_CHARS).collect()
}

/// A model's reply as a title, or None when it is not one.
///
/// Small models wrap a name in quotes, bold, a `Title:` label or a closing
/// full stop however plainly they are asked not to; those are taken off. A
/// reply that is still not a few words is a sentence about the sources, not a
/// name, and the heuristic does better than it.
fn clean_title(raw: &str) -> Option<String> {
    let line = raw.lines().map(str::trim).find(|l| !l.is_empty())?;
    let line = line.trim_start_matches('#').trim();
    let line = line
        .strip_prefix("Title:")
        .or_else(|| line.strip_prefix("Name:"))
        .unwrap_or(line);
    // Twice, because the wrappers nest either way round: `"Name".` and `"Name."`.
    let strip = |l: &str| {
        l.trim()
            .trim_end_matches(['.', '!', ';', ':', ','])
            .trim_matches(['"', '\'', '*', '“', '”', '‘', '’', '`'])
            .trim()
            .to_string()
    };
    let line = strip(&strip(line));
    let words = line.split_whitespace().count();
    (1..=10).contains(&words).then_some(line)
}

// ── what a collection holds, as a model reads it ────────────────────────────

/// How much of the outputs' titles a cover reads, in characters.
const MADE_CHARS: usize = 1500;

/// One output as a cover reads it: its kind, title and its parts' names.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Part {
    pub kind: &'static str,
    pub title: String,
    pub parts: Vec<String>,
}

/// What a collection holds, bounded, for the models that read it: the naming
/// call reads its sources, the cover call all of it.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct Digest {
    pub title: String,
    pub sources: Vec<Opening>,
    pub made: Vec<Part>,
}

impl Digest {
    /// The title and opening of each of the first sources.
    pub(crate) fn of_sources(cid: &str, names: &[String]) -> Self {
        Self {
            sources: openings(cid, names),
            ..Default::default()
        }
    }

    /// Everything: the title, the sources and the ready outputs of `g`.
    fn of(cid: &str, title: &str, g: &Gathered) -> Self {
        let names = g
            .staged
            .get(cid)
            .map(|s| s.names.clone())
            .unwrap_or_default();
        let mut d = Self::of_sources(cid, &names);
        d.title = title.trim().to_string();
        d.made = made_parts(cid, g);
        d
    }

    pub(crate) fn sources_text(&self) -> String {
        self.sources
            .iter()
            .map(|o| format!("Source: {}\n{}", o.title, o.text))
            .collect::<Vec<_>>()
            .join("\n\n")
    }

    /// The outputs as lines, cut at [`MADE_CHARS`].
    fn made_text(&self) -> String {
        let mut out = String::new();
        for p in &self.made {
            let mut line = format!("{}: {}", p.kind, p.title);
            if !p.parts.is_empty() {
                line.push_str(" — ");
                line.push_str(&p.parts.join("; "));
            }
            let room = MADE_CHARS.saturating_sub(out.chars().count());
            if room < 20 {
                break;
            }
            out.extend(line.chars().take(room - 1));
            out.push('\n');
        }
        out
    }

    /// The cover call's user message.
    pub(crate) fn cover_prompt(&self) -> String {
        let mut s = String::new();
        if !self.title.is_empty() {
            s.push_str(&format!("Collection title: {}\n\n", self.title));
        }
        if !self.sources.is_empty() {
            s.push_str("Sources:\n\n");
            s.push_str(&self.sources_text());
            s.push_str("\n\n");
        }
        let made = self.made_text();
        if !made.is_empty() {
            s.push_str("Made from them:\n");
            s.push_str(&made);
        }
        s
    }
}

/// The ready outputs of a collection, newest first, as the digest reads them.
fn made_parts(cid: &str, g: &Gathered) -> Vec<Part> {
    let mut sessions: Vec<&Session> = g
        .sessions
        .iter()
        .filter(|s| s.collection_id() == cid && s.state == SessionState::Ready)
        .collect();
    sessions.sort_by(|a, b| newest_first(a, b));
    let mut out: Vec<Part> = sessions
        .into_iter()
        .map(|s| Part {
            kind: if s.audio.is_some() {
                "Audio overview"
            } else {
                "Narrated slides"
            },
            title: s.title.clone(),
            parts: s
                .slides
                .iter()
                .map(|sl| sl.title.trim().to_string())
                .filter(|t| !t.is_empty())
                .collect(),
        })
        .collect();
    for (kind, made) in [("Mind map", &g.maps), ("Study notes", &g.notes)] {
        out.extend(made.iter().filter(|m| m.cid == cid).map(|m| Part {
            kind,
            title: m.title.clone(),
            parts: m.parts.clone(),
        }));
    }
    out
}

/// What a cover is designed from, as a short hash: the source names and each
/// ready output's id and title. A touch that changed none of them, a rename
/// or a pin, is not a reason to design it again.
pub(crate) fn content_key(cid: &str, g: &Gathered) -> String {
    let mut parts: Vec<String> = g
        .staged
        .get(cid)
        .map(|s| s.names.clone())
        .unwrap_or_default();
    let mut outs: Vec<String> = g
        .sessions
        .iter()
        .filter(|s| s.collection_id() == cid && s.state == SessionState::Ready)
        .map(|s| format!("{}\u{1f}{}", s.sid, s.title))
        .chain(
            g.maps
                .iter()
                .chain(&g.notes)
                .filter(|m| m.cid == cid)
                .map(|m| format!("{}\u{1f}{}", m.id, m.title)),
        )
        .collect();
    outs.sort();
    parts.push("--".to_string());
    parts.extend(outs);
    let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
    format!("{:016x}", crate::cover::hash(&refs))
}

/// Whether a cover should be designed now: covers on, something to read, and
/// either asked for or made from content that has since changed.
pub(crate) fn wants_design(
    covers_on: bool,
    force: bool,
    holds: bool,
    drawn_from: &str,
    key: &str,
) -> bool {
    covers_on && holds && (force || drawn_from != key)
}

/// Whether the collection holds anything a cover can be designed from.
fn holds(s: &CollectionSummary) -> bool {
    s.sources > 0 || s.maps > 0 || s.notes > 0 || s.decks + s.audios > s.preparing + s.failed
}

/// The cids of the list whose covers are out of date, most recently updated
/// first. None at all with covers off.
pub(crate) fn stale_covers(g: &Gathered, list: &[CollectionSummary]) -> Vec<String> {
    list.iter()
        .filter(|s| {
            let drawn_from = g
                .rows
                .iter()
                .find(|c| c.cid == s.cid)
                .map_or("", |c| c.cover_from.as_str());
            wants_design(
                g.covers,
                false,
                holds(s),
                drawn_from,
                &content_key(&s.cid, g),
            )
        })
        .map(|s| s.cid.clone())
        .collect()
}

/// Design a collection's cover from what it holds, unless nothing changed
/// since the last design (`force` designs it anyway).
///
/// The model is asked outside the lock; the row is written under it, re-read
/// first, and only while the collection is there ([`exists`]): a collection
/// deleted while the model thought stays deleted. A legacy one is adopted
/// ([`ensure`]), as a rename adopts it. A failed design keeps the cover the
/// collection had and still records what it was tried on, so a model that is
/// down is asked again when the content changes, not on every list.
pub(crate) async fn redraw(cid: &str, force: bool) -> Result<(), String> {
    let covers_on = opennotebook_session::settings::covers().await;
    if !covers_on {
        return Ok(());
    }
    let store = open_store().await?;
    let g = gather_one(&store, cid).await?;
    let Some(summary) = synthesize(&g).into_iter().next() else {
        return Ok(());
    };
    let key = content_key(cid, &g);
    let drawn_from = g.rows.first().map_or("", |c| c.cover_from.as_str());
    if !wants_design(covers_on, force, holds(&summary), drawn_from, &key) {
        return Ok(());
    }
    let digest = Digest::of(cid, &summary.title, &g);
    let designed = match tokio::time::timeout(
        crate::cover::DESIGN_TIMEOUT,
        crate::cover::design(cid, &digest),
    )
    .await
    {
        Ok(Ok(spec)) => Some(spec),
        Ok(Err(e)) => {
            eprintln!("opennotebook collection `{cid}`: cover kept: {e}");
            None
        }
        Err(_) => {
            eprintln!("opennotebook collection `{cid}`: cover kept: the model took too long");
            None
        }
    };
    let _held = lock(cid).await;
    if !exists(&store, cid).await? {
        return Ok(());
    }
    let mut c = ensure(&store, cid).await?;
    if designed.is_some() {
        c.cover = designed;
    }
    c.cover_from = key;
    store.collections().put(&c).await.map_err(err)
}

/// `collection_cover_refresh`: design the cover again now and wait for it.
/// `false` when nothing is under the cid.
pub(crate) async fn cover_refresh(store: &SessionStore, cid: &str) -> Result<bool, String> {
    if !exists(store, cid).await? {
        return Ok(false);
    }
    redraw(cid, true).await?;
    Ok(true)
}

/// The spec a cover is drawn from now: the designed one while covers are on,
/// else the one drawn from the cid, title and first sources' titles.
pub(crate) fn cover_spec(cid: &str, summary: &CollectionSummary, g: &Gathered) -> CoverSpec {
    if g.covers
        && let Some(spec) = g
            .rows
            .iter()
            .find(|c| c.cid == cid)
            .and_then(|c| c.cover.clone())
    {
        return spec;
    }
    let dir = create::staging_dir(cid);
    let titles: Vec<String> = g
        .staged
        .get(cid)
        .map(|s| s.names.clone())
        .unwrap_or_default()
        .iter()
        .take(5)
        .map(|n| crate::sources_impl::heading(&dir.join(n), n).0)
        .collect();
    crate::cover::fallback(cid, &summary.title, &titles)
}

/// The version [`cover_spec`] draws, without reading any source.
fn cover_version(
    cid: &str,
    title: &str,
    row: Option<&Collection>,
    covers: bool,
    staged: &Staged,
) -> String {
    match row.and_then(|c| c.cover.as_ref()) {
        Some(spec) if covers => crate::cover::version(spec),
        _ => crate::cover::fallback_version(cid, title, &staged.names),
    }
}

// ── the list ────────────────────────────────────────────────────────────────

/// Staged sources of one cid, as the list needs them.
#[derive(Debug, Clone, Default)]
pub(crate) struct Staged {
    pub count: u32,
    /// The source file names, sorted.
    pub names: Vec<String>,
    /// The first source's title, for a legacy draft with nothing else to name it.
    pub first_title: String,
    /// When a source was last added or removed: the directory's mtime.
    pub changed_ms: u64,
}

/// A mind map or set of notes, as the list needs it.
#[derive(Debug, Clone)]
pub(crate) struct Made {
    pub cid: String,
    pub id: String,
    pub title: String,
    pub created_ms: u64,
    /// Its main parts' names, for the cover.
    pub parts: Vec<String>,
}

fn made<T: Stored>(items: Vec<T>) -> Vec<Made> {
    items
        .iter()
        .map(|m| Made {
            cid: m.cid().to_string(),
            id: m.id().to_string(),
            title: m.title().to_string(),
            created_ms: m.created_ms(),
            parts: m.parts(),
        })
        .collect()
}

/// Everything the list is folded from, gathered once.
#[derive(Debug, Default)]
pub(crate) struct Gathered {
    pub rows: Vec<Collection>,
    pub sessions: Vec<Session>,
    pub staged: BTreeMap<String, Staged>,
    pub maps: Vec<Made>,
    pub notes: Vec<Made>,
    /// Whether covers are designed (the setting), read once per gather.
    pub covers: bool,
}

/// Everything, for the list of every collection.
pub(crate) async fn gather(store: &SessionStore) -> Result<Gathered, String> {
    let rows = store.collections().list().await.map_err(err)?;
    let mut sessions = Vec::new();
    for s in store.list().await.map_err(err)? {
        sessions.push(crate::session_impl::reconcile(store, s).await);
    }
    Ok(Gathered {
        rows,
        sessions,
        staged: staged_all(),
        maps: made(filestore::list_all::<crate::mindmap::MindMap>(
            &crate::mindmap_impl::maps_root(),
        )),
        notes: made(filestore::list_all::<crate::notes::StudyNotes>(
            &crate::notes_impl::notes_root(),
        )),
        covers: opennotebook_session::settings::covers().await,
    })
}

/// What one collection is folded from, read for it alone: its row, its
/// outputs by the row's index, its own staging directory, maps and notes.
pub(crate) async fn gather_one(store: &SessionStore, cid: &str) -> Result<Gathered, String> {
    let row = store.collections().get(cid).await.map_err(err)?;
    let indexed = match row.as_ref().map(|r| r.outputs.clone()) {
        Some(Some(sids)) => sids,
        Some(None) => index_from_scan(store, cid).await?,
        None => Vec::new(),
    };
    // The cid itself is asked too: a session from before collections is the
    // collection of its own sid, and no index names it.
    let mut sessions = Vec::new();
    for sid in std::iter::once(cid.to_string()).chain(indexed.into_iter().filter(|s| s != cid)) {
        if let Some(s) = store.get(&sid).await.map_err(err)?
            && s.collection_id() == cid
        {
            sessions.push(crate::session_impl::reconcile(store, s).await);
        }
    }
    Ok(Gathered {
        rows: row.into_iter().collect(),
        sessions,
        staged: staged_of(cid).into_iter().collect(),
        maps: made(filestore::list::<crate::mindmap::MindMap>(
            &crate::mindmap_impl::maps_root(),
            cid,
        )),
        notes: made(filestore::list::<crate::notes::StudyNotes>(
            &crate::notes_impl::notes_root(),
            cid,
        )),
        covers: opennotebook_session::settings::covers().await,
    })
}

/// The outputs of a row written before the index existed, found by one scan
/// and written onto the row, so the scan happens once per collection.
async fn index_from_scan(store: &SessionStore, cid: &str) -> Result<Vec<String>, String> {
    let sids: Vec<String> = store
        .list()
        .await
        .map_err(err)?
        .into_iter()
        .filter(|s| s.collection_id() == cid)
        .map(|s| s.sid)
        .collect();
    let _held = lock(cid).await;
    let collections = store.collections();
    if let Some(mut c) = collections.get(cid).await.map_err(err)?
        && c.outputs.is_none()
    {
        c.outputs = Some(sids.clone());
        collections.put(&c).await.map_err(err)?;
    }
    Ok(sids)
}

/// Every staging directory that holds at least one source.
fn staged_all() -> BTreeMap<String, Staged> {
    let Ok(rd) = std::fs::read_dir(create::staging_root()) else {
        return BTreeMap::new();
    };
    rd.flatten()
        .filter_map(|e| e.file_name().to_str().map(str::to_string))
        .filter(|cid| create::safe_sid(cid).is_ok())
        .filter_map(|cid| staged_of(&cid))
        .collect()
}

/// One cid's staged sources, None when it has none. The first source is read
/// for its heading only.
fn staged_of(cid: &str) -> Option<(String, Staged)> {
    let dir = create::staging_dir(cid);
    if !dir.is_dir() {
        return None;
    }
    let names = create::staged_names(cid);
    let first = names.first()?;
    Some((
        cid.to_string(),
        Staged {
            count: names.len() as u32,
            first_title: crate::sources_impl::heading(&dir.join(first), first).0,
            changed_ms: mtime_ms(&dir),
            names: names.clone(),
        },
    ))
}

fn mtime_ms(p: &Path) -> u64 {
    std::fs::metadata(p)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or_default()
}

/// Newest first, by the time in the sid; the sid itself breaks a tie so the
/// order is stable.
fn newest_first(a: &Session, b: &Session) -> std::cmp::Ordering {
    let (ta, tb) = (
        crate::session_impl::created_ms(&a.sid),
        crate::session_impl::created_ms(&b.sid),
    );
    tb.cmp(&ta).then_with(|| b.sid.cmp(&a.sid))
}

/// Rows, sessions, sources, maps and notes folded into one list of
/// collections, most recently updated first.
///
/// A collection is listed when it has a row, an output, a staged source, a map
/// or notes. A session with no `collection` belongs to the collection named by
/// its own sid. A collection with a row is titled by the row, even while that
/// title is empty: the studio is naming it, and a stand-in here would flash a
/// different name before the real one. One with no row is titled by what was
/// made from it — its session, then its newest map or notes, then its first
/// source — because nothing else will ever name it.
pub(crate) fn synthesize(g: &Gathered) -> Vec<CollectionSummary> {
    let mut cids: BTreeSet<&str> = BTreeSet::new();
    cids.extend(g.rows.iter().map(|c| c.cid.as_str()));
    cids.extend(g.sessions.iter().map(Session::collection_id));
    cids.extend(g.staged.keys().map(String::as_str));
    cids.extend(g.maps.iter().map(|m| m.cid.as_str()));
    cids.extend(g.notes.iter().map(|m| m.cid.as_str()));

    let mut out: Vec<CollectionSummary> = cids.into_iter().map(|cid| summarize(cid, g)).collect();
    out.sort_by(|a, b| {
        b.updated_ms
            .cmp(&a.updated_ms)
            .then_with(|| b.cid.cmp(&a.cid))
    });
    out
}

fn summarize(cid: &str, g: &Gathered) -> CollectionSummary {
    let row = g.rows.iter().find(|c| c.cid == cid);
    let mut outputs: Vec<&Session> = g
        .sessions
        .iter()
        .filter(|s| s.collection_id() == cid)
        .collect();
    outputs.sort_by(|a, b| newest_first(a, b));
    let newest_made = |made: &[Made]| {
        made.iter()
            .filter(|m| m.cid == cid)
            .max_by_key(|m| m.created_ms)
            .cloned()
    };
    let map = newest_made(&g.maps);
    let note = newest_made(&g.notes);
    let staged = g.staged.get(cid).cloned().unwrap_or_default();

    let decks = outputs.iter().filter(|s| s.audio.is_none()).count() as u32;
    let audios = outputs.len() as u32 - decks;
    let count_state = |st: SessionState| outputs.iter().filter(|s| s.state == st).count() as i64;
    let (title, title_auto, pinned) = match row {
        Some(c) => (c.title.clone(), c.title_auto, c.pinned),
        None => {
            let own = outputs
                .iter()
                .find(|s| s.sid == cid && !s.title.trim().is_empty());
            let title = own
                .map(|s| s.title.clone())
                .or_else(|| {
                    [map.as_ref(), note.as_ref()]
                        .into_iter()
                        .flatten()
                        .max_by_key(|m| m.created_ms)
                        .map(|m| m.title.clone())
                })
                .unwrap_or_else(|| staged.first_title.clone());
            (title, own.is_none(), outputs.iter().any(|s| s.pinned))
        }
    };

    let born = row
        .map(|c| c.created_ms)
        .filter(|ms| *ms > 0)
        .or_else(|| Some(crate::session_impl::created_ms(cid)).filter(|ms| *ms > 0));
    let activity = [
        row.map_or(0, |c| c.updated_ms),
        staged.changed_ms,
        map.as_ref().map_or(0, |m| m.created_ms),
        note.as_ref().map_or(0, |m| m.created_ms),
    ]
    .into_iter()
    .chain(
        outputs
            .iter()
            .map(|s| crate::session_impl::created_ms(&s.sid)),
    )
    .chain(born)
    .max()
    .unwrap_or_default();
    // A cid with no time in it and no row has no birth to read; its first
    // sign of life is the best there is.
    let created = born.unwrap_or(activity);

    CollectionSummary {
        cid: cid.to_string(),
        title_auto,
        created_ms: created as i64,
        updated_ms: activity.max(created) as i64,
        pinned,
        sources: staged.count as i64,
        decks: decks as i64,
        audios: audios as i64,
        maps: g.maps.iter().filter(|m| m.cid == cid).count() as i64,
        notes: g.notes.iter().filter(|m| m.cid == cid).count() as i64,
        preparing: count_state(SessionState::Preparing),
        failed: count_state(SessionState::Failed),
        cover_version: cover_version(cid, &title, row, g.covers, &staged),
        title,
    }
}

/// A collection's decks and audio overviews, newest first.
pub(crate) fn outputs_of(cid: &str, sessions: Vec<Session>) -> Vec<SessionSummary> {
    let mut mine: Vec<Session> = sessions
        .into_iter()
        .filter(|s| s.collection_id() == cid)
        .collect();
    mine.sort_by(newest_first);
    mine.into_iter().map(crate::session_impl::summary).collect()
}

// ── a prep in flight ────────────────────────────────────────────────────────

/// Whether a prep may still write the row of an output made for `collection`.
///
/// The backstop behind stopping the job on delete: a prep whose job could
/// not be stopped, or that was queued in the moment between the delete and
/// the stop, would otherwise write its `Ready` or `Failed` row minutes later,
/// and a row naming a collection is enough to bring that collection back to
/// the list. So an output whose collection is not [`live`] writes nothing.
/// Not the wider [`exists`]: maps, notes and the output's own sibling rows
/// are exactly what a delete in progress is removing.
///
/// An output with no collection is a session from before collections, its
/// own collection; deleting it is `session_delete`, and
/// [`output_still_wanted`] asks about its row.
pub(crate) fn output_wanted(collection: Option<&str>, has_row: bool, staged: bool) -> bool {
    match collection.map(str::trim).filter(|c| !c.is_empty()) {
        None => true,
        Some(_) => has_row || staged,
    }
}

/// [`output_wanted`] against the store and the disk, for a prep in flight.
///
/// A store that cannot be read answers "wanted": losing a finished output to
/// a database blip is worse than a deleted collection reappearing, which a
/// second delete removes.
pub(crate) async fn still_wanted(store: &SessionStore, collection: Option<&str>) -> bool {
    let Some(cid) = collection.map(str::trim).filter(|c| !c.is_empty()) else {
        return true;
    };
    let has_row = match store.collections().get(cid).await {
        Ok(row) => row.is_some(),
        Err(e) => {
            eprintln!("opennotebook prep: collection `{cid}` unreadable, writing anyway: {e}");
            return true;
        }
    };
    let wanted = output_wanted(Some(cid), has_row, create::staging_dir(cid).is_dir());
    if !wanted {
        eprintln!("opennotebook prep: collection `{cid}` was deleted; this output is not written");
    }
    wanted
}

/// Whether a dispatched prep may still write its output's row: the row is
/// still there, and so is its collection ([`still_wanted`]).
///
/// `session_prepare` writes the row before it dispatches the job, and only
/// `session_delete` and `collection_delete` remove it, so a missing row is a
/// deleted output, and writing it would undelete it. A store that cannot be
/// read answers "wanted", for the reason [`still_wanted`] gives.
pub(crate) async fn output_still_wanted(
    store: &SessionStore,
    sid: &str,
    collection: Option<&str>,
) -> bool {
    match store.get(sid).await {
        Ok(Some(_)) => still_wanted(store, collection).await,
        Ok(None) => {
            eprintln!("opennotebook prep: session `{sid}` was deleted; it is not written back");
            false
        }
        Err(e) => {
            eprintln!("opennotebook prep: session `{sid}` unreadable, writing anyway: {e}");
            true
        }
    }
}

/// Stop the prep job of an output that is still being made, so it cannot
/// write its row back after the row is deleted.
///
/// Only a `Preparing` row has a live job: `Ready` and `Failed` are written by
/// the prep as its last act. Best-effort by design: a delete the person asked
/// for is not refused because the job cannot be stopped, and [`output_still_wanted`]
/// stops the prep at its next write if the job outlives this. The failure is
/// logged, never returned.
pub(crate) async fn stop_prep(s: &Session) {
    if s.state != SessionState::Preparing {
        return;
    }
    match opennotebook_build::job::stop(&s.sid, s.prep_job_sid.as_deref()).await {
        Ok(stopped) if stopped.is_empty() => {}
        Ok(stopped) => eprintln!(
            "opennotebook: stopped prep job {} of deleted output `{}`",
            stopped.join(", "),
            s.sid
        ),
        Err(e) => eprintln!(
            "opennotebook: could not stop the prep job of deleted output `{}`: {e}",
            s.sid
        ),
    }
}

// ── delete ──────────────────────────────────────────────────────────────────

/// Where the studio keeps files by sid and cid. A struct so that deleting is
/// tested on a temporary root, not the box's own `var`.
pub(crate) struct Roots {
    pub staging: PathBuf,
    pub maps: PathBuf,
    pub notes: PathBuf,
    /// Prep specs, `<sid>.json`.
    pub prep: PathBuf,
    pub decks: PathBuf,
    pub audio: PathBuf,
    /// The per-session directories ingest imported into memory.
    pub sessions: PathBuf,
}

impl Roots {
    pub(crate) fn live() -> Self {
        let layout = crate::pipeline::Layout::in_data_dir();
        Self {
            staging: create::staging_root(),
            maps: crate::mindmap_impl::maps_root(),
            notes: crate::notes_impl::notes_root(),
            prep: crate::dispatch::spec_dir(),
            decks: layout.decks_root,
            audio: layout.audio_root,
            sessions: layout.sessions_root,
        }
    }

    /// What one output left on disk: its deck, its audio, its ingest
    /// directory and its prep spec. Best effort: the row is already gone, and
    /// a file that will not go is logged, not reported as a failed delete.
    fn remove_output(&self, sid: &str) {
        for dir in [
            self.decks.join(sid),
            self.audio.join(sid),
            self.sessions.join(sid),
        ] {
            if let Err(e) = remove_dir(&dir) {
                eprintln!("opennotebook: deleted output `{sid}` left {e}");
            }
        }
        let _ = std::fs::remove_file(self.prep.join(format!("{sid}.json")));
    }

    /// The collection's own files: sources, maps, notes, and the prep spec of
    /// a legacy cid that was a session's sid. `Ok(true)` if any was there.
    fn remove_collection(&self, cid: &str) -> Result<bool, String> {
        let mut found = false;
        for dir in [
            self.staging.join(cid),
            self.maps.join(cid),
            self.notes.join(cid),
        ] {
            found |= remove_dir(&dir)?;
        }
        let _ = std::fs::remove_file(self.prep.join(format!("{cid}.json")));
        Ok(found)
    }
}

/// `Ok(false)` when it was not there.
fn remove_dir(dir: &Path) -> Result<bool, String> {
    match std::fs::remove_dir_all(dir) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(format!("{}: {e}", dir.display())),
    }
}

/// Drop the memory collections ingest made for deleted outputs, by name. In
/// the background and best effort: those collections are not listed anywhere
/// in the studio, and a delete the person asked for does not wait on it or
/// fail because of it.
fn forget_memory(gone: Vec<Session>) {
    if gone.is_empty() {
        return;
    }
    tokio::spawn(async move {
        let memory = match opennotebook_memory::from_settings().await {
            Ok(m) => m,
            Err(e) => {
                eprintln!("opennotebook: deleted outputs keep their indexed sources: {e}");
                return;
            }
        };
        for s in gone.into_iter().filter(|s| !s.collection_name.is_empty()) {
            if let Err(e) = memory
                .delete_collection(crate::pipeline::WORKSPACE, &s.collection_name)
                .await
            {
                eprintln!(
                    "opennotebook: indexed sources of deleted output `{}` not dropped: {e}",
                    s.sid
                );
            }
        }
    });
}

/// Remove outputs whose rows are gone: their files, playheads and memory
/// collections.
fn outputs_gone(roots: &Roots, gone: Vec<Session>) {
    for s in &gone {
        roots.remove_output(&s.sid);
        crate::playback::clear(&s.sid);
    }
    forget_memory(gone);
}

/// Delete one output: stop its prep, remove its row, take it off its
/// collection's index, and remove what it left behind. `false` when there was
/// no such sid.
pub(crate) async fn delete_output(store: &SessionStore, sid: &str) -> Result<bool, String> {
    let Some(row) = store.get(sid).await.map_err(err)? else {
        return Ok(false);
    };
    // A session still being made has its job stopped before its row goes, or
    // the job writes the row back when it ends.
    stop_prep(&row).await;
    let removed = store.delete(sid).await.map_err(err)?;
    if let Some(cid) = row.collection.as_deref() {
        output_removed(store, cid, sid).await?;
    }
    outputs_gone(&Roots::live(), vec![row]);
    Ok(removed)
}

/// Remove a collection and everything made from it: its outputs as
/// [`delete_output`] removes one, its sources, maps and notes, and its row.
/// `false` when there was nothing under the cid.
///
/// Under the cid's lock throughout, so nothing in this process writes into
/// it while it goes. An output still being made has its job stopped FIRST,
/// before any row goes: a job left running writes its row back when it ends,
/// and the collection reappears with it. Outputs are found by a full scan,
/// not the index: a delete must not miss one an older row never indexed.
pub(crate) async fn delete(store: &SessionStore, cid: &str) -> Result<bool, String> {
    let roots = Roots::live();
    let _held = lock(cid).await;
    let mine: Vec<Session> = store
        .list()
        .await
        .map_err(err)?
        .into_iter()
        .filter(|s| s.collection_id() == cid)
        .collect();
    for s in &mine {
        stop_prep(s).await;
    }
    let mut found = false;
    for s in &mine {
        found |= store.delete(&s.sid).await.map_err(err)?;
    }
    outputs_gone(&roots, mine);
    found |= roots.remove_collection(cid)?;
    found |= store.collections().delete(cid).await.map_err(err)?;
    Ok(found)
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_sixth_empty_collection_is_refused_and_a_used_one_does_not_count() {
        let empty = |cid: &str| CollectionSummary {
            cid: cid.into(),
            ..Default::default()
        };
        let mut all: Vec<CollectionSummary> = (0..4).map(|i| empty(&format!("c{i}"))).collect();
        all.push(CollectionSummary {
            sources: 2,
            ..empty("used")
        });
        assert!(super::refuse_another(&all).is_none(), "four empty is fine");
        all.push(empty("c4"));
        let why = super::refuse_another(&all).expect("five empty refuses the sixth");
        assert!(
            why.starts_with("You already have 5 empty collections"),
            "{why}"
        );
        assert!(super::is_empty(&empty("x")));
        assert!(!super::is_empty(&CollectionSummary {
            maps: 1,
            ..empty("m")
        }));
    }

    use super::*;

    fn session(sid: &str, collection: Option<&str>, audio: bool, state: SessionState) -> Session {
        Session {
            sid: sid.into(),
            title: format!("title of {sid}"),
            collection_name: sid.into(),
            collection_sid: None,
            deck_ref: None,
            speakers: vec![],
            slides: vec![],
            state,
            prep_job_sid: None,
            failure: None,
            pinned: false,
            audio: audio.then(|| opennotebook_session::AudioSpec {
                format: opennotebook_session::AudioFormat::DeepDive,
                length: opennotebook_session::AudioLength::Default,
                focus: String::new(),
            }),
            collection: collection.map(str::to_string),
            spent_usd: None,
            style: None,
        }
    }

    fn staged(count: u32, title: &str, changed_ms: u64) -> Staged {
        Staged {
            count,
            names: (0..count).map(|i| format!("{i}.md")).collect(),
            first_title: title.into(),
            changed_ms,
        }
    }

    #[test]
    fn a_session_from_before_collections_is_a_collection_of_its_own() {
        let mut s = session("s1700000000000", None, false, SessionState::Ready);
        s.pinned = true;
        let g = Gathered {
            sessions: vec![s],
            staged: BTreeMap::from([(
                "s1700000000000".to_string(),
                staged(2, "First page", 1_700_000_000_500),
            )]),
            ..Default::default()
        };
        let list = synthesize(&g);
        assert_eq!(
            list.len(),
            1,
            "the session and its staging are one collection"
        );
        let c = &list[0];
        assert_eq!(c.cid, "s1700000000000");
        assert_eq!(c.title, "title of s1700000000000", "titled by its session");
        assert!(!c.title_auto, "a session's title was somebody's choice");
        assert!(c.pinned, "a pinned legacy session pins its collection");
        assert_eq!((c.sources, c.decks, c.audios), (2, 1, 0));
        assert_eq!(c.created_ms, 1_700_000_000_000, "born when its sid says");
    }

    #[test]
    fn a_staging_directory_with_no_row_is_a_draft_collection() {
        let g = Gathered {
            staged: BTreeMap::from([(
                "s1710000000000".to_string(),
                staged(1, "Quantifying Variance", 1_710_000_005_000),
            )]),
            maps: vec![],
            ..Default::default()
        };
        let list = synthesize(&g);
        assert_eq!(list.len(), 1);
        let c = &list[0];
        assert_eq!(c.title, "Quantifying Variance", "named by its first source");
        assert!(c.title_auto);
        assert_eq!(
            c.updated_ms, 1_710_000_005_000,
            "updated when a source last changed"
        );
        assert_eq!((c.decks, c.audios, c.preparing), (0, 0, 0));
    }

    #[test]
    fn a_collection_counts_every_output_of_every_kind() {
        let cid = "s1720000000000";
        let g = Gathered {
            rows: vec![Collection {
                updated_ms: 1_720_000_000_100,
                ..Collection::new(cid, 1_720_000_000_000)
            }],
            sessions: vec![
                session("s1720000001000", Some(cid), false, SessionState::Ready),
                session("s1720000002000", Some(cid), false, SessionState::Preparing),
                session("s1720000003000", Some(cid), true, SessionState::Failed),
                // Someone else's, which must not be counted here.
                session("s1720000004000", Some("s1"), false, SessionState::Ready),
            ],
            staged: BTreeMap::from([(cid.to_string(), staged(3, "x", 0))]),
            maps: vec![Made {
                cid: cid.into(),
                id: "m1".into(),
                title: "Map".into(),
                created_ms: 1_720_000_009_000,
                parts: vec![],
            }],
            notes: vec![],
            covers: true,
        };
        let list = synthesize(&g);
        assert_eq!(list.len(), 2);
        let c = list.iter().find(|c| c.cid == cid).unwrap();
        assert_eq!(
            c.title, "",
            "a row's empty title stands while the studio names it"
        );
        assert!(c.title_auto);
        assert_eq!(
            (c.sources, c.decks, c.audios, c.maps, c.notes),
            (3, 2, 1, 1, 0)
        );
        assert_eq!((c.preparing, c.failed), (1, 1));
        assert_eq!(c.created_ms, 1_720_000_000_000);
        assert_eq!(
            c.updated_ms, 1_720_000_009_000,
            "the map is the latest thing in it"
        );
        assert_eq!(list[0].cid, cid, "most recently updated first");

        let outs = outputs_of(cid, g.sessions.clone());
        let sids: Vec<_> = outs.iter().map(|o| o.sid.as_str()).collect();
        assert_eq!(sids, ["s1720000003000", "s1720000002000", "s1720000001000"]);
        assert!(outs.iter().all(|o| o.collection == cid));
        assert_eq!(outs[0].kind, "audio");
    }

    #[test]
    fn a_model_reply_is_cleaned_into_a_name_or_refused() {
        assert_eq!(
            clean_title("\"Linux Kernel Internals\".").as_deref(),
            Some("Linux Kernel Internals")
        );
        assert_eq!(
            clean_title("Title: **Speech Models**\n").as_deref(),
            Some("Speech Models")
        );
        assert_eq!(
            clean_title("\n\n# Rust Async\n").as_deref(),
            Some("Rust Async")
        );
        assert_eq!(
            clean_title("“Kernel Scheduling”.").as_deref(),
            Some("Kernel Scheduling"),
            "curly quotes are quotes too"
        );
        assert_eq!(
            clean_title("‘Rust Lifetimes’").as_deref(),
            Some("Rust Lifetimes")
        );
        assert_eq!(clean_title("   "), None);
        assert_eq!(
            clean_title(
                "These sources together discuss a number of different things about the kernel and its history"
            ),
            None,
            "a sentence is not a name"
        );
    }

    #[test]
    fn the_heuristic_title_is_the_first_sources_clipped() {
        let read = vec![
            Opening {
                title: "\"Quantifying Variance in Evaluation Benchmarks\"".into(),
                text: "x".into(),
            },
            Opening {
                title: "Other".into(),
                text: "y".into(),
            },
        ];
        assert_eq!(
            heuristic_title(&read),
            "Quantifying Variance in Evaluation Benchmarks"
        );
        let long = [Opening {
            title: "Moshi: a speech-text foundation model for real-time dialogue, and more besides"
                .into(),
            text: String::new(),
        }];
        assert_eq!(
            heuristic_title(&long),
            "Moshi: a speech-text foundation model for real-time dialogue, and more"
        );
        assert_eq!(heuristic_title(&[]), "");
    }

    #[test]
    fn the_naming_prompt_reads_past_the_heading_and_source_line() {
        let o = opening("# Tour\n\nSource: https://x\n\nThe kernel   schedules\ntasks.".as_bytes());
        assert_eq!(o, "The kernel schedules tasks.");
        // A long source is read only as far as its opening.
        let long = format!("# T\n{}", "word ".repeat(100_000));
        assert_eq!(opening(long.as_bytes()).chars().count(), NAME_OPENING_CHARS);
    }

    #[test]
    fn an_output_of_a_deleted_collection_is_not_written_back() {
        // Deleted: no row and no staging directory. The prep's late write is
        // what brought the collection back, so it must be refused.
        assert!(!output_wanted(Some("s1"), false, false));
        // Either one is enough for the collection to still be there.
        assert!(output_wanted(Some("s1"), true, false));
        assert!(output_wanted(Some("s1"), false, true));
        assert!(output_wanted(Some("s1"), true, true));
        // A session from before collections is its own collection: nothing
        // to check, and an empty name is the same as none.
        assert!(output_wanted(None, false, false));
        assert!(output_wanted(Some("  "), false, false));
    }

    #[test]
    fn minted_milliseconds_never_repeat() {
        let a = next_ms(5);
        let b = next_ms(5);
        assert!(b > a, "{a} then {b}");
    }

    #[tokio::test]
    async fn a_mint_skips_every_id_already_taken() {
        let asked = std::cell::RefCell::new(Vec::new());
        let sid = mint_free(|sid| {
            asked.borrow_mut().push(sid);
            let taken = asked.borrow().len() <= 2;
            async move { Ok(taken) }
        })
        .await
        .unwrap();
        let asked = asked.into_inner();
        assert_eq!(asked.len(), 3, "two taken, the third free: {asked:?}");
        assert_eq!(sid, asked[2]);
        let ms: Vec<u64> = asked.iter().map(|s| s[1..].parse().unwrap()).collect();
        assert!(ms[0] < ms[1] && ms[1] < ms[2], "{ms:?}");
        // A store that cannot say is an error, not a guess.
        assert!(
            mint_free(|_| async { Err::<bool, _>("down".to_string()) })
                .await
                .is_err()
        );
    }

    #[test]
    fn adopting_a_legacy_session_keeps_what_the_list_shows() {
        let mut s = session("s1700000000000", None, false, SessionState::Ready);
        s.pinned = true;
        s.title = "Linux Internals".into();
        let g = Gathered {
            sessions: vec![s],
            staged: BTreeMap::from([(
                "s1700000000000".to_string(),
                staged(2, "First page", 1_700_000_900_000),
            )]),
            ..Default::default()
        };
        let listed = &synthesize(&g)[0];
        let row = adopted(listed, vec!["s1700000000000".into()], 1_800_000_000_000);
        assert_eq!(row.title, "Linux Internals");
        assert!(!row.title_auto, "a session's title was somebody's choice");
        assert!(row.pinned, "the pin is kept");
        assert_eq!(row.created_ms, 1_700_000_000_000);
        assert_eq!(
            row.updated_ms, 1_700_000_900_000,
            "adopting it is not a change to it"
        );
        assert_eq!(
            row.outputs.as_deref(),
            Some(&["s1700000000000".to_string()][..])
        );

        // Listed again with the row, it is the same collection.
        let again = Gathered {
            rows: vec![row],
            ..g
        };
        let relisted = &synthesize(&again)[0];
        assert_eq!(
            (
                &relisted.title,
                relisted.title_auto,
                relisted.pinned,
                relisted.created_ms,
                relisted.updated_ms
            ),
            (
                &listed.title,
                listed.title_auto,
                listed.pinned,
                listed.created_ms,
                listed.updated_ms
            )
        );
    }

    #[test]
    fn a_naming_run_asked_for_twice_runs_again_not_beside_itself() {
        let mut running = HashMap::new();
        assert!(run_claim(&mut running, "s1"), "the first caller runs it");
        assert!(!run_claim(&mut running, "s1"), "a second waits on it");
        assert!(!run_claim(&mut running, "s1"), "and a third");
        assert!(
            run_claim(&mut running, "s2"),
            "another collection is not held up"
        );
        assert!(run_done(&mut running, "s1"), "asked again: once more");
        assert!(!run_done(&mut running, "s1"), "nothing new: done");
        assert!(run_claim(&mut running, "s1"), "free to run again");
    }

    fn made(cid: &str, id: &str, title: &str, parts: &[&str]) -> Made {
        Made {
            cid: cid.into(),
            id: id.into(),
            title: title.into(),
            created_ms: 1,
            parts: parts.iter().map(|p| p.to_string()).collect(),
        }
    }

    fn cover_gathered(cid: &str) -> Gathered {
        let mut deck = session("s1770000001000", Some(cid), false, SessionState::Ready);
        deck.title = "Kernels".into();
        Gathered {
            rows: vec![Collection::new(cid, 1_770_000_000_000)],
            sessions: vec![deck],
            staged: BTreeMap::from([(cid.to_string(), staged(2, "Intro", 0))]),
            maps: vec![made(cid, "m1", "Map", &["Scheduling", "Memory"])],
            notes: vec![],
            covers: true,
        }
    }

    #[test]
    fn the_cover_key_follows_sources_and_ready_outputs_only() {
        let cid = "s1770000000000";
        let g = cover_gathered(cid);
        let key = content_key(cid, &g);
        assert_eq!(key, content_key(cid, &cover_gathered(cid)), "stable");

        // A rename or a pin of the collection is not new content.
        let mut renamed = cover_gathered(cid);
        renamed.rows[0].title = "Renamed".into();
        renamed.rows[0].pinned = true;
        assert_eq!(content_key(cid, &renamed), key);

        // A source added is.
        let mut more = cover_gathered(cid);
        more.staged.get_mut(cid).unwrap().names.push("z.md".into());
        assert_ne!(content_key(cid, &more), key);

        // An output still preparing or failed is not; one that became ready is.
        let mut prep = cover_gathered(cid);
        prep.sessions.push(session(
            "s1770000002000",
            Some(cid),
            true,
            SessionState::Preparing,
        ));
        prep.sessions.push(session(
            "s1770000003000",
            Some(cid),
            true,
            SessionState::Failed,
        ));
        assert_eq!(content_key(cid, &prep), key);
        prep.sessions[1].state = SessionState::Ready;
        assert_ne!(content_key(cid, &prep), key);

        // A map renamed, or a set of notes added, is.
        let mut map = cover_gathered(cid);
        map.maps[0].title = "Other".into();
        assert_ne!(content_key(cid, &map), key);
        let mut notes = cover_gathered(cid);
        notes.notes.push(made(cid, "n1", "Notes", &[]));
        assert_ne!(content_key(cid, &notes), key);

        // Another collection's things are not.
        let mut other = cover_gathered(cid);
        other.maps.push(made("s1", "m9", "Elsewhere", &[]));
        assert_eq!(content_key(cid, &other), key);
    }

    #[test]
    fn covers_off_never_asks_the_model() {
        for force in [false, true] {
            for holds in [false, true] {
                assert!(!wants_design(false, force, holds, "", "k"));
                assert!(!wants_design(false, force, holds, "k", "k"));
            }
        }
        assert!(wants_design(true, false, true, "", "k"), "never designed");
        assert!(!wants_design(true, false, true, "k", "k"), "unchanged");
        assert!(wants_design(true, true, true, "k", "k"), "asked for");
        assert!(!wants_design(true, true, false, "", "k"), "nothing to read");

        // And the list queues nothing with covers off.
        let cid = "s1770000000000";
        let mut g = cover_gathered(cid);
        let list = synthesize(&g);
        assert_eq!(stale_covers(&g, &list), [cid.to_string()]);
        g.covers = false;
        assert!(stale_covers(&g, &list).is_empty());
    }

    #[test]
    fn a_cover_designed_from_this_content_is_not_queued_again() {
        let cid = "s1770000000000";
        let mut g = cover_gathered(cid);
        g.rows[0].cover_from = content_key(cid, &g);
        assert!(stale_covers(&g, &synthesize(&g)).is_empty());
        // An empty draft with nothing in it has nothing to design from.
        let empty = Gathered {
            rows: vec![Collection::new("s1780000000000", 1)],
            covers: true,
            ..Default::default()
        };
        assert!(stale_covers(&empty, &synthesize(&empty)).is_empty());
    }

    #[test]
    fn the_cover_version_is_the_designed_one_only_while_covers_are_on() {
        let cid = "s1770000000000";
        let mut g = cover_gathered(cid);
        let fallback = synthesize(&g)[0].cover_version.clone();
        assert!(fallback.starts_with("f-"), "{fallback}");

        let spec = crate::cover::fallback(cid, "Designed", &[]);
        g.rows[0].cover = Some(spec.clone());
        let designed = synthesize(&g)[0].cover_version.clone();
        assert_eq!(designed, crate::cover::version(&spec));
        assert_eq!(cover_spec(cid, &synthesize(&g)[0], &g), spec);

        g.covers = false;
        assert_eq!(synthesize(&g)[0].cover_version, fallback);

        // The fallback follows the title the list shows.
        g.rows[0].title = "Named Now".into();
        assert_ne!(synthesize(&g)[0].cover_version, fallback);
    }

    #[test]
    fn the_digest_reads_ready_outputs_titles_within_its_bound() {
        let cid = "s1770000000000";
        let mut g = cover_gathered(cid);
        g.sessions[0].slides = vec![];
        let d = Digest {
            title: "Linux".into(),
            sources: vec![Opening {
                title: "Intro".into(),
                text: "The kernel".into(),
            }],
            made: made_parts(cid, &g),
        };
        let p = d.cover_prompt();
        assert!(p.starts_with("Collection title: Linux"));
        assert!(p.contains("Source: Intro\nThe kernel"));
        assert!(p.contains("Narrated slides: Kernels"));
        assert!(p.contains("Mind map: Map — Scheduling; Memory"));

        let long = Digest {
            made: (0..200)
                .map(|i| Part {
                    kind: "Study notes",
                    title: format!("Notes {i}"),
                    parts: vec!["x".repeat(80); 4],
                })
                .collect(),
            ..Default::default()
        };
        assert!(long.made_text().chars().count() <= MADE_CHARS);
        assert_eq!(d.sources_text(), "Source: Intro\nThe kernel");
    }

    fn temp_roots(tag: &str) -> Roots {
        let base = std::env::temp_dir().join(format!("hs_coll_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        Roots {
            staging: base.join("staging"),
            maps: base.join("mindmaps"),
            notes: base.join("notes"),
            prep: base.join("prep"),
            decks: base.join("decks"),
            audio: base.join("audio"),
            sessions: base.join("sessions"),
        }
    }

    #[test]
    fn deleting_takes_every_file_of_the_collection_and_its_outputs_and_nothing_else() {
        let r = temp_roots("delete");
        let (cid, out, other) = ("s1750000000000", "s1750000001000", "s1760000000000");
        for dir in [
            r.staging.join(cid),
            r.maps.join(cid),
            r.notes.join(cid),
            r.decks.join(out).join("studio"),
            r.audio.join(out),
            r.sessions.join(out),
            r.decks.join(other),
            r.staging.join(other),
        ] {
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("f"), b"x").unwrap();
        }
        std::fs::create_dir_all(&r.prep).unwrap();
        std::fs::write(r.prep.join(format!("{out}.json")), b"{}").unwrap();
        std::fs::write(r.prep.join(format!("{other}.json")), b"{}").unwrap();

        r.remove_output(out);
        assert!(r.remove_collection(cid).unwrap());
        for gone in [
            r.staging.join(cid),
            r.maps.join(cid),
            r.notes.join(cid),
            r.decks.join(out),
            r.audio.join(out),
            r.sessions.join(out),
            r.prep.join(format!("{out}.json")),
        ] {
            assert!(!gone.exists(), "{} was left", gone.display());
        }
        for kept in [
            r.decks.join(other),
            r.staging.join(other),
            r.prep.join(format!("{other}.json")),
        ] {
            assert!(kept.exists(), "{} went too", kept.display());
        }
        assert!(
            !r.remove_collection(cid).unwrap(),
            "nothing left under it the second time"
        );
        // An output that left nothing behind is not an error.
        r.remove_output("s1");
        let _ = std::fs::remove_dir_all(r.staging.parent().unwrap());
    }
}
