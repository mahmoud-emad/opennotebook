//! What a build will cost, before it runs.
//!
//! A build pays for model calls at the AI endpoint (OpenRouter by default),
//! all made through `opennotebook_ai`. Mapped from the code on 2026-10-03:
//!
//! - **opennotebook_memory**, while importing the sources: Q&A-extraction
//!   calls per source file per dimension (4), on `openai/gpt-4o-mini`: one for
//!   a file up to 32,000 characters, else one per window of it
//!   (`opennotebook_memory::windows_for`, at most six), together reading the
//!   whole file. The search index is full-text by default and
//!   free; embeddings, when an embedding model is set, are not estimated.
//! - **opennotebook** (this crate): the outline, the narration script and its
//!   edit pass on the script model, and the slides, written as HTML a batch of
//!   four per call on the slide model (`opennotebook_build::slides`). An audio overview has no slides.
//! - **web research** (`research.rs`), only when a prep is asked to research
//!   a topic.
//! - Narration is local Kokoro and free.
//!
//! What is exact and what is not. Every INPUT is measured: the real source
//! files, the real settings, the live prices from the endpoint's `/models`
//! catalog (OpenRouter's, which the Q&A extraction pays too). OUTPUTS are not
//! knowable before the models run, so they are a low / typical / high range,
//! calibrated where a measurement exists (each constant below says which) and
//! bounded by the build's own limits where none does. The dialog says so
//! rather than printing a precise-looking number.
//!
//! Tokens are counted as characters / 4, the usual English average. A prep
//! records what it really spent on its session row (`Session.spent_usd`), which
//! is the number to check these constants against.

use std::collections::HashMap;

/// Characters per token, for English prose.
const CHARS_PER_TOKEN: f64 = 4.0;

/// Q&A extraction: the four dimensions the pipeline asks opennotebook_memory
/// for, each one call per window of each file.
pub const QA_DIMENSIONS: u64 = 4;
pub const QA_MODEL: &str = opennotebook_memory::DEFAULT_QA_MODEL;
/// What every call sends besides the source: the system prompt, the JSON
/// schema and the request's header (dimension, focus, budget), measured from
/// the prompt source.
const QA_PROMPT_TOKENS: u64 = 650;
/// Up to ten pairs per file and dimension, each with a one-to-three-sentence
/// answer and a short quote. The call is capped at 3000; a full ten pairs is
/// about 1000, an empty dimension almost nothing. A file read in several
/// windows asks each for its share, at least four, so it writes somewhat
/// more in all: see `qa_out_share`.
/// Not measured yet: the calls are recorded in the spend ledger, which is
/// where a measurement would come from.
const QA_OUT: Range = Range::new(250, 1000, 1800);
/// A small source cannot fill ten answers: a call writes at most twice what it
/// read, its answers restating the source with a question each, and never
/// less than this, a pair or two. Reasoned, not measured.
const QA_OUT_FLOOR: u64 = 200;

/// The outline reads up to 24 excerpts of ~900 characters and 12 Q&A pairs.
const OUTLINE_MATERIAL_CHARS: u64 = 24 * 900;
const OUTLINE_QA_TOKENS: u64 = 12 * 80;
const OUTLINE_PROMPT_TOKENS: u64 = 300;
/// One title of at most 60 characters per slide, and the three points it owns.
const OUTLINE_OUT_PER_SLIDE: u64 = 80;

/// The whole-session script: each part gets up to 8 excerpts and 4 Q&A pairs,
/// capped at `MATERIAL_PER_PART` (12,000) characters.
const SCRIPT_PART_CHARS: u64 = 8 * 900 + 4 * 320;
const MATERIAL_PER_PART: u64 = 12_000;
const SCRIPT_PROMPT_TOKENS: u64 = 900;
/// Per part, on top of its narration: up to 4 slide elements of 72 characters.
/// The opening and closing add 320 each.
const SLIDE_COPY_CHARS: u64 = 4 * 72;
const SCRIPT_OUT_CHARS_BOOKENDS: u64 = 2 * 320;
/// The narration that comes back, as a share of its budget. Measured on the
/// four 5-minute decks of prep jobs 00ky, 00l4, 00l7 and 00l8 (892 characters
/// a slide budgeted, after lengthening): 3,114 to 5,013 characters of 4,460,
/// so 0.70 to 1.12; three 5-minute audio overviews (00m3, 00m5, 00m7) came to
/// 0.89 to 1.05 of theirs. Those are after lengthening, so priced on the
/// first pass as well this errs high.
const NARRATION_SHARE: [f64; 3] = [0.7, 0.9, 1.15];

