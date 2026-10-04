//! Operator settings: `settings.toml` in the data directory, with environment
//! overrides.
//!
//! This is the one place settings are read, rather than the crate that happens
//! to need one first, because three crates need them and three copies of a
//! lookup is how two of them end up reading different keys.
//!
//! A setting is looked up in this order: an environment variable of the same
//! name (`OPENNOTEBOOK_SCRIPT_MODEL=...`), then the file, then the catalogue
//! default. Every setting has a default that is a working value, so an empty
//! file runs. A value that is present and non-empty wins; anything else falls
//! back without complaint, because a missing operator override is not an error.
//!
//! The file is a flat table of strings, written by the settings dialog:
//!
//! ```toml
//! OPENNOTEBOOK_SCRIPT_MODEL = "anthropic/claude-haiku-4.5"
//! OPENNOTEBOOK_LANGUAGE = "French"
//! ```
//!
//! It is read on every lookup, so a prep child sees a change the moment the
//! dialog saves it, and written whole through a temp file and a rename, mode
//! 0600 because it holds the API key.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;

use serde::Serialize;

/// The model that writes outlines, slide scripts and the session's bookends.
///
/// Measured against the real prompt on this box, scored on what
/// `generate.rs` actually enforces — parseable speaker lines, both speakers
/// used, the 320/650 character budgets, no invented proper nouns:
///
/// | model | p50 | $/slide | both speakers |
/// |---|---|---|---|
/// | `amazon/nova-micro-v1` | 1,083 ms | $0.00002 | yes |
/// | `google/gemini-2.5-flash-lite` | 860 ms | $0.00005 | **no, host only** |
/// | `deepseek/deepseek-v4-flash-0731` | 13,575 ms | $0.00006 | intermittent |
/// | `qwen/qwen3.7-flash` | 31,365 ms | $0.00032 | yes |
/// | `openai/gpt-5.4` (previous default) | — | ~$0.00825 | — |
///
/// A model that quietly writes every line as one speaker passes every format
/// check and turns a two-voice session into a monologue, which is why the
/// speaker column decided this and not the clock.
///
/// The default moved to Claude Haiku 4.5 on 2026-10-03. The table above
/// scored format, not content, and on content nova-micro was the limit:
/// built side by side from the Moshi paper with the same prompts, it
/// restated one fact on three slides, used phrases the prompt bans by name,
/// and named a planet its source never mentioned, where Haiku wrote a debate
/// that kept its sides and conceded real points. A 5-minute session's script
/// costs a few cents on Haiku against a fraction of one, still well inside
/// the per-session limit. See `docs/audio-overview-spec.md` section 4.
pub const SCRIPT_MODEL_KEY: &str = "OPENNOTEBOOK_SCRIPT_MODEL";
pub const SCRIPT_MODEL_DEFAULT: &str = "anthropic/claude-haiku-4.5";

/// The model that answers a spoken question. Audio in, TEXT out.
///
/// It is still an audio model because the question goes up as audio and is
/// never transcribed in front of it. What it no longer does is answer in audio:
/// `opennotebook_server::ask` synthesises the reply locally so it is spoken by
/// the narrator who was interrupted, which no audio-out model can do.
///
/// `gpt-audio-mini` stays the default. It is $0.0006 a question, and the audio
/// half of that price was the expensive half, so text out is cheaper again. A
/// replacement has to accept audio input; a text-only model would need a
/// transcription step in front of it, which is the 31 second pipeline the ask
/// route exists to avoid.
pub const ANSWER_MODEL_KEY: &str = "OPENNOTEBOOK_ANSWER_MODEL";
pub const ANSWER_MODEL_DEFAULT: &str = "openai/gpt-audio-mini";

/// The model that turns a draft's sources into a mind map.
///
/// Not the script model: a map is made from the WHOLE text of every source in
/// one call, and the largest real draft is about 50k tokens, past what
/// `nova-micro` takes. Gemini 2.5 Flash Lite has a 1M window and is the chat
/// agent's model already. See docs/mindmap-spec.md section 0.
pub const MINDMAP_MODEL_KEY: &str = "OPENNOTEBOOK_MINDMAP_MODEL";
pub const MINDMAP_MODEL_DEFAULT: &str = "google/gemini-2.5-flash-lite";

/// The model that writes study notes. It reads every source whole, as the mind
/// map's does, and then has to keep hundreds of numbered passages straight
/// while it cites them, so it needs the same long window.
pub const NOTES_MODEL_KEY: &str = "OPENNOTEBOOK_NOTES_MODEL";
pub const NOTES_MODEL_DEFAULT: &str = "google/gemini-2.5-flash-lite";

// ── the catalogue ────────────────────────────────────────────────────────────
//
// Every setting the studio has, in one table. The settings dialog is drawn
// from this table and the server validates writes against it, so the page
// cannot offer a value the studio would not accept, and a setting added here
// appears in the dialog without touching the page.

pub const TAB_DEFAULTS: &str = "Generation defaults";
pub const TAB_VOICES: &str = "Voices";
pub const TAB_LANGUAGE: &str = "Language";
pub const TAB_CONVERSATION: &str = "Live conversation";
pub const TAB_COSTS: &str = "Costs & limits";
pub const TAB_MODELS: &str = "Models";

