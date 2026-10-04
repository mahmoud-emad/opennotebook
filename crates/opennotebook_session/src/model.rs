//! The narration store's types. See `docs/phase1-spec.md` section 2.
//!
//! Sections 3 and 4 and both of phase 2's seams read these, so a wrong shape
//! here costs a migration rather than an edit. Three of the spec's constraints
//! are enforced by the types themselves rather than by convention:
//!
//! * every line names a speaker, including in a one-speaker session
//! * ordering is explicit, never implied by position in a vector
//! * a duration is only constructible from something measured

use serde::{Deserialize, Serialize};

/// A speaker id, unique within its session. Required on every line: a store
/// that gains this field later forces a migration and a player rewrite, which
/// is why two speakers is configuration here and not a later phase.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct SpeakerId(pub String);

/// A line id, unique within its session.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct LineId(pub String);

/// Addresses one slide on disk: `<collection>/<presentation>/<slide>` under the
/// deck root, where `collection` is the session's deck directory.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlideRef {
    pub collection: String,
    pub presentation: String,
    pub slide: String,
}

/// Addresses a deck under the deck root: a collection and a presentation, and
/// nothing else.
///
/// Distinct from [`SlideRef`] on purpose. `deck_ref` used to be a `SlideRef`
/// whose `slide` was always the empty string, which §4 called out as the same
/// class of lie as a declared protocol nothing serves: a field that is always
/// empty teaches a reader to ignore it, and a player addressing the deck by that
/// ref gets nothing back. §2 already describes this field as "collection +
/// presentation", so the two-field type is what the spec asked
/// for and the three-field one was the implementation's shortcut.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeckRef {
    pub collection: String,
    pub presentation: String,
}

/// Rendered slide dimensions. Two formats, two aspect ratios: html slides are
/// 1920x1080 and png slides came back 1376x768, which is 1.792:1 rather than
/// 16:9, so a player mixing both letterboxes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Aspect {
    pub width: u32,
    pub height: u32,
}

/// How long a rendered line of audio lasts.
///
/// Constructible only from a measurement. Kokoro is not reproducible per
/// render: the same voice and text produced a 6 ms length difference across two
/// renders, so a duration carried over from a previous render is wrong by an
/// unbounded amount. There is deliberately no `from_estimate` and no `From<u64>`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct DurationMs(u64);

impl DurationMs {
    /// The only constructor: the length of the WAV that will actually be played.
    pub fn measured_from_wav(millis: u64) -> Self {
        Self(millis)
    }

    pub fn millis(self) -> u64 {
        self.0
    }
}