/// The slide writer (`opennotebook_build::slides`): one call per batch of
/// `PER_CALL` (4) slides, so a 5-slide deck is a batch of 4 and a batch of 1.
/// Each call carries the style kit's instructions, recipe and example: 5,015
/// to 5,565 characters across the eight kits, measured from `slides::prompt`
/// on 2026-10-03, plus the style's brief and the batch header.
const SLIDES_PER_CALL: u64 = opennotebook_build::slides::PER_CALL as u64;
const SLIDE_PROMPT_CHARS: u64 = 5_400;
/// Per slide in a call: its title, copy and a narration excerpt of at most
/// 500 characters. Prep jobs 00ks to 00m1 (eight 5-slide decks, two calls
/// each) sent 11k to 14k characters in all, which leaves 200 to 800 a slide
/// after the two prompts.
const SLIDE_COPY_IN_CHARS: u64 = 700;
/// Characters written per slide: a whole HTML document with its figure in SVG.
/// The same eight decks, on Haiku 4.5, wrote 9k to 13k characters for five
/// slides: 1,800 to 2,600 a slide.
const SLIDE_OUT_CHARS: Range = Range::new(1_500, 2_200, 3_000);

/// A prep asked to research a topic (`research.rs`): one planning call, up to
/// five web searches on the search model (Sonar, about half a cent each), and
/// one report call on the notes model over up to twelve pages of 8,000
/// characters, about 24k tokens in and up to 6k out. Reasoned from today's
/// prices, not measured; every one of those calls is recorded in the spend
/// ledger, so a build's real figure is on its row. Low is the quick depth,
/// high a notes model priced like Sonnet. USD, low / typical / high.
const RESEARCH_USD: [f64; 3] = [0.02, 0.03, 0.20];

/// A low / typical / high count.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Range {
    pub low: u64,
    pub typical: u64,
    pub high: u64,
}

impl Range {
    pub const fn new(low: u64, typical: u64, high: u64) -> Self {
        Self { low, typical, high }
    }
    pub const fn exact(n: u64) -> Self {
        Self::new(n, n, n)
    }
}

/// A model's price, USD per token, as the catalog quotes it.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Price {
    pub prompt: f64,
    pub completion: f64,
}

pub type Prices = HashMap<String, Price>;