/// The settings dialog's server tabs, in display order. Appearance is the
/// page's own tab, drawn before these, because the theme is this browser's.
pub const TABS: &[&str] = &[
    TAB_DEFAULTS,
    TAB_VOICES,
    TAB_LANGUAGE,
    TAB_CONVERSATION,
    TAB_COSTS,
    TAB_MODELS,
];

/// One tab as the dialog describes it: a stable id for links such as
/// "Settings › Costs & limits", its label, and a note shown above its rows.
#[derive(Debug, Clone, Copy)]
pub struct Tab {
    pub id: &'static str,
    pub label: &'static str,
    pub note: &'static str,
    /// Drawn collapsed until opened.
    pub advanced: bool,
}

pub const TAB_INFO: &[Tab] = &[
    Tab {
        id: "defaults",
        label: TAB_DEFAULTS,
        note: "",
        advanced: false,
    },
    Tab {
        id: "voices",
        label: TAB_VOICES,
        note: "",
        advanced: false,
    },
    Tab {
        id: "language",
        label: TAB_LANGUAGE,
        note: "",
        advanced: false,
    },
    Tab {
        id: "conversation",
        label: TAB_CONVERSATION,
        note: "",
        advanced: false,
    },
    Tab {
        id: "costs",
        label: TAB_COSTS,
        note: "",
        advanced: false,
    },
    Tab {
        id: "models",
        label: TAB_MODELS,
        note: "Changing these affects quality and cost.",
        advanced: true,
    },
];

/// What kind of control edits a setting, and what it accepts.
#[derive(Debug, Clone, Copy)]
pub enum Kind {
    /// One of a fixed list of `(value, label)` pairs.
    Choice(&'static [(&'static str, &'static str)]),
    /// One of the slide styles. The list lives in `opennotebook_sdk`, which this
    /// crate does not depend on, so the server fills the options in.
    Style,
    /// A whole number in a range.
    Number { min: i64, max: i64 },
    /// `on` or `off`.
    Toggle,
    /// Free text, with suggestions.
    Text(&'static [&'static str]),
    /// A model id: one of the tested `(id, label)` pairs, or any id the
    /// AI endpoint's catalogue lists, which the server checks on save.
    Model(&'static [(&'static str, &'static str)]),
}

#[derive(Debug, Clone, Copy)]
pub struct Def {
    /// The name this setting is stored under, in the file and the environment.
    pub key: &'static str,
    pub tab: &'static str,
    /// A heading inside the tab that rows sharing it sit under; empty for none.
    pub group: &'static str,
    pub label: &'static str,
    pub help: &'static str,
    pub kind: Kind,
    /// Empty means "no override": the consumer decides, as documented in `help`.
    pub default: &'static str,
    /// What a number counts, shown beside it ("slides", "min"); empty for none.
    pub unit: &'static str,
    /// Drawn in a collapsed "Advanced" section.
    pub advanced: bool,
}

impl Def {
    const fn new(
        key: &'static str,
        tab: &'static str,
        label: &'static str,
        help: &'static str,
        kind: Kind,
        default: &'static str,
    ) -> Self {
        Self {
            key,
            tab,
            group: "",
            label,
            help,
            kind,
            default,
            unit: "",
            advanced: false,
        }
    }

    const fn group(mut self, group: &'static str) -> Self {
        self.group = group;
        self
    }

    const fn unit(mut self, unit: &'static str) -> Self {
        self.unit = unit;
        self
    }