/// One subtitle-sized span within a line's audio.
///
/// Empty in phase 1, and this type does not name its eventual producer.
/// `transcribe_cues` works but returns 11.6 s granularity on synthesised
/// speech, splitting mid-sentence, because TTS output has no pauses for VAD to
/// find. That is a property of the input, not a tuning problem. See section 2.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Cue {
    pub at_secs: f64,
    pub end_secs: f64,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Speaker {
    pub speaker_id: SpeakerId,
    /// A Kokoro voice id, for example `af_bella` or `am_adam`.
    pub voice_id: String,
    pub display_name: String,
    /// Free text. Phase 1 does not interpret it.
    pub role: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NarrationLine {
    pub line_id: LineId,
    /// Required, always. Not an `Option`, so a one-speaker session names its
    /// one speaker rather than leaving the field to be filled in later.
    pub speaker_id: SpeakerId,
    /// Ordinal within the slide. Explicit, so a reordering is a data change
    /// rather than a vector permutation nobody can review.
    pub ordinal: u32,
    pub text: String,
    /// Set once the line is synthesised; absent until then.
    pub audio_path: Option<String>,
    /// Measured from `audio_path`'s WAV header. Absent until synthesis.
    pub duration_ms: Option<DurationMs>,
    /// Empty in phase 1. See [`Cue`].
    #[serde(default)]
    pub cues: Vec<Cue>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionSlide {
    pub slide_ref: SlideRef,
    pub ordinal: u32,
    pub aspect: Aspect,
    pub lines: Vec<NarrationLine>,
    /// The slide's own title, as it appears ON the slide.
    ///
    /// Distinct from `slide_ref.slide`, which is a slug derived from it and is
    /// an identifier — `slide_1_attention_is_all` is a key, not a heading. The
    /// outline has always produced a real title; until this field existed it was
    /// spent on the slug and then discarded, so the deck renderer received no
    /// title at all and invented one.
    #[serde(default)]
    pub title: String,
    /// The exact copy that appears on the slide, one element per entry.
    ///
    /// Element lines in the slide spec's form — `Point: …`, `Subhead: …`,
    /// `Stat: … — …`, `[image: …] — …`. NOT the narration: what is spoken and
    /// what is printed are different texts with different budgets, and sending
    /// the spoken lines as slide copy is what made the renderer improvise.
    ///
    /// Empty is legitimate and means "title only" — a slide with no elements
    /// still renders, where a fabricated one is worse than a bare one.
    #[serde(default)]
    pub on_slide: Vec<String>,
}

impl SessionSlide {
    /// Lines in `ordinal` order. Never rely on vector position: the store round
    /// trips through JSON and a caller may append out of order.
    pub fn lines_in_order(&self) -> Vec<&NarrationLine> {
        let mut out: Vec<&NarrationLine> = self.lines.iter().collect();
        out.sort_by_key(|l| l.ordinal);
        out
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionState {
    Preparing,
    Ready,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Session {
    /// Names the session directory and the memory collection too. One
    /// value, three roles, so there is nothing to keep in sync.
    pub sid: String,
    pub title: String,
    pub collection_name: String,
    /// Unused, and always `None` for a session built now. Kept so rows that
    /// carry it still decode: it held an id an earlier retrieval service
    /// needed to delete a collection, which is now deleted by name.
    pub collection_sid: Option<String>,
    /// The deck under the deck root, once section 3 has built one.
    pub deck_ref: Option<DeckRef>,
    pub speakers: Vec<Speaker>,
    pub slides: Vec<SessionSlide>,
    pub state: SessionState,
    /// The job that prepared this session.
    pub prep_job_sid: Option<String>,
    /// Why a `Failed` session failed, in the words the prep actually produced.
    ///
    /// `Some` only alongside [`SessionState::Failed`]. The state on its own was
    /// all a caller ever got, and one enum variant cannot tell an exhausted
    /// provider quota from a theme name collision from a source that fetched a
    /// bot-check page instead of a document — all three happened on this box in
    /// one afternoon, and all three reached the person as the same sentence.
    /// The reason exists at the moment of failure; nothing but this field was
    /// keeping it.
    #[serde(default)]
    pub failure: Option<String>,
    /// Whether the person pinned this session to the gallery's featured row.
    ///
    /// `#[serde(default)]` so every session stored before this field existed
    /// loads as unpinned rather than failing to decode. Nothing derives it: it
    /// is a decision, and the featured row falls back to recency for the places
    /// nobody has claimed.
    #[serde(default)]
    pub pinned: bool,
    /// What makes this an audio overview: its format, length and focus. None
    /// is a narrated slide session, which is every session stored before audio
    /// overviews existed. An audio overview has the same parts and lines, the
    /// parts heard as chapters, and no deck.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio: Option<AudioSpec>,
    /// The studio collection this output was built from: the cid whose staged
    /// sources it read. Not `collection_name` / `collection_sid` above, which
    /// name the memory collection ingest made for this one session.
    ///
    /// None on every session stored before collections existed. Such a session
    /// is a collection of its own under its own sid, which is what
    /// [`Session::collection_id`] answers, so no stored row needs rewriting.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub collection: Option<String>,
    /// What the prep's model calls cost, in USD: the provider-reported cost of
    /// each call, or its tokens at the catalog price when none was reported.
    /// None when it is not known: a row written before this was recorded, or
    /// a build with a call that could be priced neither way, whose sum would
    /// understate it. The Q&A extraction while indexing the sources is in it:
    /// those calls go through the same client and are recorded with the rest.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spent_usd: Option<f64>,
    /// The slide style id a deck was asked for, so a retry builds it in the
    /// same look. None on an audio overview and on rows stored before it was
    /// kept, which a retry then builds in the default style.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub style: Option<String>,
}

/// A studio collection: one set of staged sources and everything made from
/// them. The session oschema's collections section has the concept.
///
/// Only what cannot be derived is stored. The sources are the files under
/// `staging/<cid>`, the decks and audio overviews are session rows naming the
/// cid, and the maps and notes are files keyed by it; counting them here would
/// be a second record of each, and the first to drift.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Collection {
    pub cid: String,
    /// Empty until something names it.
    #[serde(default)]
    pub title: String,
    /// The studio names it from its sources while this is true. Typing a title
    /// turns it off; retitling with an empty one turns it back on.
    #[serde(default = "yes")]
    pub title_auto: bool,
    #[serde(default)]
    pub created_ms: u64,
    #[serde(default)]
    pub updated_ms: u64,
    #[serde(default)]
    pub pinned: bool,
    /// The source set the current automatic title was made from, as its sorted
    /// file names. A touch that did not change the set must not spend a model
    /// call renaming the collection to what it is already called.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub titled_from: String,
    /// The sids of its decks and audio overviews, so one collection is read
    /// without scanning every session row. Kept by `session_prepare` and
    /// `session_delete`. None on a row written before the index existed, which
    /// is indexed from a scan the first time it is read.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outputs: Option<Vec<String>>,
    /// The cover a model designed from what the collection holds. None until
    /// one was made, and the server draws a cover from the cid and title then.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cover: Option<CoverSpec>,
    /// A hash of the content the cover was last designed from: the source
    /// names and the ready outputs. Set on a failed attempt too, so a model
    /// that is down is not asked again until the content changes.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub cover_from: String,
}

/// A collection's cover as the model designs it: words and choices from short
/// fixed lists, never markup. The server validates it and draws it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoverSpec {
    pub topic: String,
    pub terms: Vec<String>,
    pub motif: String,
    pub palette: String,
    pub layout: String,
}

fn yes() -> bool {
    true
}

impl Collection {
    /// A fresh, untitled collection: the studio will name it.
    pub fn new(cid: &str, now_ms: u64) -> Self {
        Self {
            cid: cid.to_string(),
            title: String::new(),
            title_auto: true,
            created_ms: now_ms,
            updated_ms: now_ms,
            pinned: false,
            titled_from: String::new(),
            outputs: Some(Vec::new()),
            cover: None,
            cover_from: String::new(),
        }
    }
}

/// An audio overview's shape. NotebookLM's four formats and three lengths;
/// see `docs/audio-overview-spec.md`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AudioSpec {
    pub format: AudioFormat,
    pub length: AudioLength,
    /// What the person asked it to centre on, in their own words: NotebookLM's
    /// Customize box. Empty when nothing.
    #[serde(default)]
    pub focus: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioFormat {
    /// Two hosts in conversation, unpacking the sources. The default.
    DeepDive,
    /// One host, a short summary of the key points.
    Brief,
    /// Two hosts reviewing the sources as an expert would, with constructive feedback.
    Critique,
    /// Two hosts arguing different positions the sources support.
    Debate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioLength {
    Shorter,
    Default,
    Longer,
}

impl AudioFormat {
    pub const ALL: [AudioFormat; 4] = [
        AudioFormat::DeepDive,
        AudioFormat::Brief,
        AudioFormat::Critique,
        AudioFormat::Debate,
    ];

    pub fn id(self) -> &'static str {
        match self {
            AudioFormat::DeepDive => "deep_dive",
            AudioFormat::Brief => "brief",
            AudioFormat::Critique => "critique",
            AudioFormat::Debate => "debate",
        }
    }

    /// From its id; anything else is the default, Deep Dive.
    pub fn parse(s: &str) -> Self {
        Self::ALL
            .into_iter()
            .find(|f| f.id() == s.trim())
            .unwrap_or(AudioFormat::DeepDive)
    }

    pub fn label(self) -> &'static str {
        match self {
            AudioFormat::DeepDive => "Deep Dive",
            AudioFormat::Brief => "Brief",
            AudioFormat::Critique => "Critique",
            AudioFormat::Debate => "Debate",
        }
    }

    /// How many voices it takes. Brief is one host, as in NotebookLM.
    pub fn speakers(self) -> usize {
        match self {
            AudioFormat::Brief => 1,
            _ => 2,
        }
    }

    /// The lengths it offers. NotebookLM: all three for Deep Dive, Shorter and
    /// Default for Critique and Debate, none for Brief.
    pub fn lengths(self) -> &'static [AudioLength] {
        match self {
            AudioFormat::DeepDive => &[
                AudioLength::Shorter,
                AudioLength::Default,
                AudioLength::Longer,
            ],
            AudioFormat::Brief => &[AudioLength::Default],
            _ => &[AudioLength::Shorter, AudioLength::Default],
        }
    }
}

impl AudioLength {
    pub fn id(self) -> &'static str {
        match self {
            AudioLength::Shorter => "shorter",
            AudioLength::Default => "default",
            AudioLength::Longer => "longer",
        }
    }

    pub fn parse(s: &str) -> Self {
        match s.trim() {
            "shorter" => AudioLength::Shorter,
            "longer" => AudioLength::Longer,
            _ => AudioLength::Default,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            AudioLength::Shorter => "Shorter",
            AudioLength::Default => "Default",
            AudioLength::Longer => "Longer",
        }
    }
}