/// Everything the estimate is computed from.
#[derive(Debug, Clone)]
pub struct Inputs {
    /// Characters in each staged source file.
    pub source_chars: Vec<u64>,
    pub slides: u64,
    pub speakers: u64,
    pub script_model: String,
    /// The model that writes the slides.
    pub slide_model: String,
    /// Narration characters per slide, from the session length.
    pub slide_narration: u64,
    pub minutes: u64,
    /// An audio overview: no slides are designed, so that group is left out.
    pub audio: bool,
    /// The prep researches a topic on the web first.
    pub research: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    pub group: &'static str,
    pub step: String,
    pub detail: String,
    /// Empty for a step that calls no model.
    pub model: String,
    /// The service that makes the call and how it is paid.
    pub via: &'static str,
    pub calls: Range,
    pub input_tokens: u64,
    pub output_tokens: Range,
    pub cost: [f64; 3],
    pub free: bool,
    /// Set when the model has no price in the catalog: the line is counted as
    /// zero and says so, rather than silently.
    pub unpriced: bool,
    pub price: Option<Price>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Estimate {
    pub lines: Vec<Line>,
    /// Low, typical, high, USD.
    pub total: [f64; 3],
    pub assumptions: Vec<String>,
    pub source_chars: u64,
}

/// 44072 → "44,072".
fn grouped(n: u64) -> String {
    let d = n.to_string();
    let mut out = String::new();
    for (i, c) in d.chars().enumerate() {
        if i > 0 && (d.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// What one of `windows` calls on a file writes, from what a single call
/// reading the whole file writes (`full`): the pairs it is asked for, a tenth
/// of the file's ten each but never fewer than four, at the same length each.
fn qa_out_share(full: u64, windows: u64) -> u64 {
    if windows <= 1 {
        return full;
    }
    full * 10u64.div_ceil(windows).max(4) / 10
}

fn tokens(chars: u64) -> u64 {
    (chars as f64 / CHARS_PER_TOKEN).ceil() as u64
}

/// A priced line. `calls` and `out` vary together: the low total is the fewest
/// calls writing the least, the high the most writing the most.
#[allow(clippy::too_many_arguments)]
fn line(
    prices: &Prices,
    group: &'static str,
    step: &str,
    detail: String,
    model: &str,
    via: &'static str,
    calls: Range,
    input_per_call: u64,
    out_per_call: Range,
) -> Line {
    totals_line(
        prices,
        group,
        step,
        detail,
        model,
        via,
        calls,
        Range::new(
            input_per_call * calls.low,
            input_per_call * calls.typical,
            input_per_call * calls.high,
        ),
        Range::new(
            out_per_call.low * calls.low,
            out_per_call.typical * calls.typical,
            out_per_call.high * calls.high,
        ),
    )
}

/// A line priced from its token totals, for a step whose calls differ in
/// size: the slide batches, the Q&A over files of different lengths.
#[allow(clippy::too_many_arguments)]
fn totals_line(
    prices: &Prices,
    group: &'static str,
    step: &str,
    detail: String,
    model: &str,
    via: &'static str,
    calls: Range,
    input: Range,
    output: Range,
) -> Line {
    let price = prices.get(model).copied();
    let cost = match price {
        Some(p) => {
            let one = |i: u64, o: u64| i as f64 * p.prompt + o as f64 * p.completion;
            [
                one(input.low, output.low),
                one(input.typical, output.typical),
                one(input.high, output.high),
            ]
        }
        None => [0.0; 3],
    };
    Line {
        group,
        step: step.to_string(),
        detail,
        model: model.to_string(),
        via,
        calls,
        // A step that usually does not run shows what it reads when it does,
        // not "0 in".
        input_tokens: if calls.typical == 0 {
            input.high
        } else {
            input.typical
        },
        output_tokens: output,
        cost,
        free: false,
        unpriced: price.is_none(),
        price,
    }
}

fn free(group: &'static str, step: &str, detail: String, via: &'static str, calls: u64) -> Line {
    Line {
        group,
        step: step.to_string(),
        detail,
        model: String::new(),
        via,
        calls: Range::exact(calls),
        input_tokens: 0,
        output_tokens: Range::exact(0),
        cost: [0.0; 3],
        free: true,
        unpriced: false,
        price: None,
    }
}

pub const GROUP_SOURCES: &str = "Reading your sources";
pub const GROUP_SCRIPT: &str = "Writing the script";
pub const GROUP_SLIDES: &str = "Designing the slides";
pub const GROUP_VOICE: &str = "Recording the narration";
pub const GROUP_RESEARCH: &str = "Researching the web";

pub fn estimate(inp: &Inputs, prices: &Prices) -> Estimate {
    let n = inp.slides.max(1);
    let files = inp.source_chars.len() as u64;
    let total_chars: u64 = inp.source_chars.iter().sum();
    let mut lines = Vec::new();
    let script = || "opennotebook · OpenRouter";
    // An audio overview's parts are chapters, and the whole of it an overview.
    let (part, parts, whole) = if inp.audio {
        ("chapter", "chapters", "overview")
    } else {
        ("slide", "slides", "session")
    };

    // ── web research ────────────────────────────────────────────────────────
    if inp.research {
        lines.push(Line {
            group: GROUP_RESEARCH,
            step: "Web research".into(),
            detail: "Plan searches, run up to 5, read up to 12 pages, write a cited report".into(),
            model: String::new(),
            via: "OpenRouter",
            calls: Range::new(5, 7, 7),
            input_tokens: 0,
            output_tokens: Range::exact(0),
            cost: RESEARCH_USD,
            free: false,
            unpriced: false,
            price: None,
        });
    }

    // ── reading the sources (opennotebook_memory) ───────────────────────────
    // A call per window of each file per dimension, the windows together
    // reading the whole file, so input and output are summed file by file
    // rather than averaged. Characters stand in for the bytes the windows are
    // cut by; for English text they are the same. A source over about 570,000
    // characters is sampled rather than read whole, so its input is
    // overstated here, which errs on the safe side of the spending limit.
    let qa_windows = |c: u64| opennotebook_memory::windows_for(c as usize) as u64;
    let qa_calls: u64 =
        inp.source_chars.iter().map(|c| qa_windows(*c)).sum::<u64>() * QA_DIMENSIONS;
    let qa_in: u64 = inp
        .source_chars
        .iter()
        .map(|c| (tokens(*c) + QA_PROMPT_TOKENS * qa_windows(*c)) * QA_DIMENSIONS)
        .sum();
    let qa_out = inp.source_chars.iter().fold(Range::exact(0), |acc, c| {
        let n = qa_windows(*c);
        // Each window writes its share of the pairs, and no more than twice
        // what it read.
        let cap = (2 * tokens(*c / n)).max(QA_OUT_FLOOR);
        let per = |full: u64| qa_out_share(full, n).min(cap) * n * QA_DIMENSIONS;
        Range::new(
            acc.low + per(QA_OUT.low),
            acc.typical + per(QA_OUT.typical),
            acc.high + per(QA_OUT.high),
        )
    });
    lines.push(totals_line(
        prices,
        GROUP_SOURCES,
        "Question & answer extraction",
        format!(
            "{files} source{} × {QA_DIMENSIONS} kinds of question, each reading the whole source \
             (a long one in parts); these calls are included in recorded spend",
            if files == 1 { "" } else { "s" }
        ),
        QA_MODEL,
        "opennotebook_memory · OpenRouter",
        Range::exact(qa_calls),
        Range::exact(qa_in),
        qa_out,
    ));
    lines.push(free(
        GROUP_SOURCES,
        "Search index",
        "every source split into passages and indexed for full-text search".into(),
        "SQLite · free",
        files,
    ));

    // ── the script (opennotebook → OpenRouter) ──────────────────────────────
    let outline_in =
        OUTLINE_PROMPT_TOKENS + tokens(total_chars.min(OUTLINE_MATERIAL_CHARS)) + OUTLINE_QA_TOKENS;
    lines.push(line(
        prices,
        GROUP_SCRIPT,
        "Outline",
        format!("{n} {part} titles from the sources, each with the points it alone explains"),
        &inp.script_model,
        script(),
        Range::exact(1),
        outline_in,
        Range::exact(OUTLINE_OUT_PER_SLIDE * n),
    ));
    // An audio overview's script writes no on-screen copy.
    let slide_out_chars = inp.slide_narration + if inp.audio { 0 } else { SLIDE_COPY_CHARS };
    let per_slide =
        inp.slide_narration > opennotebook_script::budget::WHOLE_SESSION_MAX_SLIDE as u64;
    // A part's material is what retrieval finds, so a small collection gives
    // less than the cap.
    let part_chars = SCRIPT_PART_CHARS
        .min(total_chars + 4 * 320)
        .min(MATERIAL_PER_PART);
    // A slide written on its own is given twice the material.
    let material = if per_slide {
        (2 * part_chars).min(MATERIAL_PER_PART)
    } else {
        part_chars
    };
    let share = |chars: u64| {
        let t = tokens(chars) as f64;
        Range::new(
            (t * NARRATION_SHARE[0]) as u64,
            (t * NARRATION_SHARE[1]) as u64,
            (t * NARRATION_SHARE[2]) as u64,
        )
    };
    if per_slide {
        // A long session is written one slide per call, plus the welcome and
        // the close on their own.
        lines.push(line(
            prices,
            GROUP_SCRIPT,
            "Narration script",
            format!(
                "one {part} per call, about {} minutes in all, {} voice{}",
                inp.minutes,
                inp.speakers,
                if inp.speakers == 1 { "" } else { "s" }
            ),
            &inp.script_model,
            script(),
            Range::exact(n),
            SCRIPT_PROMPT_TOKENS + tokens(material),
            share(slide_out_chars),
        ));
        lines.push(line(
            prices,
            GROUP_SCRIPT,
            "Welcome and close",
            "two short calls, written from the outline".into(),
            &inp.script_model,
            script(),
            Range::exact(2),
            OUTLINE_PROMPT_TOKENS + OUTLINE_OUT_PER_SLIDE * n,
            Range::exact(tokens(320)),
        ));
    } else {
        lines.push(line(
            prices,
            GROUP_SCRIPT,
            "Narration script",
            format!(
                "the whole {whole} in one piece, {} voice{}{}",
                inp.speakers,
                if inp.speakers == 1 { "" } else { "s" },
                if inp.audio {
                    ""
                } else {
                    ", with each slide's text"
                }
            ),
            &inp.script_model,
            script(),
            Range::exact(1),
            SCRIPT_PROMPT_TOKENS + tokens(part_chars) * n,
            share(slide_out_chars * n + SCRIPT_OUT_CHARS_BOOKENDS),
        ));
        // Only when the one-piece script misses a part: that slide is
        // rewritten on its own. Usually none; at worst every slide, once more.
        lines.push(line(
            prices,
            GROUP_SCRIPT,
            &format!("Per-{part} rewrite, if needed"),
            format!("only for a {part} the one-piece script missed"),
            &inp.script_model,
            script(),
            Range::new(0, 0, n),
            SCRIPT_PROMPT_TOKENS + tokens(part_chars),
            Range::exact(tokens(slide_out_chars)),
        ));
        // The one-piece script no longer writes the close: it came last and
        // was the first thing the budget cut, so it is its own short call.
        lines.push(line(
            prices,
            GROUP_SCRIPT,
            "Close",
            "one short call, written from the outline".into(),
            &inp.script_model,
            script(),
            Range::exact(1),
            OUTLINE_PROMPT_TOKENS + OUTLINE_OUT_PER_SLIDE * n,
            Range::exact(tokens(320)),
        ));
    }

    // A slide that comes back short is carried on in up to three more calls,
    // each re-reading its material and what was said so far. How many run is
    // not logged; one a slide is the typical guess.
    lines.push(line(
        prices,
        GROUP_SCRIPT,
        &format!("Lengthening short {parts}"),
        format!("only for a {part} whose narration came back short of its length"),
        &inp.script_model,
        script(),
        Range::new(0, n, 3 * n),
        SCRIPT_PROMPT_TOKENS + tokens(material + slide_out_chars),
        Range::exact(tokens(slide_out_chars / 2)),
    ));

    // Every part read again with the script before it, and fixed: one call a
    // part, each reading what was said so far (capped at 12,000 characters)
    // and writing the part back.
    let earlier = (slide_out_chars * n / 2).min(MATERIAL_PER_PART);
    lines.push(line(
        prices,
        GROUP_SCRIPT,
        "Editing the script",
        "each part re-read with everything said before it, to cut repeats and unanswered questions"
            .into(),
        &inp.script_model,
        script(),
        Range::exact(n),
        SCRIPT_PROMPT_TOKENS + OUTLINE_OUT_PER_SLIDE * n + tokens(earlier + inp.slide_narration),
        share(inp.slide_narration),
    ));

    // ── the slides (opennotebook → OpenRouter) ──────────────────────────────
    // An audio overview has none: its parts are chapters, heard and not seen.
    if !inp.audio {
        let (calls, input, output) = slide_design(n);
        lines.push(free(
            GROUP_SLIDES,
            "Style kit",
            "fonts, colours and textures, added to every slide by the studio".into(),
            "opennotebook · no call",
            0,
        ));
        lines.push(totals_line(
            prices,
            GROUP_SLIDES,
            "Slide design",
            format!(
                "{n} slide{} in {} call{} of up to {SLIDES_PER_CALL}, each figure drawn in SVG; a batch that comes back short is asked again",
                if n == 1 { "" } else { "s" },
                calls.typical,
                if calls.typical == 1 { "" } else { "s" },
            ),
            &inp.slide_model,
            script(),
            calls,
            input,
            output,
        ));
    }

    // ── the voice (the local speech server) ─────────────────────────────────
    // Lines run about 250 characters; never fewer than the short-slide count.
    let lines_spoken = (n * inp.slide_narration / 250).max(n * 3) + 2;
    lines.push(free(
        GROUP_VOICE,
        "Narration audio",
        format!("about {lines_spoken} spoken lines"),
        "local voice · free",
        lines_spoken,
    ));

    let mut total = [0.0; 3];
    for l in &lines {
        for (t, c) in total.iter_mut().zip(l.cost) {
            *t += c;
        }
    }

    let mut assumptions = vec![
        format!(
            "Measured: {files} source{} ({} characters), {n} {}, about {} minutes, {} voice{}, {}today's prices.",
            if files == 1 { "" } else { "s" },
            grouped(total_chars),
            if inp.audio { "chapters" } else { "slides" },
            inp.minutes,
            inp.speakers,
            if inp.speakers == 1 { "" } else { "s" },
            if inp.audio {
                String::new()
            } else {
                format!("slides written by {}, ", inp.slide_model)
            }
        ),
        if inp.audio {
            "Estimated: how much each model writes. The range covers it; the narration lengths are calibrated on past builds.".into()
        } else {
            "Estimated: how much each model writes, and whether a batch of slides has to be asked twice. The range covers both; the slide and narration lengths are calibrated on past builds.".into()
        },
        "Tokens are counted as characters ÷ 4.".into(),
        "A call that fails is retried up to 3 times, and a retry is billed again; that is not included.".into(),
    ];
    assumptions.push(if inp.research {
        "Web research is a range reasoned from its calls and today's prices; what it really cost is recorded with the build.".into()
    } else {
        "Research you ran in the chat was billed then, and is not part of the build.".into()
    });
    if lines.iter().any(|l| l.unpriced) {
        assumptions.push(
            "A model with no price in the catalog is counted as $0 and marked \"no price\"; the real cost is higher."
                .into(),
        );
    }

    Estimate {
        lines,
        total,
        assumptions,
        source_chars: total_chars,
    }
}

/// The slide writer's calls and token totals for `n` slides: batches of
/// `SLIDES_PER_CALL`, each carrying the whole prompt once and its own slides'
/// copy. Low and typical ask each batch once; high asks every batch twice.
fn slide_design(n: u64) -> (Range, Range, Range) {
    let calls = n.div_ceil(SLIDES_PER_CALL);
    let input = tokens(calls * SLIDE_PROMPT_CHARS + n * SLIDE_COPY_IN_CHARS);
    (
        Range::new(calls, calls, 2 * calls),
        Range::new(input, input, 2 * input),
        Range::new(
            tokens(n * SLIDE_OUT_CHARS.low),
            tokens(n * SLIDE_OUT_CHARS.typical),
            2 * tokens(n * SLIDE_OUT_CHARS.high),
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// OpenRouter's prices on 2026-10-02 for the models a default build uses.
    fn prices() -> Prices {
        let p = |prompt: f64, completion: f64| Price { prompt, completion };
        HashMap::from([
            ("amazon/nova-micro-v1".into(), p(0.035e-6, 0.14e-6)),
            ("openai/gpt-4o-mini".into(), p(0.15e-6, 0.6e-6)),
            ("anthropic/claude-haiku-4.5".into(), p(1e-6, 5e-6)),
            ("anthropic/claude-sonnet-5.5".into(), p(2e-6, 10e-6)),
        ])
    }

    /// The session the spending limit was set against: four sources, 33k
    /// characters, five slides, two voices, 20 minutes, on the default model.
    fn inputs() -> Inputs {
        Inputs {
            source_chars: vec![12_000, 9_000, 7_000, 5_320],
            slides: 5,
            speakers: 2,
            script_model: "amazon/nova-micro-v1".into(),
            slide_model: "anthropic/claude-haiku-4.5".into(),
            slide_narration: opennotebook_script::budget::slide_narration(20, 5) as u64,
            minutes: 20,
            audio: false,
            research: false,
        }
    }

    /// An audio overview designs no slides, so the most expensive group of a
    /// session is not in its breakdown or its price.
    #[test]
    fn an_audio_overview_pays_for_no_slides() {
        let slides = estimate(&inputs(), &prices());
        let audio = estimate(
            &Inputs {
                audio: true,
                ..inputs()
            },
            &prices(),
        );
        assert!(slides.lines.iter().any(|l| l.group == GROUP_SLIDES));
        assert!(!audio.lines.iter().any(|l| l.group == GROUP_SLIDES));
        assert!(audio.total[1] < slides.total[1]);
    }

    /// An audio overview is told in chapters: no step of its breakdown speaks
    /// of slides.
    #[test]
    fn an_audio_overview_is_described_in_chapters() {
        let e = estimate(
            &Inputs {
                audio: true,
                ..inputs()
            },
            &prices(),
        );
        for l in &e.lines {
            assert!(
                !l.step.contains("slide") && !l.detail.contains("slide"),
                "{} — {}",
                l.step,
                l.detail
            );
        }
        assert!(step(&e, "Outline").detail.starts_with("5 chapter titles"));
        assert!(
            e.lines
                .iter()
                .any(|l| l.step == "Lengthening short chapters")
        );
    }

    /// The Q&A calls go through the same client as the rest of the prep and
    /// land in the spend ledger, so the estimate says they are recorded.
    #[test]
    fn the_qa_line_says_it_is_in_recorded_spend() {
        let e = estimate(&inputs(), &prices());
        assert!(
            step(&e, "Question & answer extraction")
                .detail
                .contains("these calls are included in recorded spend")
        );
    }

    #[test]
    fn the_range_is_ordered_and_every_model_is_priced() {
        let e = estimate(&inputs(), &prices());
        assert!(
            e.total[0] <= e.total[1] && e.total[1] <= e.total[2],
            "{:?}",
            e.total
        );
        assert!(!e.lines.iter().any(|l| l.unpriced));
    }

    #[test]
    fn a_twenty_minute_five_slide_session_stays_well_under_fifty_cents() {
        let e = estimate(&inputs(), &prices());
        assert!(e.total[2] < 0.25, "high {:?}", e.total);
        // Written one slide at a time at this length.
        let script = e
            .lines
            .iter()
            .find(|l| l.step == "Narration script")
            .unwrap();
        assert_eq!(script.calls, Range::exact(5));
    }

    #[test]
    fn slides_go_four_to_a_call_on_the_slide_model() {
        let mut i = inputs();
        i.slides = 9;
        let e = estimate(&i, &prices());
        let design = e.lines.iter().find(|l| l.step == "Slide design").unwrap();
        assert_eq!(design.model, "anthropic/claude-haiku-4.5");
        assert_eq!(design.calls, Range::new(3, 3, 6));
        i.slide_model = "anthropic/claude-sonnet-5.5".into();
        let sonnet = estimate(&i, &prices());
        let d2 = sonnet
            .lines
            .iter()
            .find(|l| l.step == "Slide design")
            .unwrap();
        assert!(d2.cost[1] > design.cost[1], "Sonnet costs more than Haiku");
    }

    #[test]
    fn qa_extraction_reads_every_file_whole_four_times() {
        let mut i = inputs();
        i.source_chars = vec![44_000, 12_000];
        let e = estimate(&i, &prices());
        let qa = &e.lines[0];
        // The 44,000-character file is read in two windows, the other in one.
        assert_eq!(qa.calls, Range::exact((2 + 1) * 4));
        assert_eq!(qa.input_tokens, (11_000 + 2 * 650) * 4 + (3_000 + 650) * 4);
        // Two windows ask for five pairs each, so write what one call would.
        assert_eq!(qa.output_tokens.typical, 1000 * 4 + 1000 * 4);
    }

    #[test]
    fn a_very_long_file_is_read_in_at_most_six_windows() {
        let mut i = inputs();
        i.source_chars = vec![1_000_000];
        let e = estimate(&i, &prices());
        let qa = step(&e, "Question & answer extraction");
        let windows = opennotebook_memory::MAX_WINDOWS_PER_DOC as u64;
        assert_eq!(qa.calls, Range::exact(windows * 4));
        // Six windows ask for four pairs each: 24 where one call asks for 10.
        assert_eq!(qa.output_tokens.typical, 1000 * 24 / 10 * 4);
    }

    #[test]
    fn a_model_with_no_price_is_marked_not_hidden() {
        let mut i = inputs();
        i.slide_model = "someone/unknown-model".into();
        let e = estimate(&i, &prices());
        assert!(e.lines.iter().any(|l| l.unpriced));
        assert!(e.assumptions.iter().any(|a| a.contains("no price")));
        assert_eq!(grouped(44_072), "44,072");
        assert_eq!(grouped(999), "999");
    }

    #[test]
    fn free_steps_are_listed_not_dropped() {
        let e = estimate(&inputs(), &prices());
        // The search index, the style kit and the narration audio.
        assert_eq!(e.lines.iter().filter(|l| l.free).count(), 3);
    }

    fn step<'a>(e: &'a Estimate, step: &str) -> &'a Line {
        e.lines.iter().find(|l| l.step == step).unwrap()
    }

    /// Five slides go out as a batch of four and a batch of one: two prompts,
    /// five slides' copy, five slides written. Not two full batches of four.
    #[test]
    fn slides_are_priced_by_the_batches_they_go_out_in() {
        let (calls, input, output) = slide_design(5);
        assert_eq!(calls, Range::new(2, 2, 4));
        assert_eq!(input.typical, tokens(2 * 5_400 + 5 * 700));
        assert_eq!(output.typical, tokens(5 * 2_200));
        assert_eq!(input.high, 2 * input.typical, "every batch asked twice");
        let (one, _, out1) = slide_design(1);
        assert_eq!(one.typical, 1);
        assert_eq!(out1.typical, tokens(2_200));
    }

    /// The calibration holds against what eight real 5-slide decks did on
    /// Haiku 4.5 (prep jobs 00ks to 00m1): 11k to 14k characters sent, 9k to
    /// 13k written.
    #[test]
    fn a_five_slide_deck_matches_the_measured_builds() {
        let (_, input, output) = slide_design(5);
        let chars = |t: u64| t * CHARS_PER_TOKEN as u64;
        assert!((11_000..=15_000).contains(&chars(input.typical)));
        assert!((9_000..=14_000).contains(&chars(output.typical)));
        assert!(chars(output.low) <= 9_000 && chars(output.high) >= 13_000);
    }

    #[test]
    fn a_tiny_source_is_not_priced_as_ten_full_answers() {
        let mut i = inputs();
        i.source_chars = vec![140, 249, 223];
        let e = estimate(&i, &prices());
        let qa = step(&e, "Question & answer extraction");
        assert_eq!(qa.calls, Range::exact(12));
        assert_eq!(qa.output_tokens, Range::exact(12 * QA_OUT_FLOOR));
        // A large source still gets the full range.
        i.source_chars = vec![40_000];
        let big = estimate(&i, &prices());
        assert_eq!(
            step(&big, "Question & answer extraction").output_tokens,
            Range::new(4 * 250, 4 * 1000, 4 * 1800)
        );
    }

    #[test]
    fn lengthening_reads_the_material_a_part_was_given() {
        let mut small = inputs();
        small.source_chars = vec![600];
        small.slide_narration = 892;
        let mut big = small.clone();
        big.source_chars = vec![60_000];
        let s = step(&estimate(&small, &prices()), "Lengthening short slides").input_tokens;
        let b = step(&estimate(&big, &prices()), "Lengthening short slides").input_tokens;
        assert!(s < b, "{s} vs {b}");
    }

    #[test]
    fn research_is_a_line_only_when_the_prep_researches() {
        let off = estimate(&inputs(), &prices());
        assert!(!off.lines.iter().any(|l| l.group == GROUP_RESEARCH));
        let on = estimate(
            &Inputs {
                research: true,
                ..inputs()
            },
            &prices(),
        );
        let r = step(&on, "Web research");
        assert_eq!(r.cost, RESEARCH_USD);
        assert!((on.total[1] - off.total[1] - RESEARCH_USD[1]).abs() < 1e-9);
        assert!(on.assumptions.iter().any(|a| a.contains("Web research")));
    }

    /// Collection s1791059435652 as the studio had it on 2026-10-03: three
    /// notes of 140, 249 and 223 characters, five slides, two voices, five
    /// minutes, Haiku 4.5 for script and slides. The old estimate said
    /// $0.080 / $0.129 / $0.335; this one says $0.044 / $0.063 / $0.132.
    #[test]
    fn the_three_note_collection_is_priced_near_what_such_builds_cost() {
        let i = Inputs {
            source_chars: vec![140, 249, 223],
            slides: 5,
            speakers: 2,
            script_model: "anthropic/claude-haiku-4.5".into(),
            slide_model: "anthropic/claude-haiku-4.5".into(),
            slide_narration: opennotebook_script::budget::slide_narration(5, 5) as u64,
            minutes: 5,
            audio: false,
            research: false,
        };
        let e = estimate(&i, &prices());
        assert!(e.total[1] < 0.129 && e.total[2] < 0.335, "{:?}", e.total);
        let d = step(&e, "Slide design");
        assert!(d.cost[1] < 0.03, "slide design typical {}", d.cost[1]);
    }
}