    const fn advanced(mut self) -> Self {
        self.advanced = true;
        self
    }
}

/// The voices offered, by their Kokoro names, which Speaches and Kokoro-FastAPI
/// both accept. A server that does not know an id may fall back to a default
/// voice silently, which is why the dialog offers only these.
pub const VOICES: &[(&str, &str)] = &[
    ("af_bella", "Bella (US, female)"),
    ("af_nicole", "Nicole (US, female)"),
    ("af_sarah", "Sarah (US, female)"),
    ("af_sky", "Sky (US, female)"),
    ("am_adam", "Adam (US, male)"),
    ("am_michael", "Michael (US, male)"),
    ("bf_emma", "Emma (UK, female)"),
    ("bf_isabella", "Isabella (UK, female)"),
    ("bm_george", "George (UK, male)"),
    ("bm_lewis", "Lewis (UK, male)"),
];

/// Languages the narration and answers can be written in, with the BCP 47 tag
/// the browser's speech recogniser wants for the same language.
pub const LANGUAGES: &[(&str, &str)] = &[
    ("English", "en-US"),
    ("Arabic", "ar-EG"),
    ("Chinese", "zh-CN"),
    ("Dutch", "nl-NL"),
    ("French", "fr-FR"),
    ("German", "de-DE"),
    ("Hindi", "hi-IN"),
    ("Italian", "it-IT"),
    ("Japanese", "ja-JP"),
    ("Portuguese", "pt-BR"),
    ("Spanish", "es-ES"),
];
const LANGUAGE_CHOICES: &[(&str, &str)] = &[
    ("English", "English"),
    ("Arabic", "Arabic"),
    ("Chinese", "Chinese"),
    ("Dutch", "Dutch"),
    ("French", "French"),
    ("German", "German"),
    ("Hindi", "Hindi"),
    ("Italian", "Italian"),
    ("Japanese", "Japanese"),
    ("Portuguese", "Portuguese"),
    ("Spanish", "Spanish"),
];

pub const SLIDE_COUNT_KEY: &str = "OPENNOTEBOOK_SLIDE_COUNT";
pub const STYLE_KEY: &str = "OPENNOTEBOOK_STYLE";
pub const AUDIO_FORMAT_KEY: &str = "OPENNOTEBOOK_AUDIO_FORMAT";
pub const AUDIO_LENGTH_KEY: &str = "OPENNOTEBOOK_AUDIO_LENGTH";
pub const RESEARCH_DEPTH_KEY: &str = "OPENNOTEBOOK_RESEARCH_DEPTH";
pub const AUTO_NAME_KEY: &str = "OPENNOTEBOOK_AUTO_NAME";
pub const COVERS_KEY: &str = "OPENNOTEBOOK_COVERS";
pub const SPEAKER_COUNT_KEY: &str = "OPENNOTEBOOK_SPEAKER_COUNT";
pub const SPEAKER1_NAME_KEY: &str = "OPENNOTEBOOK_SPEAKER1_NAME";
pub const SPEAKER1_VOICE_KEY: &str = "OPENNOTEBOOK_SPEAKER1_VOICE";
pub const SPEAKER1_ROLE_KEY: &str = "OPENNOTEBOOK_SPEAKER1_ROLE";
pub const SPEAKER2_NAME_KEY: &str = "OPENNOTEBOOK_SPEAKER2_NAME";
pub const SPEAKER2_VOICE_KEY: &str = "OPENNOTEBOOK_SPEAKER2_VOICE";
pub const SPEAKER2_ROLE_KEY: &str = "OPENNOTEBOOK_SPEAKER2_ROLE";
pub const CHAT_MODEL_KEY: &str = "OPENNOTEBOOK_CHAT_MODEL";
pub const SEARCH_MODEL_KEY: &str = "OPENNOTEBOOK_SEARCH_MODEL";
pub const LANGUAGE_KEY: &str = "OPENNOTEBOOK_LANGUAGE";
pub const ANSWER_LENGTH_KEY: &str = "OPENNOTEBOOK_ANSWER_LENGTH";
pub const COURTESY_KEY: &str = "OPENNOTEBOOK_COURTESY";
pub const INTERRUPT_KEY: &str = "OPENNOTEBOOK_INTERRUPT";
pub const PATIENCE_KEY: &str = "OPENNOTEBOOK_TURN_PATIENCE";
pub const SESSION_MINUTES_KEY: &str = "OPENNOTEBOOK_SESSION_MINUTES";
pub const SLIDE_MODEL_KEY: &str = "OPENNOTEBOOK_SLIDE_MODEL";
pub const MAX_BUILD_USD_KEY: &str = "OPENNOTEBOOK_MAX_BUILD_USD";
pub const SHOW_COST_KEY: &str = "OPENNOTEBOOK_SHOW_COST";

/// How many slides a deck may have. One range for the setting and for every
/// build request, the dialog's and an agent's alike; a request outside it is
/// clamped into it, see [`clamp_slides`].
pub const SLIDES_MIN: i64 = 3;
pub const SLIDES_MAX: i64 = 12;
pub const SLIDES_DEFAULT: i64 = 5;

/// A deck's slide count, brought into [`SLIDES_MIN`]..=[`SLIDES_MAX`].
pub fn clamp_slides(n: i64) -> i64 {
    n.clamp(SLIDES_MIN, SLIDES_MAX)
}

/// The model that writes the slides as HTML, figures included.
///
/// Compared in Slide Lab against the earlier slide service it replaced: Claude Haiku
/// 4.5 and Claude Sonnet 5.5 both drew on-style, readable slides; Haiku does a
/// five-slide deck for about $0.05 on OpenRouter's 2026-10-02 prices.
pub const SLIDE_MODEL_DEFAULT: &str = "anthropic/claude-haiku-4.5";
const SLIDE_MODELS: &[(&str, &str)] = &[
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"),
];

const SCRIPT_MODELS: &[(&str, &str)] = &[
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("amazon/nova-micro-v1", "Amazon Nova Micro"),
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("qwen/qwen3.7-flash", "Qwen 3.7 Flash"),
    ("openai/gpt-5.4", "GPT-5.4"),
];
/// Long-context models for the steps that read every source whole.
const LONG_MODELS: &[(&str, &str)] = &[
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("openai/gpt-5.4", "GPT-5.4"),
];
/// Models that call tools reliably, which the add-sources assistant needs.
const CHAT_MODELS: &[(&str, &str)] = &[
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("openai/gpt-5.4", "GPT-5.4"),
];
const ANSWER_MODELS: &[(&str, &str)] = &[
    ("openai/gpt-audio-mini", "GPT Audio Mini"),
    ("openai/gpt-audio", "GPT Audio"),
];
/// Search models that return the pages they cite, which is what a search
/// result is made from.
const SEARCH_MODELS: &[(&str, &str)] = &[
    ("perplexity/sonar", "Perplexity Sonar"),
    ("perplexity/sonar-pro", "Perplexity Sonar Pro"),
];

/// The model the web search runs on: Perplexity's Sonar does a real web
/// search behind a chat completion and returns the URLs it found.
pub const SEARCH_MODEL_DEFAULT: &str = "perplexity/sonar";

/// The model the chat and Ask answers, the add-sources assistant and
/// collection naming run on. The script model's default is too small to call
/// tools reliably; this one is cheap and does.
pub const AGENT_MODEL_DEFAULT: &str = "google/gemini-2.5-flash-lite";

pub const AUDIO_FORMATS: &[(&str, &str)] = &[
    ("deep_dive", "Deep Dive"),
    ("brief", "Brief"),
    ("critique", "Critique"),
    ("debate", "Debate"),
];
pub const AUDIO_LENGTHS: &[(&str, &str)] = &[
    ("shorter", "Shorter"),
    ("default", "Default"),
    ("longer", "Longer"),
];
pub const RESEARCH_DEPTHS: &[(&str, &str)] = &[
    ("quick", "Quick (about 1 min)"),
    ("standard", "Standard (a few min)"),
];

const WRITING: &str = "Writing";
const CHAT: &str = "Chat & answers";
const HOST: &str = "Host";
const SECOND: &str = "Second voice";

pub const CATALOGUE: &[Def] = &[
    // Generation defaults
    Def::new(
        STYLE_KEY,
        TAB_DEFAULTS,
        "Slide style",
        "Starting style for new decks. You can pick another when you create one.",
        Kind::Style,
        "editorial",
    ),
    Def::new(
        SLIDE_COUNT_KEY,
        TAB_DEFAULTS,
        "Slides per deck",
        "Decks only. Audio overviews set their own length.",
        Kind::Number {
            min: SLIDES_MIN,
            max: SLIDES_MAX,
        },
        "5",
    )
    .unit("slides"),
    Def::new(
        SESSION_MINUTES_KEY,
        TAB_DEFAULTS,
        "Deck narration length",
        "About how long the spoken script runs. Decks only.",
        Kind::Number { min: 2, max: 20 },
        "5",
    )
    .unit("min"),
    Def::new(
        AUDIO_FORMAT_KEY,
        TAB_DEFAULTS,
        "Audio overview format",
        "Starting format in the Create panel.",
        Kind::Choice(AUDIO_FORMATS),
        "deep_dive",
    ),
    Def::new(
        AUDIO_LENGTH_KEY,
        TAB_DEFAULTS,
        "Audio overview length",
        "Brief is always about 2 minutes.",
        Kind::Choice(AUDIO_LENGTHS),
        "default",
    ),
    Def::new(
        RESEARCH_DEPTH_KEY,
        TAB_DEFAULTS,
        "Web research depth",
        "How long Research a topic reads the web before it writes its report.",
        Kind::Choice(RESEARCH_DEPTHS),
        "standard",
    ),
    Def::new(
        AUTO_NAME_KEY,
        TAB_DEFAULTS,
        "Name collections automatically",
        "Titles a new collection from its sources. Your own title is never replaced.",
        Kind::Toggle,
        "on",
    ),
    Def::new(
        COVERS_KEY,
        TAB_DEFAULTS,
        "Design collection covers",
        "Draws each collection's cover from its sources and what you made. One small model call when they change, well under a cent.",
        Kind::Toggle,
        "on",
    ),
    // Voices
    Def::new(
        SPEAKER_COUNT_KEY,
        TAB_VOICES,
        "Speakers in a deck",
        "Automatic uses two voices when there are two or more sources. Audio overview formats choose their own.",
        Kind::Choice(&[("auto", "Automatic"), ("1", "One"), ("2", "Two")]),
        "auto",
    ),
    Def::new(
        SPEAKER1_NAME_KEY,
        TAB_VOICES,
        "Host name",
        "Shown beside everything they say, and used when they answer a question.",
        Kind::Text(&[]),
        "Host",
    )
    .group(HOST),
    Def::new(
        SPEAKER1_VOICE_KEY,
        TAB_VOICES,
        "Host voice",
        "Local English voice.",
        Kind::Choice(VOICES),
        "af_bella",
    )
    .group(HOST),
    Def::new(
        SPEAKER1_ROLE_KEY,
        TAB_VOICES,
        "Host persona",
        "A few words on who they are and how they talk.",
        Kind::Text(&[]),
        "narrator",
    )
    .group(HOST),
    Def::new(
        SPEAKER2_NAME_KEY,
        TAB_VOICES,
        "Second voice name",
        "Used for two-voice decks and Deep Dive, Critique and Debate.",
        Kind::Text(&[]),
        "Expert",
    )
    .group(SECOND),
    Def::new(
        SPEAKER2_VOICE_KEY,
        TAB_VOICES,
        "Second voice",
        "Local English voice.",
        Kind::Choice(VOICES),
        "am_adam",
    )
    .group(SECOND),
    Def::new(
        SPEAKER2_ROLE_KEY,
        TAB_VOICES,
        "Second voice persona",
        "A few words on who they are and how they talk.",
        Kind::Text(&[]),
        "expert",
    )
    .group(SECOND),
    // Language
    Def::new(
        LANGUAGE_KEY,
        TAB_LANGUAGE,
        "Output language",
        "Scripts, notes, maps, answers and chat are written in this. Voices stay English-accented.",
        Kind::Choice(LANGUAGE_CHOICES),
        "English",
    ),
    // Live conversation
    Def::new(
        ANSWER_LENGTH_KEY,
        TAB_CONVERSATION,
        "Answer length",
        "How long a spoken answer to your question is.",
        Kind::Choice(&[
            ("short", "Short, one sentence"),
            ("normal", "Normal, one or two sentences"),
            ("detailed", "Detailed, three or four sentences"),
        ]),
        "normal",
    ),
    Def::new(
        PATIENCE_KEY,
        TAB_CONVERSATION,
        "Waiting for you to finish",
        "How long the studio waits after a pause that sounds unfinished, such as one after \"and\" or \"um\", before it answers. Takes effect next time you open the player.",
        Kind::Choice(&[
            ("quick", "Quick, 1 second"),
            ("balanced", "Balanced, 2 seconds"),
            ("patient", "Patient, 4 seconds"),
        ]),
        "balanced",
    ),
    Def::new(
        INTERRUPT_KEY,
        TAB_CONVERSATION,
        "Let me talk over the studio",
        "When on, starting to speak while the studio is talking stops it and gives you the floor. Takes effect next time you open the player.",
        Kind::Toggle,
        "on",
    ),
    Def::new(
        COURTESY_KEY,
        TAB_CONVERSATION,
        "Courtesy lines",
        "The short \"go ahead\" and \"good question\" lines around a question. Skipped when the output language is not English.",
        Kind::Toggle,
        "on",
    ),
    // Costs & limits
    Def::new(
        MAX_BUILD_USD_KEY,
        TAB_COSTS,
        "Spending limit per output",
        "A deck or audio overview whose highest estimate is over this is refused before it starts. Mind maps and notes cost under a cent and are not limited.",
        Kind::Choice(&[
            ("0.25", "$0.25"),
            ("0.50", "$0.50"),
            ("1", "$1"),
            ("2", "$2"),
            ("5", "$5"),
            ("off", "No limit"),
        ]),
        "0.50",
    ),
    Def::new(
        SHOW_COST_KEY,
        TAB_COSTS,
        "Show cost before generating",
        "Shows the estimate in the Create panel. A build over your limit is always flagged.",
        Kind::Toggle,
        "on",
    ),
    // Models
    Def::new(
        SCRIPT_MODEL_KEY,
        TAB_MODELS,
        "Script model",
        "Writes the outline and every spoken line. It must keep two speakers apart.",
        Kind::Model(SCRIPT_MODELS),
        SCRIPT_MODEL_DEFAULT,
    )
    .group(WRITING)
    .advanced(),
    Def::new(
        SLIDE_MODEL_KEY,
        TAB_MODELS,
        "Slide model",
        "Writes every slide and draws its figure. Sonnet takes more care over layouts and costs about twice as much as Haiku.",
        Kind::Model(SLIDE_MODELS),
        SLIDE_MODEL_DEFAULT,
    )
    .group(WRITING)
    .advanced(),
    Def::new(
        MINDMAP_MODEL_KEY,
        TAB_MODELS,
        "Mind map model",
        "Reads every source whole and draws the mind map. It needs a long context window.",
        Kind::Model(LONG_MODELS),
        MINDMAP_MODEL_DEFAULT,
    )
    .group(WRITING)
    .advanced(),
    Def::new(
        NOTES_MODEL_KEY,
        TAB_MODELS,
        "Study notes model",
        "Reads every source whole and writes notes that cite their passages. It needs a long context window.",
        Kind::Model(LONG_MODELS),
        NOTES_MODEL_DEFAULT,
    )
    .group(WRITING)
    .advanced(),
    Def::new(
        CHAT_MODEL_KEY,
        TAB_MODELS,
        "Chat & Ask model",
        "Answers in Ask, runs the add-sources assistant and names collections. Empty uses Gemini 2.5 Flash Lite.",
        Kind::Model(CHAT_MODELS),
        AGENT_MODEL_DEFAULT,
    )
    .group(CHAT)
    .advanced(),
    Def::new(
        ANSWER_MODEL_KEY,
        TAB_MODELS,
        "Spoken answer model",
        "Answers questions asked aloud in the player. It must accept audio input.",
        Kind::Model(ANSWER_MODELS),
        ANSWER_MODEL_DEFAULT,
    )
    .group(CHAT)
    .advanced(),
    Def::new(
        SEARCH_MODEL_KEY,
        TAB_MODELS,
        "Web search model",
        "Finds pages for the add-sources assistant. It must return the pages it cites.",
        Kind::Model(SEARCH_MODELS),
        SEARCH_MODEL_DEFAULT,
    )
    .group(CHAT)
    .advanced(),
];

/// The catalogue entry for a key.
pub fn def(key: &str) -> Option<&'static Def> {
    CATALOGUE.iter().find(|d| d.key == key)
}