impl AudioSpec {
    /// The length it really gets: one its format does not offer falls back to
    /// Default.
    pub fn length(&self) -> AudioLength {
        if self.format.lengths().contains(&self.length) {
            self.length
        } else {
            AudioLength::Default
        }
    }

    /// About how many minutes of audio. Brief is "under two minutes"; a Deep
    /// Dive defaults near NotebookLM's typical ten to twelve.
    pub fn minutes(&self) -> u32 {
        match (self.format, self.length()) {
            (AudioFormat::Brief, _) => 2,
            (AudioFormat::DeepDive, AudioLength::Shorter) => 5,
            (AudioFormat::DeepDive, AudioLength::Default) => 10,
            (AudioFormat::DeepDive, AudioLength::Longer) => 16,
            (_, AudioLength::Shorter) => 5,
            (_, _) => 8,
        }
    }

    /// How many chapters. Each one is a part of the plan with its own points.
    pub fn chapters(&self) -> usize {
        match (self.format, self.length()) {
            (AudioFormat::Brief, _) => 2,
            (_, AudioLength::Shorter) => 3,
            (AudioFormat::DeepDive, AudioLength::Default) => 5,
            (AudioFormat::DeepDive, AudioLength::Longer) => 6,
            (_, _) => 4,
        }
    }
}

impl Session {
    /// The collection this session belongs to: the one it names, or its own
    /// sid for a session made before collections.
    pub fn collection_id(&self) -> &str {
        self.collection
            .as_deref()
            .filter(|c| !c.is_empty())
            .unwrap_or(&self.sid)
    }

    /// Slides in `ordinal` order.
    pub fn slides_in_order(&self) -> Vec<&SessionSlide> {
        let mut out: Vec<&SessionSlide> = self.slides.iter().collect();
        out.sort_by_key(|s| s.ordinal);
        out
    }

    /// `speaker_count` is derived, not stored: a stored count can disagree with
    /// the speakers actually present, and then two fields describe one fact.
    pub fn speaker_count(&self) -> usize {
        self.speakers.len()
    }

    /// Every `speaker_id` used by a line exists in `speakers`. The type makes a
    /// line name a speaker; only this can check the name resolves.
    pub fn dangling_speaker_ids(&self) -> Vec<&SpeakerId> {
        let known: std::collections::HashSet<_> =
            self.speakers.iter().map(|s| &s.speaker_id).collect();
        self.slides
            .iter()
            .flat_map(|s| s.lines.iter())
            .map(|l| &l.speaker_id)
            .filter(|id| !known.contains(id))
            .collect()
    }
}