/// Whether `value` is acceptable for `d`. Styles are checked by the server,
/// which knows the list. An empty value always is: it means "use the default".
pub fn validate(d: &Def, value: &str) -> Result<(), String> {
    let v = value.trim();
    if v.is_empty() {
        return Ok(());
    }
    match d.kind {
        Kind::Choice(opts) => {
            if opts.iter().any(|(o, _)| *o == v) {
                Ok(())
            } else {
                Err(format!("`{v}` is not one of the choices for {}", d.label))
            }
        }
        Kind::Number { min, max } => match v.parse::<i64>() {
            Ok(n) if (min..=max).contains(&n) => Ok(()),
            _ => Err(format!(
                "{} must be a whole number from {min} to {max}",
                d.label
            )),
        },
        Kind::Toggle => match v {
            "on" | "off" => Ok(()),
            _ => Err(format!("{} must be on or off", d.label)),
        },
        Kind::Text(_) if v.len() > 400 => Err(format!("{} is too long", d.label)),
        Kind::Model(_) if v.len() > 200 || v.chars().any(char::is_whitespace) => Err(format!(
            "`{v}` is not a model id; ids look like `anthropic/claude-haiku-4.5`"
        )),
        Kind::Text(_) | Kind::Style | Kind::Model(_) => Ok(()),
    }
}

/// One setting as the dialog sees it.
#[derive(Debug, Clone, Serialize)]
pub struct Current {
    pub key: String,
    /// The stored override, or empty when there is none.
    pub value: String,
    pub default: String,
}

// ── reading and writing ──────────────────────────────────────────────────────

/// Read one setting, falling back to its default.
///
/// Never fails: an unreadable file, a missing key and an empty value all mean
/// the same thing to a caller, which is "use the default". The alternative is
/// a prep job that refuses to start because nobody set an override.
pub async fn setting(key: &str, default: &'static str) -> String {
    stored(key, &read_file().unwrap_or_default()).unwrap_or_else(|| default.to_string())
}

/// A catalogued setting, or its catalogue default.
pub async fn get(key: &str) -> String {
    let default = def(key).map_or("", |d| d.default);
    setting(key, default).await
}

/// The override in force for `key`: the environment, then the file.
fn stored(key: &str, file: &BTreeMap<String, String>) -> Option<String> {
    std::env::var(key)
        .ok()
        .or_else(|| file.get(key).cloned())
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// Every catalogued setting with its override.
///
/// Unlike [`setting`] this DOES fail when the file cannot be read: a dialog
/// that showed every default because the file was damaged would invite someone
/// to "fix" settings that were never lost.
pub async fn all() -> Result<Vec<Current>, String> {
    let file = read_file()?;
    Ok(CATALOGUE
        .iter()
        .map(|d| Current {
            key: d.key.to_string(),
            value: stored(d.key, &file).unwrap_or_default(),
            default: d.default.to_string(),
        })
        .collect())
}

/// Store an override, or remove it when `value` is empty or the default.
///
/// Removing rather than storing the default keeps "never changed" and "changed
/// back" the same thing, so a later change of default reaches everyone who did
/// not choose otherwise. An environment variable of the same name still wins
/// over whatever is stored here.
pub async fn set(key: &str, value: &str) -> Result<(), String> {
    let d = def(key).ok_or_else(|| format!("no setting named {key}"))?;
    validate(d, value)?;
    let v = value.trim();
    // Read-modify-write of one file: one writer at a time in this process.
    static WRITE: Mutex<()> = Mutex::new(());
    let _guard = WRITE.lock().unwrap_or_else(|p| p.into_inner());
    let mut file = read_file()?;
    if v.is_empty() || v == d.default {
        if file.remove(key).is_none() {
            return Ok(());
        }
    } else {
        file.insert(key.to_string(), v.to_string());
    }
    write_file(&crate::paths::settings_file(), &file)
}

/// The stored overrides. A file that is not there is an empty one.
fn read_file() -> Result<BTreeMap<String, String>, String> {
    let path = crate::paths::settings_file();
    match std::fs::read_to_string(&path) {
        Ok(text) => toml::from_str(&text).map_err(|e| format!("{}: {e}", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

fn write_file(path: &Path, file: &BTreeMap<String, String>) -> Result<(), String> {
    let err = |e: std::io::Error| format!("{}: {e}", path.display());
    let text = toml::to_string(file).map_err(|e| format!("{}: {e}", path.display()))?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(err)?;
    }
    let tmp = path.with_extension("toml.tmp");
    std::fs::write(&tmp, text).map_err(err)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600)).map_err(err)?;
    }
    std::fs::rename(&tmp, path).map_err(err)
}

/// The script model, or its default.
pub async fn script_model() -> String {
    setting(SCRIPT_MODEL_KEY, SCRIPT_MODEL_DEFAULT).await
}

/// The answer model, or its default.
pub async fn answer_model() -> String {
    setting(ANSWER_MODEL_KEY, ANSWER_MODEL_DEFAULT).await
}

/// The mind map model, or its default.
pub async fn mindmap_model() -> String {
    setting(MINDMAP_MODEL_KEY, MINDMAP_MODEL_DEFAULT).await
}

/// The study notes model, or its default.
pub async fn notes_model() -> String {
    setting(NOTES_MODEL_KEY, NOTES_MODEL_DEFAULT).await
}

/// The model behind Ask, the add-sources assistant and collection naming:
/// the Chat & Ask setting, else [`AGENT_MODEL_DEFAULT`]. The one fallback for
/// every chat-shaped call.
pub async fn agent_model() -> String {
    or_default(get(CHAT_MODEL_KEY).await, AGENT_MODEL_DEFAULT)
}

#[deprecated(note = "use `agent_model`; there is one chat model and one fallback")]
pub async fn chat_model() -> String {
    agent_model().await
}

fn or_default(own: String, default: &str) -> String {
    if own.trim().is_empty() {
        default.to_string()
    } else {
        own
    }
}

/// The model the web search runs on.
pub async fn search_model() -> String {
    or_default(get(SEARCH_MODEL_KEY).await, SEARCH_MODEL_DEFAULT)
}

/// The depth of a web research run: `quick` or
/// `standard`, from the one setting every research path reads.
pub async fn research_depth() -> String {
    research_tier(&get(RESEARCH_DEPTH_KEY).await).to_string()
}

/// A stored depth as a tier research knows; anything else is Standard.
pub fn research_tier(v: &str) -> &'static str {
    match v.trim() {
        "quick" => "quick",
        _ => "standard",
    }
}

/// Whether the studio names new collections from their sources.
pub async fn auto_name() -> bool {
    is_on(&get(AUTO_NAME_KEY).await)
}

/// Whether a model designs collection covers. Off, every cover is the one
/// drawn from the cid and title, and no model is called for it.
pub async fn covers() -> bool {
    is_on(&get(COVERS_KEY).await)
}

/// The session language, as the English name of the language.
pub async fn language() -> String {
    get(LANGUAGE_KEY).await
}

/// The BCP 47 tag for a language name, for the browser's recogniser.
pub fn language_tag(name: &str) -> &'static str {
    LANGUAGES
        .iter()
        .find(|(n, _)| *n == name)
        .map_or("en-US", |(_, t)| t)
}

/// A sentence for a system prompt that pins the output language, or nothing
/// for English, which every prompt was already written for.
pub fn language_rule(name: &str) -> String {
    if name.is_empty() || name == "English" {
        String::new()
    } else {
        format!("Write everything you say in {name}, whatever language the material is in.")
    }
}

/// What a build may spend and how its slides are made, read once per build.
#[derive(Debug, Clone, PartialEq)]
pub struct CostPolicy {
    pub slide_model: String,
    /// `None` is no limit.
    pub max_build_usd: Option<f64>,
    pub session_minutes: u32,
}

/// The studio's cost settings, each falling back to its default.
pub async fn cost_policy() -> CostPolicy {
    let minutes = get(SESSION_MINUTES_KEY)
        .await
        .parse::<u32>()
        .unwrap_or(5)
        .clamp(2, 20);
    CostPolicy {
        slide_model: get(SLIDE_MODEL_KEY).await,
        max_build_usd: parse_limit(&get(MAX_BUILD_USD_KEY).await),
        session_minutes: minutes,
    }
}

/// `"0.50"` → `Some(0.5)`; `"off"` or anything unreadable → `None`.
pub fn parse_limit(v: &str) -> Option<f64> {
    v.trim().parse::<f64>().ok().filter(|x| *x > 0.0)
}

pub fn is_on(value: &str) -> bool {
    value != "off"
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The defaults are the values the benchmark chose. A change to either is a
    /// change to what every session costs and how it sounds, so it should show
    /// up in a diff rather than in a bill.
    #[test]
    fn the_defaults_are_the_benchmarked_models() {
        assert_eq!(SCRIPT_MODEL_DEFAULT, "anthropic/claude-haiku-4.5");
        assert_eq!(ANSWER_MODEL_DEFAULT, "openai/gpt-audio-mini");
    }

    /// A default the dialog cannot select would show a blank control.
    #[test]
    fn every_default_is_valid_for_its_own_setting() {
        for d in CATALOGUE {
            assert!(validate(d, d.default).is_ok(), "{}", d.key);
            assert!(
                TABS.contains(&d.tab),
                "{} is on unknown tab {}",
                d.key,
                d.tab
            );
        }
    }

    #[test]
    fn keys_are_unique_and_namespaced() {
        let mut seen = std::collections::HashSet::new();
        for d in CATALOGUE {
            assert!(d.key.starts_with("OPENNOTEBOOK_"), "{}", d.key);
            assert!(seen.insert(d.key), "{} twice", d.key);
        }
    }

    #[test]
    fn out_of_range_and_unknown_choices_are_refused() {
        let slides = def(SLIDE_COUNT_KEY).unwrap();
        assert!(validate(slides, "2").is_err());
        assert!(validate(slides, "13").is_err());
        assert!(validate(slides, "abc").is_err());
        assert!(validate(slides, "8").is_ok());
        let voice = def(SPEAKER1_VOICE_KEY).unwrap();
        assert!(validate(voice, "zz_nobody").is_err());
        assert!(validate(def(INTERRUPT_KEY).unwrap(), "maybe").is_err());
    }

    #[test]
    fn the_tabs_are_in_the_plans_order_and_each_has_its_info() {
        assert_eq!(
            TABS,
            [
                "Generation defaults",
                "Voices",
                "Language",
                "Live conversation",
                "Costs & limits",
                "Models"
            ]
        );
        let labels: Vec<_> = TAB_INFO.iter().map(|t| t.label).collect();
        assert_eq!(labels, TABS);
        for t in TABS {
            assert!(CATALOGUE.iter().any(|d| d.tab == *t), "{t} is empty");
        }
    }

    #[test]
    fn the_new_settings_exist_with_their_defaults() {
        let want = [
            (AUDIO_FORMAT_KEY, TAB_DEFAULTS, "deep_dive"),
            (AUDIO_LENGTH_KEY, TAB_DEFAULTS, "default"),
            (RESEARCH_DEPTH_KEY, TAB_DEFAULTS, "standard"),
            (AUTO_NAME_KEY, TAB_DEFAULTS, "on"),
            (COVERS_KEY, TAB_DEFAULTS, "on"),
            (SHOW_COST_KEY, TAB_COSTS, "on"),
            (SEARCH_MODEL_KEY, TAB_MODELS, SEARCH_MODEL_DEFAULT),
            (SLIDE_MODEL_KEY, TAB_MODELS, SLIDE_MODEL_DEFAULT),
        ];
        for (key, tab, default) in want {
            let d = def(key).unwrap_or_else(|| panic!("{key} is missing"));
            assert_eq!(d.tab, tab, "{key}");
            assert_eq!(d.default, default, "{key}");
        }
        assert!(validate(def(AUDIO_FORMAT_KEY).unwrap(), "podcast").is_err());
        assert!(validate(def(RESEARCH_DEPTH_KEY).unwrap(), "quick").is_ok());
        assert!(validate(def(RESEARCH_DEPTH_KEY).unwrap(), "deep").is_err());
        assert!(validate(def(SHOW_COST_KEY).unwrap(), "maybe").is_err());
    }

    /// Every audio format and length a setting offers is one the model parses
    /// back to itself, or the default would quietly become Deep Dive.
    #[test]
    fn the_audio_choices_are_the_models_ids() {
        for (id, _) in AUDIO_FORMATS {
            assert_eq!(crate::AudioFormat::parse(id).id(), *id);
        }
        for (id, _) in AUDIO_LENGTHS {
            assert_eq!(crate::AudioLength::parse(id).id(), *id);
        }
        for (id, _) in RESEARCH_DEPTHS {
            assert_eq!(research_tier(id), *id);
        }
        assert_eq!(research_tier("nonsense"), "standard");
    }

    #[test]
    fn models_are_advanced_grouped_and_offer_their_default() {
        for d in CATALOGUE {
            let is_model = matches!(d.kind, Kind::Model(_));
            assert_eq!(is_model, d.tab == TAB_MODELS, "{}", d.key);
            assert_eq!(d.advanced, is_model, "{}", d.key);
            if let Kind::Model(tested) = d.kind {
                assert!(!d.group.is_empty(), "{} has no group", d.key);
                assert!(
                    tested.iter().any(|(id, _)| *id == d.default),
                    "{}'s default is not among its tested models",
                    d.key
                );
            }
        }
        let m = def(SCRIPT_MODEL_KEY).unwrap();
        assert!(validate(m, "some-vendor/any-new-model").is_ok());
        assert!(validate(m, "not a model").is_err());
    }

    /// Ask, the assistant and naming all read `agent_model`; the deprecated
    /// `chat_model` used to fall back to the script model instead.
    #[test]
    fn the_chat_model_has_one_fallback() {
        let d = def(CHAT_MODEL_KEY).unwrap();
        assert_eq!(d.default, AGENT_MODEL_DEFAULT);
        assert!(d.help.contains("Gemini 2.5 Flash Lite"));
        assert_eq!(
            or_default(String::new(), AGENT_MODEL_DEFAULT),
            AGENT_MODEL_DEFAULT
        );
        assert_eq!(
            or_default("  ".into(), AGENT_MODEL_DEFAULT),
            AGENT_MODEL_DEFAULT
        );
        assert_eq!(or_default("x/y".into(), AGENT_MODEL_DEFAULT), "x/y");
    }

    #[test]
    fn the_slide_range_is_one_range() {
        let d = def(SLIDE_COUNT_KEY).unwrap();
        assert!(matches!(
            d.kind,
            Kind::Number {
                min: SLIDES_MIN,
                max: SLIDES_MAX
            }
        ));
        assert_eq!(d.default, SLIDES_DEFAULT.to_string());
        assert_eq!(clamp_slides(1), SLIDES_MIN);
        assert_eq!(clamp_slides(30), SLIDES_MAX);
        assert_eq!(clamp_slides(7), 7);
    }

    #[test]
    fn every_language_has_a_recogniser_tag() {
        for (name, _) in LANGUAGE_CHOICES {
            assert_ne!(language_tag(name), "", "{name}");
            assert!(LANGUAGES.iter().any(|(n, _)| n == name), "{name}");
        }
        assert_eq!(language_rule("English"), "");
        assert!(language_rule("French").contains("French"));
    }
}
