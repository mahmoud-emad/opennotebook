//! Outline, then one script per slide, both grounded in the ingested material.
//!
//! Three things section 3 does not specify are decided here and flagged in the
//! report rather than buried: how many slides a session gets, how long a
//! slide's narration runs, and what two speakers do differently.

use opennotebook_ai::{Message, Provider};
use opennotebook_memory::Memory;
use opennotebook_session::{
    Aspect, LineId, NarrationLine, SessionSlide, SlideRef, Speaker, SpeakerId,
};

use crate::budget;
use crate::error::ScriptError;
use crate::grounding;
use crate::parse::parse_lines;

/// Slides per session when the caller does not say.
///
/// UNSPECIFIED BY SECTION 3. Five, the same default the slide count setting
/// has (`settings::SLIDES_DEFAULT`): two defaults for one number would be a bug
/// waiting to happen.
pub const DEFAULT_SLIDE_COUNT: usize = 5;

/// How many passages and pairs to retrieve per query.
const TOP_K: i64 = 4;

#[derive(Debug, Clone)]
pub struct ScriptSpec {
    pub title: String,
    /// One speaker or two. The set is data; nothing below branches on its size.
    pub speakers: Vec<Speaker>,
    pub slide_count: usize,
    /// The deck directory and presentation the slides will be written in.
    /// Until the slides are written there is no deck, so these name where it
    /// will go rather than where it is.
    pub deck_collection: String,
    pub deck_presentation: String,
    /// Narration characters per slide; see [`budget::slide_narration`].
    pub slide_narration: usize,
    /// The most `[image: …]` lines one slide may carry. Each one is a generated
    /// image, which is most of what a build costs; `None` is no limit.
    pub images_per_slide: Option<usize>,
    /// Present for an audio overview: no slides, the parts are chapters, and
    /// the format decides how the hosts talk. See `docs/audio-overview-spec.md`.
    pub audio: Option<opennotebook_session::AudioSpec>,
    /// The output language's English name, read once by the caller for the
    /// whole build. Empty is English, which every prompt is written in.
    pub language: String,
}

impl ScriptSpec {
    pub fn new(title: impl Into<String>, speakers: Vec<Speaker>, deck: (&str, &str)) -> Self {
        Self {
            title: title.into(),
            speakers,
            slide_count: DEFAULT_SLIDE_COUNT,
            deck_collection: deck.0.to_string(),
            deck_presentation: deck.1.to_string(),
            slide_narration: budget::SLIDE_NARRATION,
            images_per_slide: None,
            audio: None,
            language: String::new(),
        }
    }

    /// A system prompt written for a slide session, made right for an audio
    /// overview.
    ///
    /// One set of prompts serves both, because everything that makes the
    /// script good — the plan's owned points, the host dynamic, the edit pass —
    /// is the same. What differs is said here: there is no slide, so the
    /// instructions for its copy go and "slide" becomes "chapter"; the format
    /// and the focus are added at the end. A slide session's prompt passes
    /// through unchanged.
    fn adapt(&self, prompt: String) -> String {
        let Some(a) = &self.audio else {
            return prompt;
        };
        let mut out = format!("{}\n\n{}", self.adapt_user(prompt), format_rules(a));
        if !a.focus.trim().is_empty() {
            out.push_str(&format!(
                "\n\nThe listener asked for this focus, in their words: \"{}\". Build \
                 everything around it, and leave out what does not bear on it.",
                a.focus.trim()
            ));
        }
        out
    }

    /// The wording half of [`adapt`](Self::adapt), for a user prompt: the slide
    /// copy instructions removed and "slide" said as "chapter".
    fn adapt_user(&self, prompt: String) -> String {
        let Some(a) = &self.audio else {
            return prompt;
        };
        let mut p = prompt;
        // The copy block of a one-slide prompt runs to the end of it.
        if let Some(i) = p.find("\n\nThen write the line `SLIDE:`") {
            p.truncate(i);
        }
        // The whole-session prompt's copy block sits in the middle.
        const END: &str = "no colours or fonts.\n\n";
        if let (Some(i), Some(j)) = (p.find("Then the line `SLIDE:`"), p.find(END))
            && i < j
        {
            p.replace_range(i..j + END.len(), "");
        }
        let what = format!(
            "an audio overview, a {} episode about the listener's sources, heard and never seen",
            a.format.label()
        );
        p.replace("a short explanatory session", &what)
            .replace("an explanatory session", &what)
            .replace("a spoken explanatory session", &what)
            .replace("explanatory session", "audio overview")
            .replace("is shown as", "is heard as")
            .replace("Slides", "Chapters")
            .replace("slides", "chapters")
            .replace("Slide", "Chapter")
            .replace("slide", "chapter")
    }

    /// What the prompt asks for: a little under the budget, because the
    /// budget is a hard cut and a model told only a ceiling writes far less
    /// than a long session needs.
    fn narration_target(&self) -> usize {
        self.slide_narration * 9 / 10
    }

    /// The image element line for the slide-copy instructions, or none at all
    /// when a slide may not have an image.
    fn image_element(&self) -> String {
        match self.images_per_slide {
            Some(0) => String::new(),
            Some(n) => format!(
                "- [image: <what it depicts>] — <placement> (at most {n} on a slide; leave it \
                 out when a Point or Stat already says it)\n"
            ),
            None => "- [image: <what it depicts>] — <placement>\n".to_string(),
        }
    }

    fn speaker_ids(&self) -> Vec<SpeakerId> {
        self.speakers.iter().map(|s| s.speaker_id.clone()).collect()
    }
}

/// The broker, which every studio model call goes through; see
/// `opennotebook_session::ai`.
async fn provider() -> Result<Provider, ScriptError> {
    opennotebook_session::ai::provider()
        .await
        .map_err(|reason| ScriptError::Model {
            stage: "provider",
            reason,
        })
}

/// One completion, with the truncation discriminator checked.
///
/// `finish_reason` is the fourth silent-empty case in this stack: a model that
/// runs out of room returns prose that reads whole. Nothing in the text shows
/// it, so the text is never trusted without the reason beside it.
/// `complete`, but a truncated reply is kept rather than refused.
///
/// Truncation is not emptiness. When the model runs into its token ceiling the
/// lines BEFORE the cut are complete and usable, and everything downstream
/// already trims: `budget::room` drops a line there is no space for, and
/// `budget::fit_spoken` ends the last one on a sentence. Refusing the whole
/// reply threw all of that away and killed a prep that had already paid for its
/// outline — observed as "the model truncated its answer for slide script
/// (narration only)".
///
/// Only the slide script uses this. The outline still refuses a truncated reply,
/// because there the cut costs whole SLIDES rather than the tail of one line,
/// and a deck quietly missing its last two topics is the kind of success-shaped
/// failure this project keeps meeting.
async fn complete_partial(
    language: &str,
    system: &str,
    user: &str,
    stage: &'static str,
    breaker: bool,
) -> Result<String, ScriptError> {
    match complete_with(language, system, user, stage, breaker).await {
        Err(ScriptError::Truncated { text, .. }) if !text.trim().is_empty() => Ok(text),
        other => other,
    }
}

async fn complete(
    language: &str,
    system: &str,
    user: &str,
    stage: &'static str,
) -> Result<String, ScriptError> {
    complete_with(language, system, user, stage, true).await
}

/// `breaker: false` marks a call whose failure must not count against the
/// model: the whole-session call fails over to a per-slide fallback, which
/// must still be tried. The client has no circuit breaker today, so the flag
/// is recorded for intent and changes nothing.
async fn complete_with(
    language: &str,
    system: &str,
    user: &str,
    stage: &'static str,
    breaker: bool,
) -> Result<String, ScriptError> {
    // The model is an operator setting, not a constant. Changing which model
    // writes the scripts should be a `secret set`, not an edit and a rebuild.
    let model = opennotebook_session::settings::script_model().await;
    // The language rule is added here because every stage passes through
    // this call. The language itself is the spec's, read once per build. The
    // format markers stay as written or the parser cannot read the reply.
    let rule = opennotebook_session::settings::language_rule(language);
    let system = if rule.is_empty() {
        system.to_string()
    } else {
        format!(
            "{system}\n\n{rule} Keep speaker ids, markers such as `SLIDE:` and field labels \
             exactly as written above; only the words themselves change language."
        )
    };
    send(&model, &system, user, stage, breaker).await
}

/// One completion on a named model, with the truncation discriminator checked.
///
/// `complete_with` is this plus the script model and the language rule. A
/// caller with its own model setting and its own prompt rules, the mind map,
/// calls this directly.
pub(crate) async fn send(
    model: &str,
    system: &str,
    user: &str,
    stage: &'static str,
    breaker: bool,
) -> Result<String, ScriptError> {
    let response = provider()
        .await?
        .completions()
        .model(model)
        .message(Message::system(system.to_string()))
        .user(user.to_string())
        .options(opennotebook_ai::CallOptions {
            use_breaker: breaker,
            ..Default::default()
        })
        .send()
        .await
        .map_err(|e| ScriptError::Model {
            stage,
            reason: e.to_string(),
        })?;
    // Before the checks below: a truncated or empty reply was billed too.
    opennotebook_session::spend::record(stage, model, response.usage.as_ref());

    let finish = format!("{:?}", response.finish_reason).to_ascii_lowercase();
    if finish.contains("length") || finish.contains("max_token") {
        // The text rides along so `complete_partial` can keep it. A caller that
        // wants the strict behaviour simply ignores the field.
        return Err(ScriptError::Truncated {
            stage,
            finish_reason: finish,
            text: response.text,
        });
    }
    if response.text.trim().is_empty() {
        return Err(ScriptError::Empty { stage });
    }
    Ok(response.text)
}

/// One real id to show the model, so the format line has nothing generic in it
/// to copy. A prompt that said "lines of the form `speaker_id: …`" got exactly
/// that back, tagged `host_id`, and failed the prep.
fn example_id(spec: &ScriptSpec) -> String {
    spec.speakers
        .first()
        .map(|s| s.speaker_id.0.clone())
        .unwrap_or_else(|| "host".to_string())
}

/// One part of the plan: the slide's title and the points that part alone
/// explains.
#[derive(Debug, Clone, Default, PartialEq)]
struct Planned {
    title: String,
    points: Vec<String>,
}

/// How many points the plan gives a part.
const POINTS_PER_PART: usize = 3;

/// Section 3 step 2: one call, the whole collection as context, out comes the
/// plan: a title per slide and the points each one owns.
///
/// Titles alone were not enough to keep the parts apart. Five titles on a paper
/// about Moshi all led back to its headline idea, and each part, knowing only
/// its own title, explained that idea again: a real session explained "Inner
/// Monologue" on all five slides and the latency figure on three. NotebookLM's
/// open reimplementations plan first for the same reason — the sources become
/// a list of key points, and the script walks through them once. A point is
/// owned by exactly one part, so every other part knows not to explain it.
async fn outline(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    spec: &ScriptSpec,
) -> Result<Vec<Planned>, ScriptError> {
    let context =
        grounding::retrieve(memory, workspace, collection, &spec.title, TOP_K * 3).await?;
    if context.is_empty() {
        return Err(ScriptError::NoGrounding {
            workspace: workspace.to_string(),
            collection: collection.to_string(),
        });
    }

    let system = format!(
        "You plan a short explanatory session from source material, the way a good teacher \
         plans a lesson: what the listener needs first, what builds on it, and what they \
         should walk away with.\n\
         Return exactly {count} parts, in the order they will be told. For each part write one \
         line with its title (at most {title} characters, no numbering, naming its subject and \
         never its role in the plan: no \"Foundation:\", \"Payoff:\" or \"Introduction\"), \
         then {points} lines \
         starting with \"- \", each one specific idea, fact, number or example from the material \
         that THIS part explains. Nothing else: no headings, no blank commentary.\n\
         Rules:\n\
         - Every point belongs to exactly one part. If two parts would explain the same idea, \
           give it to the first and make the later one build on it instead.\n\
         - The first part is the foundation: what the subject is and the problem it solves. \
           The last part is the payoff: results, limits or what it means.\n\
         - Points are concrete (\"200 ms end-to-end latency on an L4 GPU\"), never vague \
           (\"discusses latency\").\n\
         - Cover only what the SOURCE and FACT material below actually says. Do not add \
           topics from your own knowledge.",
        count = spec.slide_count,
        title = budget::TITLE,
        points = POINTS_PER_PART,
    );
    let user = format!(
        "Session title: {}\n\nMaterial:\n\n{}",
        spec.title,
        context.as_context()
    );

    let system = spec.adapt(system) + &outline_shape(spec);
    let plan = parse_plan(
        &complete(&spec.language, &system, &user, "outline").await?,
        spec.slide_count,
    );
    if plan.is_empty() {
        return Err(ScriptError::Empty { stage: "outline" });
    }
    Ok(plan)
}

/// How an audio overview's format shapes its plan; nothing for a slide session.
fn outline_shape(spec: &ScriptSpec) -> String {
    use opennotebook_session::AudioFormat as F;
    match spec.audio.as_ref().map(|a| a.format) {
        None | Some(F::DeepDive) => String::new(),
        Some(F::Brief) => "\nPlan it as a brief: the first part is the single most important \
             finding, the last part the other key points and the takeaway."
            .to_string(),
        Some(F::Critique) => "\nPlan it as a critique: the first part is what the material sets \
             out to do and its real strengths; each middle part is one weakness or risk, most \
             important first, with what would fix it; the last part is the revisions in order \
             of priority."
            .to_string(),
        Some(F::Debate) => "\nPlan it as a debate: the first part states the question and the \
             two positions the material supports; each middle part is one contested point with \
             the evidence on both sides; the last part is what each side must concede and what \
             stays open."
            .to_string(),
    }
}

/// A title that is only a numbering: "PART ONE", "Part 2", "Section III".
fn is_label(title: &str) -> bool {
    const WORDS: &[&str] = &["part", "chapter", "section", "slide", "segment"];
    const NUMS: &[&str] = &[
        "one", "two", "three", "four", "five", "six", "seven", "eight", "i", "ii", "iii", "iv",
        "v", "vi", "vii", "viii",
    ];
    let words: Vec<String> = title
        .split_whitespace()
        .map(|w| {
            w.trim_matches(|c: char| !c.is_alphanumeric())
                .to_lowercase()
        })
        .filter(|w| !w.is_empty())
        .collect();
    words.len() == 2
        && WORDS.contains(&words[0].as_str())
        && (NUMS.contains(&words[1].as_str()) || words[1].chars().all(|c| c.is_ascii_digit()))
}

/// A title cut to its budget, without a small word left hanging at the end
/// ("…why text might not be the").
fn fit_title(title: &str) -> String {
    const SMALL: &[&str] = &[
        "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "be", "is", "at",
        "by", "from", "as",
    ];
    let mut t = budget::fit(title, budget::TITLE);
    if t.chars().count() < title.trim().chars().count() {
        while let Some((head, last)) = t.rsplit_once(' ') {
            if !SMALL.contains(&last.to_lowercase().as_str()) {
                break;
            }
            t = head
                .trim_end_matches([',', ':', ';', '-', '—'])
                .trim_end()
                .to_string();
        }
    }
    t
}

/// Read a plan: a title line, then its `- ` points.
///
/// A small model decorates — bold titles, "Slide 2:" labels, `*` bullets,
/// numbered points — and a reply with no points at all is still a usable list
/// of titles, which is exactly what the outline used to be. A point is told
/// from a title by its bullet; a title is anything else that is not blank.
fn parse_plan(raw: &str, count: usize) -> Vec<Planned> {
    let mut plan: Vec<Planned> = Vec::new();
    for line in raw.lines() {
        let t = line.trim();
        if t.is_empty() {
            continue;
        }
        let bullet = t
            .strip_prefix("- ")
            .or_else(|| t.strip_prefix("* "))
            .or_else(|| t.strip_prefix("• "))
            .or_else(|| t.strip_prefix("– "));
        match (bullet, plan.last_mut()) {
            (Some(p), Some(part)) => {
                let p = p.trim();
                if !p.is_empty() && part.points.len() < POINTS_PER_PART + 1 {
                    part.points.push(p.to_string());
                }
            }
            (Some(_), None) => {}
            (None, _) => {
                let title =
                    strip_slide_label(t.trim_matches(|c: char| matches!(c, '*' | '#' | '_' | ' ')));
                let title = title.trim_start_matches(['-', '*', '#', ' ']).trim();
                if !title.is_empty() {
                    plan.push(Planned {
                        title: fit_title(title),
                        points: Vec::new(),
                    });
                }
            }
        }
    }
    // A heading the model put over the plan ("Debate: Moshi") or a bare label
    // ("PART ONE") arrives as a part with no points. When other parts have
    // points those are the plan, and a part without is decoration: measured,
    // two such lines shifted every chapter title of a debate by two.
    // A part with no points is a heading when it comes before the first part
    // that has points, and padding when the plan is longer than asked; in the
    // middle of a plan of the right size it is a part whose points the model
    // forgot, and it stays.
    plan.retain(|p| !is_label(&p.title));
    if let Some(first) = plan.iter().position(|p| !p.points.is_empty()) {
        plan.drain(..first);
    }
    if plan.len() > count {
        let mut extra = plan.len() - count;
        plan.retain(|p| {
            if extra > 0 && p.points.is_empty() {
                extra -= 1;
                false
            } else {
                true
            }
        });
    }
    plan.truncate(count);
    plan
}

/// An outline line without the "Slide 2:" or "2." a model puts in front of it.
///
/// The prompt asks for no numbering and got "Slide 1: Process Isolation in
/// Linux" anyway; that line is the slide's heading, so the label would be
/// printed on the slide.
fn strip_slide_label(line: &str) -> &str {
    // "Part 1:" and "Chapter 1:" too: an audio overview's plan is asked for
    // parts, and a model numbers them the way it was asked.
    let labelled = ["Slide", "slide", "Part", "part", "Chapter", "chapter"]
        .iter()
        .find_map(|w| line.strip_prefix(w));
    let rest = labelled.unwrap_or(line).trim_start();
    let digits = rest.chars().take_while(|c| c.is_ascii_digit()).count();
    if digits == 0 {
        return line;
    }
    let tail = &rest[digits..];
    // A bare number is a list marker only when punctuation follows it: "2. The
    // task_struct" is numbered, "64-bit address spaces" is a title.
    if labelled.is_none() && !tail.starts_with(['.', ':', ')']) {
        return line;
    }
    let after = tail.trim_start_matches([':', '.', ')', '-', '—', ' ']);
    if after.is_empty() { line } else { after }
}

/// Section 3 step 3: one call per slide, grounded on that slide's own topic.
/// The spoken half and the printed half of one slide.
pub(crate) struct SlideDraft {
    pub lines: Vec<crate::parse::ScriptedLine>,
    /// `Layout:` plus the element lines, the copy the slide model is given.
    /// Empty when the model returned nothing usable, which leaves a title-only
    /// slide.
    pub on_slide: Vec<String>,
}

async fn script_one(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    spec: &ScriptSpec,
    place: &Place<'_>,
) -> Result<SlideDraft, ScriptError> {
    // A long slide needs more to say: twice the passages once a slide is past
    // what one part of the whole-session call carries.
    let k = if spec.slide_narration > budget::WHOLE_SESSION_MAX_SLIDE {
        TOP_K * 2
    } else {
        TOP_K
    };
    let context =
        grounding::retrieve(memory, workspace, collection, &place.here().query(), k).await?;
    let roster = roster(spec);

    let system = format!(
        "You write the spoken narration for one slide of an explanatory session.\n\
         Speakers, by id:\n{roster}\n\n\
         {continuity}\n\n\
         {dialogue}\n\n\
         Return only spoken lines. Start every line with one of those ids \
         exactly as written, then a colon, then what they say — like this:\n\
         {example}: what they say\n\
         Use no other name and no other prefix. \
         Every line must be at most {line} characters. Write between {target} \
         and {total} characters of narration in all, explaining THIS slide's points \
         with the examples and numbers in the material.\n\
         Say only what the material below supports. Do not introduce facts from \
         your own knowledge, and do not leave a placeholder of any kind.\n\n\
         Then write the line `SLIDE:` on its own, and after it the copy that \
         appears ON the slide. The slide is looked at while the narration is \
         heard, so it carries the few words that anchor what is being said — \
         never the spoken sentences again.\n\
         Give one `Layout:` line naming one of: {layouts}.\n\
         Then at most {elements} lines, each one element, exact final wording:\n\
         - Point: <a few words>\n\
         - Subhead: <a short phrase>\n\
         - Stat: <number> — <what it measures>\n\
         {image}\
         Each element at most {elen} characters. No sentences, no full stops at \
         the end of a Point, no colours or fonts, and no placeholder — if the \
         material does not support an element, leave it out.",
        continuity = CONTINUITY,
        dialogue = dialogue(spec),
        example = example_id(spec),
        line = budget::LINE,
        target = spec.narration_target(),
        total = spec.slide_narration,
        image = spec.image_element(),
        layouts = crate::parse::LAYOUTS.join(", "),
        elements = budget::SLIDE_ELEMENTS,
        elen = budget::ELEMENT,
    );
    let user = format!(
        "{}\n\nMaterial:\n\n{}",
        place.describe(),
        context.as_context()
    );

    let system = spec.adapt(system);
    let user = spec.adapt_user(user);
    let raw = complete_partial(&spec.language, &system, &user, "slide script", true).await?;
    let (mut spoken, mut elements) = crate::parse::split_slide_reply(&raw);

    // Narration is the half that must survive.
    //
    // Asking one call for two things — spoken lines AND the slide's own copy —
    // is cheap when it works and is what keeps this to one model call per slide.
    // But the script model is deliberately small (`amazon/nova-micro-v1`, chosen
    // for costing $0.00002 a slide), and a small model given two jobs sometimes
    // does only the second: observed returning `SLIDE:` and a clean element
    // block with no speaker-prefixed line in front of it, which failed the whole
    // prep with "the model returned no usable slide script".
    //
    // So the two jobs come apart on failure. One retry, narration only, with the
    // slide-copy half of the instruction removed: the session gets its voice and
    // the slide falls back to its title, which is a far better outcome than a
    // prep that dies at 20% having already paid for the outline.
    // Retry when the first reply cannot be used AT ALL — whether it came back
    // with no narration, or with speaker tags that name nobody.
    //
    // `unwrap_or(true)` is doing the second half: an `Err` here is
    // `UnknownSpeaker`, and until this existed that error travelled all the way
    // out and killed the prep. A label the resolver cannot place is a formatting
    // accident, not a reason to throw away an outline that has already been paid
    // for. Layer one is `parse::resolve`, which now understands a speaker named
    // by position; this is the net under it for whatever it has not seen.
    if lines_of(spec, &spoken)
        .map(|l| l.is_empty())
        .unwrap_or(true)
    {
        let narration_only = system
            .split_once("\n\nThen write the line")
            .map(|(head, _)| head.to_string())
            .unwrap_or_else(|| system.clone());
        let retry = complete_partial(
            &spec.language,
            &narration_only,
            &user,
            "slide script (narration only)",
            true,
        )
        .await?;
        let (again, _) = crate::parse::split_slide_reply(&retry);
        // Keep whichever reply can actually be read. A retry that is itself
        // unparseable must not throw away a first reply that was merely thin.
        spoken = match (lines_of(spec, &again), lines_of(spec, &spoken)) {
            (Ok(a), _) if !a.is_empty() => again,
            (_, Ok(b)) if !b.is_empty() => spoken,
            _ => again,
        };
        // The elements from the first reply are still good — it was the
        // narration that was missing — so they are kept rather than discarded.
        if elements.is_empty() {
            elements = crate::parse::split_slide_reply(&raw).1;
        }
    }
    // The narration is parsed from the spoken half only. Parsing the whole reply
    // would read `Point: …` as a line by a speaker called `Point`, which is the
    // dangling-speaker error and would fail the slide over its own slide copy.
    let mut lines = lines_of(spec, &spoken)?;
    // The prompt asks for no greeting; this is what holds when a small model
    // greets anyway.
    drop_preamble(&mut lines);
    if lines.is_empty() {
        return Err(ScriptError::Empty {
            stage: "slide script",
        });
    }
    extend(spec, &roster, &user, &mut lines).await;

    Ok(shape(
        lines,
        &raw,
        elements,
        spec.slide_narration,
        spec.images_per_slide,
    ))
}

/// How many follow-up calls a slide may take to reach its length.
const EXTEND_CALLS: usize = 3;

/// Carry a slide's conversation on until it is close to its budget.
///
/// A small model asked for 3,700 characters of one slide wrote about 2,000 on
/// a real 20-minute build, so the session came out at nine minutes; a 5-minute
/// session written in one call came out at two. Asking
/// harder in the one prompt does not fix that reliably; asking it to go on from
/// where it stopped does, and each call costs a fraction of a cent. A slide
/// already near its budget takes no call. A reply that adds nothing ends it, as
/// does any error: the slide already has a script, and a shorter session is
/// better than a failed one.
async fn extend(
    spec: &ScriptSpec,
    roster: &str,
    material: &str,
    lines: &mut Vec<crate::parse::ScriptedLine>,
) {
    // Measured: a 3/4 goal left a 20-minute session at 16 minutes, two slides
    // stopping just under it. The budget's hard cut keeps an overshoot in check.
    let goal = spec.slide_narration * 9 / 10;
    for _ in 0..EXTEND_CALLS {
        let have: usize = lines.iter().map(|l| l.text.chars().count()).sum();
        if have >= goal {
            return;
        }
        let more = spec.narration_target().saturating_sub(have).max(600);
        let so_far = lines
            .iter()
            .map(|l| format!("{}: {}", l.speaker_id.0, l.text))
            .collect::<Vec<_>>()
            .join("\n");
        // "Go deeper" with nothing to go deeper INTO is how a slide filled its
        // length by explaining the session's headline idea once more. The
        // continuation is pointed at this slide's own points, and at concrete
        // detail from the material, and told what belongs to other slides.
        let system = format!(
            "You continue the spoken narration for one slide of an explanatory session.\n\
             Speakers, by id:\n{roster}\n\n\
             {dialogue}\n\n\
             The conversation so far is below. Carry it on from its last line. Take up THIS \
             slide's points that the conversation has not reached yet; once they are all \
             covered, add what the material offers about them that has not been said: a \
             concrete example, a number, a consequence, a limitation, or how one point leads \
             to the next. Never re-explain what was already said, nothing that belongs to \
             another slide, no greeting and no summing up. End on a statement, not a question.\n\
             Return only the NEW spoken lines, each starting with a speaker id exactly as \
             written, a colon, then what they say — like this:\n{example}: what they say\n\
             Each line at most {line} characters, about {more} characters in all. Say only \
             what the material supports.",
            dialogue = dialogue(spec),
            example = example_id(spec),
            line = budget::LINE,
        );
        let user = format!("{material}\n\nThe conversation so far:\n{so_far}");
        let system = spec.adapt(system);
        let user = spec.adapt_user(user);
        let Ok(raw) = complete_partial(
            &spec.language,
            &system,
            &user,
            "slide script (continued)",
            true,
        )
        .await
        else {
            return;
        };
        let (spoken, _) = crate::parse::split_slide_reply(&raw);
        let Ok(mut added) = lines_of(spec, &spoken) else {
            return;
        };
        drop_preamble(&mut added);
        if added.is_empty() {
            return;
        }
        lines.extend(added);
    }
}

/// A slide's narration budget: its own share, plus the welcome on the first
/// slide, which rides on it. The close is not counted here: it is written
/// apart and added after every cut, with its own budget.
fn slide_total(spec: &ScriptSpec, ordinal: usize, _n: usize) -> usize {
    spec.slide_narration
        + if ordinal == 0 {
            budget::BOOKEND_NARRATION
        } else {
            0
        }
}

/// Lengthen a slide from the whole-session script that came back short.
///
/// That call writes every slide in one reply and a small model keeps each part
/// brief: a 5-minute session came out at 2.1 minutes. The slide is carried on
/// from its own material, as a slide written on its own is, then cut to its
/// budget again.
// Every argument is one fact about the part being topped up; a struct for
// them would be a second name for the same eight things.
#[allow(clippy::too_many_arguments)]
async fn top_up(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    spec: &ScriptSpec,
    plan: &[Planned],
    ordinal: usize,
    total: usize,
    draft: &mut SlideDraft,
) {
    let have: usize = draft.lines.iter().map(|l| l.text.chars().count()).sum();
    if have >= spec.slide_narration * 9 / 10 {
        return;
    }
    let Ok(context) =
        grounding::retrieve(memory, workspace, collection, &plan[ordinal].query(), TOP_K).await
    else {
        return;
    };
    let material = format!(
        "{}\n\nMaterial:\n\n{}",
        plan_listing(plan, Some(ordinal)),
        budget::fit(&context.as_context(), MATERIAL_PER_PART)
    );
    extend(spec, &roster(spec), &material, &mut draft.lines).await;
    fit_to(&mut draft.lines, total);
}

/// A reply's spoken lines, with a speaker named by their display name read as
/// that speaker.
///
/// The prompt lists each speaker as `host (Bella): narrator` and asks for the
/// id, and a model writes "Bella:" anyway. The parser knows ids and positions
/// ("A:", "Speaker 1:") but not names, so the whole reply failed to parse:
/// measured on a Brief, the whole-session reply AND both bookends came back
/// unusable, and the episode ran 0.64 minutes of a 2-minute target. A name is
/// as unambiguous as a position, so it is turned into the id before parsing.
fn lines_of(spec: &ScriptSpec, text: &str) -> Result<Vec<crate::parse::ScriptedLine>, ScriptError> {
    parse_lines(&named_to_ids(spec, text), &spec.speaker_ids())
}

fn named_to_ids(spec: &ScriptSpec, text: &str) -> String {
    let names: Vec<(String, String)> = spec
        .speakers
        .iter()
        .filter(|s| !s.display_name.trim().is_empty())
        .map(|s| (s.display_name.trim().to_lowercase(), s.speaker_id.0.clone()))
        .collect();
    text.lines()
        .map(|line| {
            let t = line.trim_start();
            let Some((label, rest)) = t.split_once(':') else {
                return line.to_string();
            };
            let bare = label
                .trim()
                .trim_matches(|c: char| c == '*' || c == '_')
                .trim()
                .to_lowercase();
            let rest = rest.trim_start_matches(['*', '_']);
            match names.iter().find(|(n, _)| *n == bare) {
                Some((_, id)) => format!("{id}:{rest}"),
                None => line.to_string(),
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Cut lines to a narration budget.
///
/// The conversation is cut where it stops fitting, not thinned out. A line
/// keeps its whole sentences or goes, and once one line has been shortened or
/// dropped every line after it goes too: the next line was written to answer
/// the one before it, and skipping a line turns a dialogue into non sequiturs.
/// Then the part may not end on a question, because the line that answered it
/// is the one that was cut — a real session ended slide 4 on "How does the
/// 'Inner Monologue' method contribute to the." and moved on.
fn fit_to(lines: &mut Vec<crate::parse::ScriptedLine>, total: usize) {
    drop_restatements(lines);
    let mut spent = 0usize;
    let mut open = true;
    lines.retain_mut(|l| {
        if !open {
            return false;
        }
        let Some(room) = budget::room(spent, total) else {
            open = false;
            return false;
        };
        let whole = l.text.trim().chars().count() <= room;
        let Some(fitted) = budget::whole_sentences(&l.text, room) else {
            open = false;
            return false;
        };
        open = whole;
        l.text = fitted;
        spent += l.text.chars().count();
        true
    });
    drop_dangling_question(lines);
}

/// Drop a line that says again what the same speaker said in the line before.
///
/// A small script model often writes a point twice in a row, short and then
/// long — measured on a real build, three slides of five had "Adam: Moshi is
/// full-duplex and real-time." straight followed by "Adam: Moshi's full-duplex
/// design means it can listen and speak simultaneously…". Heard aloud that is
/// one person repeating themselves. The longer line is kept, because it is the
/// one that carries the explanation.
fn drop_restatements(lines: &mut Vec<crate::parse::ScriptedLine>) {
    let mut out: Vec<crate::parse::ScriptedLine> = Vec::with_capacity(lines.len());
    for l in lines.drain(..) {
        if let Some(prev) = out.last_mut()
            && prev.speaker_id == l.speaker_id
            && overlaps(&prev.text, &l.text)
        {
            if l.text.chars().count() > prev.text.chars().count() {
                *prev = l;
            }
            continue;
        }
        out.push(l);
    }
    *lines = out;
}

/// Whether two lines say mostly the same thing: two in five or more of the
/// content words of the shorter one are in the longer one. Half was too strict
/// for a paraphrase — the measured pair below shares seven of fifteen — and the
/// test only ever runs on one speaker talking twice in a row, which a dialogue
/// rarely does for a good reason.
fn overlaps(a: &str, b: &str) -> bool {
    const STOP: &[&str] = &[
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "by", "is",
        "are", "was", "it", "its", "it's", "this", "that", "these", "those", "as", "at", "from",
        "be", "can", "so", "than", "then", "also", "which", "while", "into", "without", "their",
    ];
    let words = |t: &str| -> std::collections::HashSet<String> {
        t.split(|c: char| !c.is_alphanumeric() && c != '\'')
            .map(|w| w.to_lowercase())
            .filter(|w| w.len() > 2 && !STOP.contains(&w.as_str()))
            .collect()
    };
    let (x, y) = (words(a), words(b));
    let (small, big) = if x.len() <= y.len() {
        (&x, &y)
    } else {
        (&y, &x)
    };
    small.len() >= 3 && small.iter().filter(|w| big.contains(*w)).count() * 5 >= small.len() * 2
}

/// Drop the trailing lines that ask something nobody answers.
///
/// A part is spoken and then the next one starts from its own script, so a
/// question left at the end of a part is heard and never answered. Never
/// empties a part.
fn drop_dangling_question(lines: &mut Vec<crate::parse::ScriptedLine>) {
    while lines.len() > 1
        && lines
            .last()
            .is_some_and(|l| l.text.trim_end().ends_with('?'))
    {
        lines.pop();
    }
}

/// A slide's parsed narration and copy, cut to budget.
///
/// `raw` is the reply the layout is read from; `total` is the narration budget,
/// which is larger on a slide that also carries the welcome or the close.
fn shape(
    mut lines: Vec<crate::parse::ScriptedLine>,
    raw: &str,
    elements: Vec<String>,
    total: usize,
    images: Option<usize>,
) -> SlideDraft {
    // The layout is validated against the closed set and falls back rather than
    // being passed through: a name outside the set is worse than the plain
    // one, because the slide model then guesses the composition too.
    let layout = crate::parse::layout_of(raw).unwrap_or_else(|| "text only".to_string());
    let mut on_slide = vec![format!("Layout: {layout}")];
    // Each `[image: …]` line becomes a generated image, so the limit is applied
    // here as well as asked for: a model told "at most one" sometimes writes two.
    let mut pictures = 0usize;
    let elements = elements.into_iter().filter(|e| {
        if !e.trim_start().starts_with("[image:") {
            return true;
        }
        pictures += 1;
        images.is_none_or(|max| pictures <= max)
    });
    for e in elements.take(budget::SLIDE_ELEMENTS) {
        let fitted = budget::fit(&e, budget::ELEMENT);
        if !fitted.trim().is_empty() {
            on_slide.push(fitted);
        }
    }

    // The budget is enforced on the way out, not requested and hoped for. A
    // line is dropped rather than shortened once the room left is too small to
    // say anything in — see `budget::room`.
    fit_to(&mut lines, total);
    SlideDraft { lines, on_slide }
}

/// What every slide is told about the session it sits in.
///
/// Each slide is one model call, and until this existed each call saw only its
/// own topic. Asked to narrate "Linux processes" with nothing else, a model
/// does what a model does with a blank page: it opens a talk. A real five-slide
/// session said "Welcome to our session" on four of its five slides and wrapped
/// up on the fifth, and it played as five separate clips rather than one
/// session. The greeting and the wrap-up belong to the bookends, written once;
/// the slides are the middle, and they are told so.
const CONTINUITY: &str = "This slide is one part of a single continuous session that is already \
     under way. The listener has heard every slide before this one; the session's welcome and \
     its closing are spoken separately, not by you. So never greet, welcome or introduce the \
     session, never say what \"today\" or \"this session\" covers, and never sum up or wrap up. \
     Pick up where the previous slide left off, without repeating it, and explain this slide's \
     points — only this slide's: what other slides own is theirs to explain. If there is a \
     next slide, you may end with a short statement that leads towards it, never a question. \
     The first line may answer or build on the last thing said on the previous slide.";

/// The speakers as the prompts list them: id, name, role.
fn roster(spec: &ScriptSpec) -> String {
    spec.speakers
        .iter()
        .map(|s| format!("  {} ({}): {}", s.speaker_id.0, s.display_name, s.role))
        .collect::<Vec<_>>()
        .join("\n")
}

/// What an audio overview's format asks of the script, on top of the host
/// dynamic in [`dialogue`].
///
/// NotebookLM's four formats, as its own descriptions put them (Deep Dive: "a
/// lively conversation"; Brief: one host, under two minutes; Critique: "an
/// expert review, offering constructive feedback"; Debate: "different
/// perspectives"), written out the way the open implementations and listeners'
/// complaints point. The stock phrases listeners tire of most are banned by
/// name.
fn format_rules(a: &opennotebook_session::AudioSpec) -> String {
    use opennotebook_session::AudioFormat as F;
    let common = "This is audio only. Nothing is ever on screen, so never say \"as you can see\", \
         \"on the slide\" or \"this chart\", and say numbers the way a person says them aloud. \
         Never say \"deep dive\", \"let's dive in\", \"unpack\", \"buckle up\" or \"stay \
         tuned\", and do not let the hosts agree with each other more than once in six lines: \
         agreeing all the time is the most common complaint about shows like this.";
    let shape = match a.format {
        F::DeepDive => {
            "Format: Deep Dive. Two hosts in a lively conversation that makes the listener \
             understand the material. Every chapter moves from a claim, to how it works, to a \
             concrete example or analogy from the material, and somewhere in it the second \
             host pushes back, adds a caveat or asks \"but wait\": real tension, not \
             performed agreement."
        }
        F::Brief => {
            "Format: Brief. One host, about two minutes, for a listener who wants the gist. \
             Open on the single most important or surprising point, then the other key points \
             with one concrete detail each, then a one-sentence takeaway. No banter, no \
             greeting ritual, no padding."
        }
        F::Critique => {
            "Format: Critique. Two hosts give the material an expert review, as if it were \
             the listener's own work: the first host reviews, the second speaks for the \
             author and asks what the fix would be. Strengths first, named specifically. \
             Then the weaknesses, most important first, each with where it is in the \
             material and what would fix it. Constructive and concrete, never vague praise \
             or vague blame."
        }
        F::Debate => {
            "Format: Debate. The two hosts take different positions on the question the \
             material raises, each argued from what the material says, never a straw man. \
             Each makes their case, they answer each other's strongest point, and each \
             concedes something real. Nobody wins: the listener is left with the open \
             question and what would settle it."
        }
    };
    format!("{shape}\n{common}")
}

/// How the speakers talk, said once and used by every call that writes speech.
///
/// UNSPECIFIED BY SECTION 3: what two speakers do differently. The roles are
/// the session's own `Speaker.role` text, but a role of "narrator" next to
/// "expert" told the model nothing about how a conversation moves, and it wrote
/// an interview: one speaker asking "Can you elaborate on the practical
/// implications of…?", the other answering in a paragraph, the same fact said
/// by both. NotebookLM's two hosts are a curious host and an explaining one,
/// and every question follows from the line before it; the rules below are
/// that dynamic, written as plainly as the open reimplementations write it.
/// The first speaker leads, which is the host's seat; the roles still say who
/// they are.
fn dialogue(spec: &ScriptSpec) -> String {
    const BANNED: &str = "Never use these words or openers: \"Certainly\", \"Absolutely\", \
         \"Great question\", \"Fascinating\", \"That's interesting\", \"delve\", \"elaborate\", \
         \"in summary\", \"in conclusion\", \"it's worth noting\".";
    let name = |i: usize| {
        spec.speakers
            .get(i)
            .map(|s| s.display_name.clone())
            .unwrap_or_default()
    };
    if spec.speakers.len() < 2 {
        return format!(
            "How it sounds:\n\
             - Speak straight to the listener as \"you\", the way a good teacher explains one \
               to one. You may raise the question the listener is probably thinking, and \
               answer it in the same breath.\n\
             - Short sentences. Concrete numbers and examples from the material, and an \
               everyday analogy when it makes an idea click.\n\
             - Say each idea once. Something already explained earlier in the session is \
               referred to in a few words, never explained again.\n\
             - {BANNED}"
        );
    }
    let (host, guest) = (name(0), name(1));
    // A debate and a critique are not a newcomer and an explainer: measured,
    // a Debate written with those roles had both hosts explaining the same
    // side, and the disagreement only arrived in the closing line. Their own
    // roles replace the first rule; every other rule still holds.
    use opennotebook_session::AudioFormat as F;
    let roles = match spec.audio.as_ref().map(|a| a.format) {
        Some(F::Debate) => Some(format!(
            "- {host} argues one side of the question the material raises and {guest} argues \
               the other, each from what the material says. They answer each other's strongest \
               point directly, they keep their sides all the way through, and each concedes \
               something real near the end. Neither is the listener's stand-in."
        )),
        Some(F::Critique) => Some(format!(
            "- {host} is the reviewer: names what works and what does not, specifically, with \
               where it is in the material. {guest} speaks for the author: explains the \
               intent, pushes back where the criticism is unfair, and asks what the fix would \
               be. Constructive, never vague praise or vague blame."
        )),
        _ => None,
    };
    if let Some(roles) = roles {
        return format!(
            "How the conversation sounds — two people talking, not an interview:\n\
             {roles}\n\
             - Every question comes out of the line just before it, and is answered in the \
               very next line. Nothing ends on an unanswered question.\n\
             - Short turns, like real talk. Most lines are one or two sentences.\n\
             - Never repeat or reword what the other speaker just said. Say each point once \
               in the whole episode.\n\
             - Do not start a line with the other speaker's name.\n\
             - {BANNED}"
        );
    }
    format!(
        "How the conversation sounds — two people talking, not an interview:\n\
         - {host} leads and is the listener's stand-in: sharp but new to this. {host} asks what \
           a smart newcomer would ask at that exact moment, reacts honestly (\"Wait, so…\", \
           \"Huh, that's faster than I'd have guessed\"), and now and then says an idea back \
           in plain words to check it (\"So basically…\"). {guest} explains, with the \
           material's concrete numbers and examples, and an everyday analogy when one helps.\n\
         - Every question comes out of the line just before it: something unclear, \
           surprising, or a natural \"so what?\". Never a generic interview question such as \
           \"Can you elaborate on…\", \"How does X differ from Y?\" or \"What are the \
           implications of…\".\n\
         - A question is answered in the very next line, and an answer never ends by asking \
           the other speaker something back. Nothing ends on an unanswered question.\n\
         - Short turns, like real talk. Most lines are one or two sentences; an explanation \
           may take three. Some lines are just a quick reaction and the next thought.\n\
         - Never repeat or reword what the other speaker just said. Say each idea once in \
           the whole session: something already explained is referred to in a few words \
           (\"that inner monologue trick\") and only what is new is added.\n\
         - Do not start a line with the other speaker's name.\n\
         - {BANNED}"
    )
}

impl Planned {
    /// What to retrieve this part's material with: its title and its points,
    /// so a part is grounded on what it explains rather than on a heading that
    /// four other parts share.
    fn query(&self) -> String {
        if self.points.is_empty() {
            return self.title.clone();
        }
        format!("{}. {}", self.title, self.points.join(". "))
    }
}

/// The plan as the prompts show it, each part with the points it owns, and
/// `here` marked when the reader is writing one of them.
fn plan_listing(plan: &[Planned], here: Option<usize>) -> String {
    let mut out =
        String::from("The session, part by part, and the points each part alone explains:\n");
    for (i, p) in plan.iter().enumerate() {
        let mark = if Some(i) == here {
            "  <- this slide"
        } else {
            ""
        };
        out.push_str(&format!("  {}. {}{mark}\n", i + 1, p.title));
        for point in &p.points {
            out.push_str(&format!("     - {point}\n"));
        }
    }
    out
}

/// A slide's place in the session: which one it is, what surrounds it, and how
/// the slide before it ended.
struct Place<'a> {
    ordinal: usize,
    plan: &'a [Planned],
    /// The previous slide's last lines, as spoken, so this one continues rather
    /// than restarts. Empty on the first slide.
    before: Vec<String>,
    /// The copy earlier slides already put on screen, so this one does not
    /// show it again.
    shown: Vec<String>,
}

impl Place<'_> {
    fn here(&self) -> &Planned {
        &self.plan[self.ordinal]
    }

    fn describe(&self) -> String {
        let mut out = format!(
            "{}\nThis is slide {} of {}: {}. Explain its points; the other slides' points are \
             theirs.",
            plan_listing(self.plan, Some(self.ordinal)),
            self.ordinal + 1,
            self.plan.len(),
            self.here().title
        );
        if self.before.is_empty() {
            out.push_str("\n\nThe welcome has just been spoken; go straight into the topic.");
        } else {
            out.push_str("\n\nThe previous slide ended with:\n");
            out.push_str(&self.before.join("\n"));
        }
        if !self.shown.is_empty() {
            out.push_str(
                "\n\nAlready on earlier slides, so not to be shown again on this one — \
                 pick a different fact, number or point:\n",
            );
            out.push_str(&self.shown.join("\n"));
        }
        if self.ordinal + 1 == self.plan.len() {
            out.push_str("\n\nThis is the last slide; the closing is spoken after it, not by you.");
        }
        out
    }
}

/// Two `Stat:` lines that show the same number. The number is what the eye
/// catches; the caption under it is reworded from slide to slide.
fn same_stat(a: &str, b: &str) -> bool {
    let figure = |s: &str| {
        s.trim_start_matches("Stat:")
            .split('—')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase()
    };
    let (x, y) = (figure(a), figure(b));
    !x.is_empty() && x == y
}

/// How many of the previous slide's lines the next one is shown.
const CARRIED_LINES: usize = 2;

/// Drop the lines that open a talk rather than continue one.
///
/// The net under [`CONTINUITY`], for a model that greets regardless. Only the
/// LEADING lines go, because a greeting comes first — a last slide that ends on
/// "today we covered…" is closing, not opening, and keeps it. It matches the
/// openings actually seen and never empties a slide: if every line is a
/// greeting, the slide keeps them, because a slide that says "welcome" is a
/// better outcome than a prep that fails over one.
fn drop_preamble(lines: &mut Vec<crate::parse::ScriptedLine>) {
    const OPENERS: &[&str] = &[
        "welcome",
        "hello",
        "hi everyone",
        "hi, everyone",
        "good morning",
        "good afternoon",
        "good evening",
        "today we",
        "today, we",
        "in this session",
        "in today's session",
        "in this presentation",
        "here's what you'll learn",
        "here is what you'll learn",
    ];
    let opens = |text: &str| {
        let t = text.trim_start().to_lowercase();
        OPENERS.iter().any(|o| t.starts_with(o))
    };
    let lead = lines.iter().take_while(|l| opens(&l.text)).count();
    if lead < lines.len() {
        lines.drain(..lead);
    }
}

/// The whole session as one script, cut at the slide boundaries.
///
/// This is how NotebookLM's overviews are made: the sources become a plan, the
/// plan becomes ONE script in which the hosts carry a single thread from start
/// to finish, and the visuals follow that script. Writing each slide in its own
/// call is the opposite — five cold starts — and it sounded like it: a real
/// session greeted the listener on four slides out of five. One call sees the
/// whole arc, so the welcome is said once, each part picks up from the last,
/// the speakers answer each other across a slide change, and the close comes
/// at the end.
///
/// Returns one entry per topic. `None` is a part the reply did not deliver or
/// that could not be parsed — the caller writes that slide on its own rather
/// than failing the prep, the same trade as everywhere else here.
async fn write_session(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    spec: &ScriptSpec,
    plan: &[Planned],
) -> Result<Vec<Option<SlideDraft>>, ScriptError> {
    let ids = spec.speaker_ids();
    let roster = roster(spec);

    // Each part is grounded on its own points, and the material is labelled by
    // part so the model knows what belongs where: a part handed the whole
    // source explains the whole source, which is the repetition this replaced.
    let mut material = String::new();
    for (i, part) in plan.iter().enumerate() {
        let context =
            grounding::retrieve(memory, workspace, collection, &part.query(), TOP_K).await?;
        material.push_str(&format!(
            "--- Material for part {} ({}) ---\n{}\n",
            i + 1,
            part.title,
            budget::fit(&context.as_context(), MATERIAL_PER_PART)
        ));
    }

    let n = plan.len();
    let system = format!(
        "You write the complete spoken script of a short explanatory session, from start to \
         finish, in one piece.\n\
         Speakers, by id:\n{roster}\n\n\
         The session is shown as {n} slides, one per part, and the script is cut into the same \
         {n} parts. It is ONE continuous conversation, not {n} separate talks:\n\
         - Part 1 opens with a hook: one or two sentences on why this matters to the listener \
           — a surprising fact, a problem they will recognise, or a question the session \
           answers — then goes straight into its points.\n\
         - Every later part carries straight on from the one before it. No greeting, no \
           \"welcome\", no \"today we will\", no re-introducing the subject, and no announcing the \
           part (\"let's continue\", \"now we explore\", \"moving on\"): just say the next \
           thing, the way a conversation moves on. A part may begin by building on the last \
           line of the previous part, and may end with a statement that leads to the next.\n\
         - Each part explains the points the plan gives it, and only those. A point owned by \
           an earlier part has been explained already: refer back to it in a few words, \
           never explain it again. A point owned by a later part is left for later.\n\
         - Part {n} ends on its last point. The closing is spoken after it, separately, so \
           nothing is summed up or wrapped up anywhere in the script.\n\n\
         {dialogue}\n\n\
         Format, exactly. For each part, in order:\n\
         === PART <number> ===\n\
         then the spoken lines, each starting with a speaker id exactly as written, a colon, \
         then what they say — like this:\n\
         {example}: what they say\n\
         Use no other name and no other prefix. Each line at most {line} characters. Each part \
         has about {target} characters of narration, and never more than {total}.\n\
         Then the line `SLIDE:` on its own, and after it the copy that appears ON that part's \
         slide — the few words that anchor what is being said, never the spoken sentences \
         again. One `Layout:` line naming one of: {layouts}. Then at most {elements} lines, \
         each one element, exact final wording:\n\
         - Point: <a few words>\n\
         - Subhead: <a short phrase>\n\
         - Stat: <number> — <what it measures>\n\
         {image}\
         Each element at most {elen} characters. No sentences, no full stops at the end of a \
         Point, no colours or fonts.\n\n\
         Say only what the material supports. Do not introduce facts from your own knowledge, \
         and never leave a placeholder of any kind.",
        dialogue = dialogue(spec),
        example = example_id(spec),
        line = budget::LINE,
        target = spec.narration_target(),
        total = spec.slide_narration,
        image = spec.image_element(),
        layouts = crate::parse::LAYOUTS.join(", "),
        elements = budget::SLIDE_ELEMENTS,
        elen = budget::ELEMENT,
    );
    let user = format!(
        "Session title: {}\n\n{}\n{material}",
        spec.title,
        plan_listing(plan, None)
    );

    // Truncation keeps the parts before the cut; the ones after it are `None`
    // and are written one at a time.
    let system = spec.adapt(system);
    let user = spec.adapt_user(user);
    let raw = complete_partial(&spec.language, &system, &user, "session script", false).await?;
    let parts = split_parts(&raw, n);

    Ok(parts
        .into_iter()
        .enumerate()
        .map(|(i, part)| {
            let part = part?;
            let (spoken, elements) = crate::parse::split_slide_reply(&speech_first(&part, &ids));
            let mut lines = lines_of(spec, &spoken).ok()?;
            if i > 0 {
                drop_preamble(&mut lines);
            }
            if lines.is_empty() {
                return None;
            }
            let total = slide_total(spec, i, n);
            Some(shape(lines, &part, elements, total, spec.images_per_slide))
        })
        .collect())
}

/// A part with its spoken lines moved in front of its `SLIDE:` block.
///
/// Writing the whole session, the model sometimes carries on talking after the
/// slide copy — seen in a real reply, where part 1's second line came after its
/// `Point:`. `split_slide_reply` reads everything after the marker as slide
/// copy and drops a line it does not recognise, so that line would vanish
/// without a trace. A line that opens with a speaker id is speech wherever it
/// sits.
fn speech_first(part: &str, ids: &[SpeakerId]) -> String {
    let is_speech = |line: &str| {
        let Some((label, _)) = line.split_once(':') else {
            return false;
        };
        let label = label.trim().trim_matches(|c: char| c == '*' || c == '_');
        ids.iter().any(|id| id.0.eq_ignore_ascii_case(label))
    };
    let (speech, rest): (Vec<&str>, Vec<&str>) = part.lines().partition(|l| is_speech(l));
    let mut out = speech.join("\n");
    out.push('\n');
    out.push_str(&rest.join("\n"));
    out
}

/// Characters of source material each part of the whole-session call carries.
///
/// Retrieval returns whole passages, and some are long: the first live run
/// asked for 309,138 tokens against the model's 128,000 and got a 400. One slide
/// on its own fits, five together do not. 12,000 characters is about 3,000
/// tokens, so five parts plus the instructions stay near 20,000 — well inside
/// the window, and more than a 650-character part can use.
const MATERIAL_PER_PART: usize = 12_000;

/// Cut a whole-session reply at its `=== PART n ===` markers.
///
/// The marker is read loosely — `## Part 2`, `PART 2:`, `**Slide 2**` all count
/// — because the model is small and decoration is its habit. It needs a number,
/// which is what keeps it apart from the bare `SLIDE:` copy marker. Text before
/// the first marker is dropped; a part that appears twice keeps the first.
fn split_parts(raw: &str, n: usize) -> Vec<Option<String>> {
    let mut parts: Vec<Option<String>> = vec![None; n];
    let mut current: Option<usize> = None;
    for line in raw.lines() {
        if let Some(k) = part_marker(line) {
            current = (1..=n)
                .contains(&k)
                .then_some(k - 1)
                .filter(|&i| parts[i].is_none());
            if let Some(i) = current {
                parts[i] = Some(String::new());
            }
            continue;
        }
        if let Some(buf) = current.and_then(|i| parts[i].as_mut()) {
            buf.push_str(line);
            buf.push('\n');
        }
    }
    parts
}

fn part_marker(line: &str) -> Option<usize> {
    let t = line
        .trim()
        .trim_matches(|c: char| matches!(c, '=' | '#' | '*' | '_' | '-' | ':' | ' ' | '[' | ']'));
    let lower = t.to_ascii_lowercase();
    let rest = lower
        .strip_prefix("part")
        .or_else(|| lower.strip_prefix("slide"))?;
    // A number must follow the word: that is what separates "Part 2" from
    // the bare `SLIDE:` copy marker, whose colon is trimmed above. Anything
    // after the number is a title the model added, and is ignored.
    let rest = rest.trim_start();
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    digits.parse().ok()
}

/// The opening and the closing, as spoken lines.
///
/// A session that starts mid-explanation and stops mid-sentence sounds like a
/// clip of something rather than a thing made for you. The opening says what is
/// about to be covered and why it is worth the next few minutes; the closing
/// says what was covered. Both are written from the OUTLINE rather than from the
/// source material, because they are about the shape of the session, not about
/// its facts — and a bookend that invents a fact is worse than no bookend.
///
/// They are lines on the first and last slides rather than slides of their own.
/// The deck is built from the outline titles, so an extra slide here would need
/// an extra slide there, and a title card saying "Introduction" is a slide
/// nobody needs to look at.
async fn bookend(
    spec: &ScriptSpec,
    plan: &[Planned],
    which: Bookend,
) -> Result<Vec<crate::parse::ScriptedLine>, ScriptError> {
    let roster = roster(spec);

    let job = match which {
        Bookend::Intro => {
            "Write the OPENING of the session. Open with a hook — why this matters to the listener, or the question the session answers — then say in a breath what it covers. Do not cover the material itself — that is what the rest of the session is for. Do not say \"in this presentation\" or \"today we will discuss\"; just start."
        }
        Bookend::Outro => {
            "Write the CLOSING of the session. In one or two sentences of ordinary conversation, say the one or two things most worth remembering — not a list of every slide. Then close warmly and briefly. Do not introduce anything new, do not thank the listener for watching, and do not invite questions — they could ask at any point and the session is over now."
        }
    };

    // An audio overview closes the way its format ends: NotebookLM's deep
    // dives end on a takeaway and a question to think about; a debate leaves
    // its question open; a critique leaves the revisions in order.
    let audio_close = spec.audio.as_ref().map(|a| {
        use opennotebook_session::AudioFormat as F;
        match a.format {
            F::Debate => "Write the CLOSING of the debate: each host says in one sentence what they would concede, then leave the listener with the open question and what would settle it. No winner. Close briefly.",
            F::Critique => "Write the CLOSING of the critique: the two or three revisions that matter most, in order of priority, in ordinary conversation. Close briefly and encouragingly.",
            F::Brief => "Write the CLOSING of the brief: the takeaway in one sentence. Nothing else.",
            F::DeepDive => "Write the CLOSING of the episode: the one or two things worth remembering, said as conversation rather than a recap, then one question for the listener to think about, then a short sign-off. Do not introduce anything new.",
        }
    });
    let job = match (which, audio_close) {
        (Bookend::Outro, Some(close)) => close,
        _ => job,
    };
    let system = format!(
        "You write spoken narration for an explanatory session.\nSpeakers, by id:\n{roster}\n\n{job}\n\nReturn only spoken lines. Start every line with one of those ids exactly as written, then a colon, then what they say — like this:\n{example}: what they say\nUse no other name and no other prefix. At most {n} lines, each at most {line} characters. This is speech: no headings, no lists, no stage directions.",
        example = example_id(spec),
        n = BOOKEND_LINES,
        line = budget::LINE,
    );
    let user = format!(
        "Session title: {}\n\n{}",
        spec.title,
        plan_listing(plan, None)
    );

    let stage = match which {
        Bookend::Intro => "intro",
        Bookend::Outro => "outro",
    };
    let system = spec.adapt(system);
    let user = spec.adapt_user(user);
    let raw = complete(&spec.language, &system, &user, stage).await?;
    // A bookend that cannot be parsed is dropped, not fatal. It is the frame
    // around the session, and a session without a frame is still the session;
    // failing the whole prep over a welcome line would be the wrong trade.
    let mut lines = lines_of(spec, &raw).unwrap_or_default();
    // An audio overview's close carries more: a debate's needs both hosts to
    // concede something and the open question, which did not fit in one
    // line's budget and was cut mid-sentence ("I'd concede that text-based
    // systems let.").
    let (max_lines, budget_total) = if spec.audio.is_some() {
        (BOOKEND_LINES + 2, 3 * budget::LINE)
    } else {
        (BOOKEND_LINES, budget::BOOKEND_NARRATION)
    };
    lines.truncate(max_lines);
    // Enforced on the way out, like every other budget here: the bookend rides
    // on a slide that has already spent its own, so what it adds is counted.
    // Whole sentences only, and a line that does not fit ends the bookend
    // rather than being chopped, the same rule as `fit_to`. Unlike `fit_to`
    // a closing may end on a question: one for the listener to think about is
    // what a deep dive's close is for.
    let mut spent = 0usize;
    let mut open = true;
    lines.retain_mut(|l| {
        if !open {
            return false;
        }
        let Some(room) = budget::room(spent, budget_total) else {
            open = false;
            return false;
        };
        let whole = l.text.trim().chars().count() <= room;
        let Some(fitted) = budget::whole_sentences(&l.text, room) else {
            open = false;
            return false;
        };
        open = whole;
        l.text = fitted;
        spent += l.text.chars().count();
        true
    });
    Ok(lines)
}

#[derive(Clone, Copy)]
enum Bookend {
    Intro,
    Outro,
}

/// Two lines is a welcome; four is a preamble nobody asked for.
const BOOKEND_LINES: usize = 2;

/// Build the session's slides. Nothing here synthesises: `audio_path` and
/// `duration_ms` stay absent, and `cues` stays empty.
pub async fn generate_script(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    spec: &ScriptSpec,
) -> Result<Vec<SessionSlide>, ScriptError> {
    let plan = outline(memory, workspace, collection, spec).await?;

    // One script for the whole session. A reply that fails outright is not
    // fatal: every slide is then written on its own, as below.
    // Said on stderr, which is the prep job's log: a silent fallback is how the
    // first deploy of this failed without anyone being able to say why.
    let one_piece = spec.slide_narration <= budget::WHOLE_SESSION_MAX_SLIDE;
    if !one_piece {
        eprintln!(
            "opennotebook prep: {} characters a slide is a long session; writing each slide on its own",
            spec.slide_narration
        );
    }
    let written = if one_piece {
        write_session(memory, workspace, collection, spec, &plan).await
    } else {
        Ok(Vec::new())
    };
    let mut whole = match written {
        Ok(parts) => parts,
        Err(e) => {
            eprintln!(
                "opennotebook prep: whole-session script failed ({e}); writing each slide on its own"
            );
            Vec::new()
        }
    };
    whole.resize_with(plan.len(), || None);
    let missing: Vec<String> = whole
        .iter()
        .enumerate()
        .filter(|(_, p)| p.is_none())
        .map(|(i, _)| (i + 1).to_string())
        .collect();
    if !missing.is_empty() {
        eprintln!(
            "opennotebook prep: slide(s) {} written on their own",
            missing.join(", ")
        );
    }

    let mut slides = Vec::with_capacity(plan.len());
    let mut scripts: Vec<Vec<crate::parse::ScriptedLine>> = Vec::with_capacity(plan.len());
    // Closes written apart from the whole-session reply, added after the edit
    // pass so neither the cut nor the editor can take them.
    let mut closes: std::collections::HashMap<usize, Vec<crate::parse::ScriptedLine>> =
        std::collections::HashMap::new();
    let mut before: Vec<String> = Vec::new();
    let mut shown: Vec<String> = Vec::new();
    for (ordinal, part) in plan.iter().enumerate() {
        let topic = &part.title;
        let last = ordinal + 1 == plan.len();
        let draft = match whole[ordinal].take() {
            Some(mut draft) => {
                // The close is written on its own and added after the cut,
                // never asked of the whole-session reply: it came last in that
                // reply, so it was the first thing the budget cut, and a real
                // build ended on a question about latency with no goodbye.
                if last {
                    let close = bookend(spec, &plan, Bookend::Outro)
                        .await
                        .unwrap_or_default();
                    closes.insert(ordinal, close);
                }
                top_up(
                    memory,
                    workspace,
                    collection,
                    spec,
                    &plan,
                    ordinal,
                    slide_total(spec, ordinal, plan.len()),
                    &mut draft,
                )
                .await;
                draft
            }
            None => {
                // The fallback: this slide alone, told where it sits and how
                // the previous one ended, with the welcome or the close written
                // separately when this slide is the one that carries it.
                let place = Place {
                    ordinal,
                    plan: &plan,
                    before: before.clone(),
                    shown: shown.clone(),
                };
                let mut draft = script_one(memory, workspace, collection, spec, &place).await?;
                if ordinal == 0 {
                    let mut lines = bookend(spec, &plan, Bookend::Intro)
                        .await
                        .unwrap_or_default();
                    lines.append(&mut draft.lines);
                    draft.lines = lines;
                }
                if last {
                    // Kept out of the edit pass with the whole-session close:
                    // the editor trims a closing question, and a deep dive's
                    // close ends on one on purpose.
                    let close = bookend(spec, &plan, Bookend::Outro)
                        .await
                        .unwrap_or_default();
                    closes.insert(ordinal, close);
                }
                draft
            }
        };
        before = draft
            .lines
            .iter()
            .rev()
            .take(CARRIED_LINES)
            .rev()
            .map(|l| format!("{}: {}", l.speaker_id.0, l.text))
            .collect();
        // The backstop under the prompt: a stat already shown is dropped, and
        // every remaining element is remembered for the slides after this one.
        // Written one slide at a time, a small model reached for the same
        // quotable number on all five slides of a real build.
        let on_slide: Vec<String> = draft
            .on_slide
            .into_iter()
            .filter(|e| !(e.starts_with("Stat:") && shown.iter().any(|s| same_stat(s, e))))
            .collect();
        shown.extend(
            on_slide
                .iter()
                .filter(|e| !e.starts_with("Layout:"))
                .cloned(),
        );
        scripts.push(draft.lines);
        let slide_name = slug(topic, ordinal);
        slides.push(SessionSlide {
            // The outline's own words. Until this was carried the title was
            // spent on the slug and dropped, and the renderer had no heading.
            title: topic.to_string(),
            on_slide,
            slide_ref: SlideRef {
                collection: spec.deck_collection.clone(),
                presentation: spec.deck_presentation.clone(),
                slide: slide_name,
            },
            ordinal: ordinal as u32,
            // The studio's slides are HTML at 1920x1080. A png deck from an
            // earlier slide service was 1376x768; the deck step sets this from
            // what was actually written.
            aspect: Aspect {
                width: 1920,
                height: 1080,
            },
            // Filled below, once the edit pass has read the whole script.
            lines: Vec::new(),
        });
    }

    review(spec, &plan, &mut scripts).await;
    for (ordinal, close) in closes {
        if let Some(lines) = scripts.get_mut(ordinal) {
            lines.extend(close);
        }
    }

    for (slide, scripted) in slides.iter_mut().zip(scripts) {
        let ordinal = slide.ordinal;
        slide.lines = scripted
            .into_iter()
            .enumerate()
            .map(|(n, l)| NarrationLine {
                line_id: LineId(format!("s{ordinal}l{n}")),
                speaker_id: l.speaker_id,
                ordinal: n as u32,
                text: l.text,
                audio_path: None,
                duration_ms: None,
                cues: Vec::new(),
            })
            .collect();
    }
    Ok(slides)
}

/// How much of the script before a part the edit pass is shown.
const REVIEW_CONTEXT: usize = 12_000;

/// The edit pass: every part read again by an editor who has heard everything
/// before it, and fixed in place.
///
/// NotebookLM's own pipeline, as its team described it, is outline, revised
/// outline, script, critique, rewrite: the first draft is not what is spoken.
/// A draft has the faults only a reader of the WHOLE script can see — the idea
/// a later part explains again, the question nobody answered, the interview
/// question that does not follow from anything — and the writer of one part
/// cannot see them. So each part is sent back with the script before it, in
/// order, so the edit of part 3 reads the already-edited parts 1 and 2.
///
/// An edit is a chance to make a part worse, so it is only taken when it is
/// plainly the same part: it parses, it names the same speakers, and it did not
/// lose more than two fifths of its length. Otherwise the draft stands, and any
/// failure leaves the draft as it was — the pass improves a script, it never
/// costs one.
async fn review(
    spec: &ScriptSpec,
    plan: &[Planned],
    scripts: &mut [Vec<crate::parse::ScriptedLine>],
) {
    let n = scripts.len();
    let speak = |lines: &[crate::parse::ScriptedLine]| {
        lines
            .iter()
            .map(|l| format!("{}: {}", l.speaker_id.0, l.text))
            .collect::<Vec<_>>()
            .join("\n")
    };
    for i in 0..n {
        if scripts[i].is_empty() {
            continue;
        }
        let earlier = scripts[..i]
            .iter()
            .enumerate()
            .map(|(k, l)| format!("--- part {} ---\n{}", k + 1, speak(l)))
            .collect::<Vec<_>>()
            .join("\n");
        // The most recent part matters most, so a long session keeps the tail.
        let skip = earlier.chars().count().saturating_sub(REVIEW_CONTEXT);
        let earlier: String = earlier.chars().skip(skip).collect();
        let draft = speak(&scripts[i]);
        let system = format!(
            "You are the editor of a spoken explanatory session. You get the plan, what has \
             already been said, and the draft of part {k} of {n}. Return part {k}, fixed.\n\
             Speakers, by id:\n{roster}\n\n\
             Fix only these faults, and keep everything else as it is:\n\
             1. An idea already explained earlier is explained again: cut the repeat, or \
                replace it with a brief reference back and keep only what is new.\n\
             2. A generic interview question (\"Can you elaborate on…\", \"How does X differ \
                from Y?\", \"What are the implications of…\") that does not come out of the \
                line before it: rewrite it as a real reaction to that line, or cut it.\n\
             3. A question not answered by the very next line, or a part that ends on a \
                question: answer it from what the draft says, or cut it.\n\
             4. A sentence that is cut off or unfinished: finish it or cut it.\n\
             5. A line that repeats or rewords the line before it: cut it.\n\
             6. Stock phrases: \"Certainly\", \"Absolutely\", \"Great question\", \
                \"Fascinating\", \"delve\", \"elaborate\", \"in summary\", a line starting \
                with the other speaker's name, or {greet}.\n\
             Do not add facts, do not change who says what unless a fix needs it, and keep \
             the part about as long as the draft: a fix that cuts a sentence makes room for \
             the next sentence of explanation, it does not shorten the part.\n\
             Return only the spoken lines of part {k}, each starting with a speaker id exactly \
             as written, a colon, then what they say — like this:\n{example}: what they say\n\
             Each line at most {line} characters.",
            k = i + 1,
            roster = roster(spec),
            greet = if i == 0 {
                "a welcome longer than two sentences"
            } else {
                "a greeting or welcome — the session is already under way"
            },
            example = example_id(spec),
            line = budget::LINE,
        );
        let user = format!(
            "{}\nAlready said, in order:\n{}\n\nDRAFT OF PART {}:\n{draft}",
            plan_listing(plan, Some(i)),
            if earlier.is_empty() {
                "(nothing: this is the first part)".to_string()
            } else {
                earlier
            },
            i + 1,
        );
        let system = spec.adapt(system);
        let user = spec.adapt_user(user);
        let Ok(raw) = complete_partial(&spec.language, &system, &user, "script edit", true).await
        else {
            continue;
        };
        let (spoken, _) = crate::parse::split_slide_reply(&raw);
        let Ok(mut edited) = lines_of(spec, &spoken) else {
            continue;
        };
        if i > 0 {
            drop_preamble(&mut edited);
        }
        if !keeps_the_part(&scripts[i], &edited) {
            eprintln!(
                "opennotebook prep: edit of part {} not taken; the draft stands",
                i + 1
            );
            continue;
        }
        fit_to(&mut edited, slide_total(spec, i, n));
        scripts[i] = edited;
    }
}

/// Whether an edited part is still the part: not emptied, not a monologue where
/// there was a conversation, and not cut by more than two fifths. Cutting is
/// what the edit is FOR — a repeat removed is a shorter part — so the bound is
/// loose; it is there to catch an editor that returned a summary.
fn keeps_the_part(
    draft: &[crate::parse::ScriptedLine],
    edited: &[crate::parse::ScriptedLine],
) -> bool {
    let len =
        |l: &[crate::parse::ScriptedLine]| l.iter().map(|x| x.text.chars().count()).sum::<usize>();
    let voices = |l: &[crate::parse::ScriptedLine]| {
        let mut v: Vec<&str> = l.iter().map(|x| x.speaker_id.0.as_str()).collect();
        v.sort_unstable();
        v.dedup();
        v.len()
    };
    !edited.is_empty()
        && len(edited) * 5 >= len(draft) * 3
        && voices(edited) >= voices(draft).min(2)
}

/// A snake_case slide name, safe as a file name, so the name this slice
/// chooses is the name the deck ends up with rather than a placeholder to
/// reconcile later.
fn slug(topic: &str, ordinal: usize) -> String {
    let body: String = topic
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();
    let body = body
        .split('_')
        .filter(|s| !s.is_empty())
        .take(5)
        .collect::<Vec<_>>()
        .join("_");
    if body.is_empty() {
        format!("slide_{ordinal}")
    } else {
        body
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_speaker_named_by_display_name_is_that_speaker() {
        let sp = |id: &str, name: &str| opennotebook_session::Speaker {
            speaker_id: SpeakerId(id.into()),
            voice_id: String::new(),
            display_name: name.into(),
            role: String::new(),
        };
        let spec = ScriptSpec::new(
            "t",
            vec![sp("host", "Bella"), sp("expert", "Adam")],
            ("c", "p"),
        );
        let raw = "Bella: Webb sees in infrared.\n**Adam**: Which goes through dust.\nhost: And far back in time.";
        let lines = lines_of(&spec, raw).unwrap();
        let who: Vec<&str> = lines.iter().map(|l| l.speaker_id.0.as_str()).collect();
        assert_eq!(who, ["host", "expert", "host"]);
        assert_eq!(lines[1].text, "Which goes through dust.");
    }

    use super::*;

    fn line(id: &str, text: &str) -> crate::parse::ScriptedLine {
        crate::parse::ScriptedLine {
            speaker_id: SpeakerId(id.into()),
            text: text.into(),
        }
    }

    #[test]
    fn a_part_is_cut_where_it_stops_fitting_and_never_ends_on_a_question() {
        let mut lines = vec![
            line(
                "adam",
                "Moshi treats dialogue as speech-to-speech generation.",
            ),
            line("bella", "Wait, so there's no transcription step at all?"),
            line(
                "adam",
                "None in front of the reply. Text comes along as an inner monologue, \
                          predicted just ahead of the audio it describes, which keeps the words \
                          coherent.",
            ),
            line("bella", "And that is all?"),
        ];
        // Room for the first two lines and not the answer: the question goes too.
        fit_to(&mut lines, 120);
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert!(lines[0].text.starts_with("Moshi treats"));
    }

    #[test]
    fn a_speaker_saying_the_same_thing_twice_says_it_once() {
        // Slide 3 of a real build on the default script model.
        let mut lines = vec![
            line(
                "bella",
                "How does Moshi actually improve real-time dialogue then?",
            ),
            line(
                "adam",
                "Moshi's design is full-duplex and real-time. It removes text-based \
                          delays and handles overlapping speech without speaker turns.",
            ),
            line(
                "adam",
                "Moshi's full-duplex design means it can listen and speak \
                          simultaneously, drastically reducing latency. Unlike traditional \
                          systems, it skips text-to-speech conversion, lowering delays.",
            ),
            line("bella", "So the latency is what you feel first."),
            line(
                "adam",
                "Exactly, and the codec is what makes that possible.",
            ),
        ];
        drop_restatements(&mut lines);
        assert_eq!(lines.len(), 4, "{lines:?}");
        assert!(
            lines[1]
                .text
                .starts_with("Moshi's full-duplex design means")
        );
        // Different speakers, or different content, are left alone.
        assert_eq!(lines[3].speaker_id.0, "adam");
    }

    #[test]
    fn a_line_after_a_cut_is_not_kept_out_of_order() {
        let mut lines = vec![
            line(
                "adam",
                "A first sentence that fits. A second sentence that is far too long \
                          to be said inside what is left of this budget.",
            ),
            line("bella", "Right."),
        ];
        fit_to(&mut lines, 60);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "A first sentence that fits.");
    }

    #[test]
    fn a_slide_keeps_no_more_images_than_it_may_have() {
        let elements = vec![
            "[image: a kernel scheduler] — right half".to_string(),
            "Point: processes share the CPU".to_string(),
            "[image: a run queue] — left".to_string(),
        ];
        let lines = vec![line("host", "The scheduler decides who runs next.")];
        let one = shape(
            lines.clone(),
            "Layout: text left, image right",
            elements.clone(),
            650,
            Some(1),
        );
        assert_eq!(
            one.on_slide
                .iter()
                .filter(|e| e.starts_with("[image:"))
                .count(),
            1
        );
        assert!(one.on_slide.iter().any(|e| e.starts_with("Point:")));
        let none = shape(lines.clone(), "", elements.clone(), 650, Some(0));
        assert!(!none.on_slide.iter().any(|e| e.starts_with("[image:")));
        let open = shape(lines, "", elements, 650, None);
        assert_eq!(
            open.on_slide
                .iter()
                .filter(|e| e.starts_with("[image:"))
                .count(),
            2
        );
    }

    #[test]
    fn a_stat_is_the_same_when_its_number_is() {
        assert!(same_stat(
            "Stat: 512 — maximum entries in the task vector",
            "Stat: 512 — max task_struct entries"
        ));
        assert!(!same_stat("Stat: 512 — entries", "Stat: 32 — signals"));
        assert!(!same_stat("Stat:  — nothing", "Stat:  — nothing"));
    }

    #[test]
    fn the_prompt_offers_no_image_line_when_a_slide_may_have_none() {
        let mut spec = ScriptSpec::new("t", Vec::new(), ("c", "p"));
        assert!(spec.image_element().contains("[image:"));
        spec.images_per_slide = Some(0);
        assert!(spec.image_element().is_empty());
        spec.images_per_slide = Some(1);
        assert!(spec.image_element().contains("at most 1"));
    }

    /// The slide-4 narration of a real run: a greeting and a promise, then
    /// nothing. Neither survives; the material does.
    #[test]
    fn a_middle_slide_loses_its_welcome() {
        let mut lines = vec![
            line("bella", "Welcome to our session on Linux processes."),
            line(
                "bella",
                "Here's what you'll learn about task_struct data today.",
            ),
            line("adam", "Every process is described by a task_struct."),
        ];
        drop_preamble(&mut lines);
        assert_eq!(lines.len(), 1);
        assert_eq!(
            lines[0].text,
            "Every process is described by a task_struct."
        );
    }

    #[test]
    fn a_slide_of_nothing_but_greetings_is_kept_rather_than_emptied() {
        let mut lines = vec![line("bella", "Welcome, everyone.")];
        drop_preamble(&mut lines);
        assert_eq!(lines.len(), 1);
    }

    #[test]
    fn a_slide_knows_where_it_is_and_what_came_before() {
        let plan = vec![
            Planned {
                title: "Scheduling".into(),
                points: vec!["each CPU has its own run queue".into()],
            },
            Planned {
                title: "task_struct".into(),
                points: Vec::new(),
            },
        ];
        let place = Place {
            ordinal: 1,
            plan: &plan,
            before: vec!["adam: Each CPU runs its own scheduler.".into()],
            shown: vec!["Stat: 512 — task vector entries".into()],
        };
        let d = place.describe();
        assert!(d.contains("not to be shown again"));
        assert!(d.contains("Stat: 512"));
        assert!(d.contains("slide 2 of 2"));
        assert!(d.contains("2. task_struct  <- this slide"));
        assert!(d.contains("adam: Each CPU runs its own scheduler."));
        assert!(d.contains("last slide"));
    }

    fn audio(format: opennotebook_session::AudioFormat, focus: &str) -> ScriptSpec {
        let mut spec = ScriptSpec::new("t", Vec::new(), ("c", "p"));
        spec.audio = Some(opennotebook_session::AudioSpec {
            format,
            length: opennotebook_session::AudioLength::Default,
            focus: focus.into(),
        });
        spec
    }

    #[test]
    fn an_audio_prompt_has_no_slide_and_says_its_format() {
        use opennotebook_session::AudioFormat as F;
        let one = "You write the spoken narration for one slide of an explanatory session.\n\
                   Explain THIS slide's points.\n\nThen write the line `SLIDE:` on its own, and \
                   after it the copy that appears ON the slide.";
        let whole = "The session is shown as 5 slides, one per part.\n\
                     Then the line `SLIDE:` on its own, and after it the copy. Layout. Point. \
                     No sentences, no colours or fonts.\n\nSay only what the material supports.";
        let spec = audio(F::Debate, " the latency claims ");
        let a = spec.adapt(one.to_string());
        assert!(
            !a.contains("SLIDE:") && !a.to_lowercase().contains("slide's"),
            "{a}"
        );
        assert!(
            a.contains("one chapter of an audio overview, a Debate episode"),
            "{a}"
        );
        assert!(a.contains("Format: Debate") && a.contains("Never say \"deep dive\""));
        assert!(a.contains("\"the latency claims\""));
        let w = spec.adapt(whole.to_string());
        assert!(!w.contains("SLIDE:") && !w.contains("Layout"), "{w}");
        assert!(
            w.contains("is heard as 5 chapters") && w.contains("Say only what"),
            "{w}"
        );
        // A slide session is not touched.
        let plain = ScriptSpec::new("t", Vec::new(), ("c", "p"));
        assert_eq!(plain.adapt(one.to_string()), one);
        assert_eq!(plain.adapt_user(whole.to_string()), whole);
        assert_eq!(outline_shape(&plain), "");
        assert!(outline_shape(&audio(F::Critique, "")).contains("weakness"));
    }

    #[test]
    fn every_format_has_its_own_rules() {
        use opennotebook_session::{AudioFormat as F, AudioLength as L, AudioSpec};
        for f in F::ALL {
            let spec = AudioSpec {
                format: f,
                length: L::Default,
                focus: String::new(),
            };
            assert!(format_rules(&spec).contains(&format!("Format: {}", f.label())));
        }
        // A length the format does not offer becomes Default; Brief is short.
        let longer_debate = AudioSpec {
            format: F::Debate,
            length: L::Longer,
            focus: String::new(),
        };
        assert_eq!(longer_debate.length(), L::Default);
        assert_eq!(
            AudioSpec {
                format: F::Brief,
                length: L::Longer,
                focus: String::new()
            }
            .minutes(),
            2
        );
        assert_eq!(F::Brief.speakers(), 1);
        assert_eq!(F::parse("nonsense"), F::DeepDive);
    }

    #[test]
    fn decoration_in_a_plan_is_not_a_part() {
        let raw = "Debate: Moshi\n\nPART ONE\nThe problem\n- latency\nThe answer\n- speech in, speech out\n";
        let plan = parse_plan(raw, 3);
        let titles: Vec<&str> = plan.iter().map(|p| p.title.as_str()).collect();
        assert_eq!(titles, ["The problem", "The answer"]);
        assert!(is_label("PART ONE") && is_label("Section 3") && !is_label("Part of speech"));
        let long = "The problem Moshi solves and why text might not be the interface we need";
        assert_eq!(
            fit_title(long),
            "The problem Moshi solves and why text might not"
        );
    }

    #[test]
    fn a_debate_has_sides_and_a_critique_has_a_reviewer() {
        use opennotebook_session::{AudioFormat as F, AudioLength, AudioSpec};
        let sp = |id: &str, name: &str| opennotebook_session::Speaker {
            speaker_id: SpeakerId(id.into()),
            voice_id: String::new(),
            display_name: name.into(),
            role: String::new(),
        };
        let mut spec = ScriptSpec::new(
            "t",
            vec![sp("bella", "Bella"), sp("adam", "Adam")],
            ("c", "p"),
        );
        for (f, want, not) in [
            (
                F::Debate,
                "Bella argues one side",
                "listener's stand-in: sharp",
            ),
            (
                F::Critique,
                "Bella is the reviewer",
                "listener's stand-in: sharp",
            ),
            (F::DeepDive, "listener's stand-in: sharp", "argues one side"),
        ] {
            spec.audio = Some(AudioSpec {
                format: f,
                length: AudioLength::Default,
                focus: String::new(),
            });
            let d = dialogue(&spec);
            assert!(d.contains(want) && !d.contains(not), "{f:?}: {d}");
        }
    }

    #[test]
    fn a_plan_gives_each_part_its_own_points() {
        let raw = "**Slide 1: Why spoken dialogue is hard**\n\
                   - Pipelines of ASR, LLM and TTS add seconds of latency\n\
                   - Text loses tone and emotion\n\
                   2. Moshi's answer\n\
                   * Speech in, speech out, one model\n\
                   * 200 ms in practice\n\
                   Inner Monologue\n\
                   Extra part beyond the count\n";
        let plan = parse_plan(raw, 3);
        assert_eq!(plan.len(), 3);
        assert_eq!(plan[0].title, "Why spoken dialogue is hard");
        assert_eq!(plan[0].points.len(), 2);
        assert_eq!(plan[1].title, "Moshi's answer");
        assert_eq!(plan[1].points[1], "200 ms in practice");
        assert!(plan[2].points.is_empty(), "a bare title is still a part");
        assert!(
            plan[1].query().contains("200 ms"),
            "retrieval follows the points"
        );
        let listing = plan_listing(&plan, Some(1));
        assert!(listing.contains("2. Moshi's answer  <- this slide"));
        assert!(listing.contains("- Text loses tone and emotion"));
    }

    #[test]
    fn two_speakers_are_a_host_and_an_explainer_and_one_speaks_to_you() {
        let sp = |id: &str, name: &str| opennotebook_session::Speaker {
            speaker_id: SpeakerId(id.into()),
            voice_id: String::new(),
            display_name: name.into(),
            role: String::new(),
        };
        let mut spec = ScriptSpec::new(
            "t",
            vec![sp("bella", "Bella"), sp("adam", "Adam")],
            ("c", "p"),
        );
        let two = dialogue(&spec);
        assert!(two.contains("Bella leads"));
        assert!(two.contains("Adam explains"));
        assert!(two.contains("answered in the very next line"));
        spec.speakers.truncate(1);
        assert!(dialogue(&spec).contains("\"you\""));
    }

    #[test]
    fn an_edit_that_guts_a_part_is_not_taken() {
        let draft = vec![
            line("bella", "So the model hears and speaks at the same time?"),
            line(
                "adam",
                "Yes, it models both streams in parallel, which is how it handles overlap.",
            ),
        ];
        assert!(keeps_the_part(&draft, &draft));
        assert!(
            !keeps_the_part(&draft, &draft[1..]),
            "a monologue where there was a conversation"
        );
        assert!(!keeps_the_part(&draft, &[line("adam", "Yes.")]));
        assert!(!keeps_the_part(&draft, &[]));
    }

    #[test]
    fn a_whole_session_reply_is_cut_at_its_parts() {
        let raw = "Sure, here is the script.\n\
                   === PART 1 ===\n\
                   bella: Welcome.\n\
                   SLIDE:\n\
                   Layout: text only\n\
                   Point: one\n\
                   ## Part 2 — Scheduling\n\
                   adam: And each CPU has its own queue.\n\
                   **Slide 3**\n\
                   bella: Which brings us to task_struct.\n";
        let parts = split_parts(raw, 4);
        assert_eq!(parts.len(), 4);
        let one = parts[0].as_deref().unwrap();
        assert!(one.starts_with("bella: Welcome."));
        assert!(
            one.contains("SLIDE:"),
            "the copy marker stays inside its part"
        );
        assert_eq!(
            parts[1].as_deref(),
            Some("adam: And each CPU has its own queue.\n")
        );
        assert!(parts[2].is_some());
        assert!(
            parts[3].is_none(),
            "a part never written is left to the fallback"
        );
    }

    #[test]
    fn speech_after_the_slide_copy_is_still_speech() {
        let ids = [SpeakerId("host".into()), SpeakerId("expert".into())];
        let part = "host: Welcome.\nSLIDE:\nLayout: text only\nPoint: states\nhost: Processes change state.\n";
        let (spoken, elements) = crate::parse::split_slide_reply(&speech_first(part, &ids));
        let lines = parse_lines(&spoken, &ids).unwrap();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[1].text, "Processes change state.");
        assert_eq!(elements.len(), 1);
    }

    #[test]
    fn an_outline_title_loses_its_slide_label() {
        assert_eq!(
            strip_slide_label("Slide 1: Process Isolation"),
            "Process Isolation"
        );
        assert_eq!(strip_slide_label("2. The task_struct"), "The task_struct");
        assert_eq!(
            strip_slide_label("Symmetric multiprocessing"),
            "Symmetric multiprocessing"
        );
        assert_eq!(
            strip_slide_label("64-bit address spaces"),
            "64-bit address spaces"
        );
        assert_eq!(strip_slide_label("3) Zombies"), "Zombies");
        assert_eq!(
            strip_slide_label("Part 1: The Foundation of Moshi"),
            "The Foundation of Moshi"
        );
        assert_eq!(strip_slide_label("Chapter 2 - Mimi"), "Mimi");
        assert_eq!(strip_slide_label("Particle physics"), "Particle physics");
    }

    #[test]
    fn the_copy_marker_is_not_a_part_marker() {
        assert_eq!(part_marker("SLIDE:"), None);
        assert_eq!(part_marker("Slide: Layout: text only"), None);
        assert_eq!(part_marker("=== PART 3 ==="), Some(3));
        assert_eq!(part_marker("PART 12:"), Some(12));
    }

    #[test]
    fn a_closing_line_on_the_last_slide_is_kept() {
        let mut lines = vec![
            line("bella", "task_struct holds a process's state."),
            line("bella", "Today, we covered scheduling and task_struct."),
        ];
        drop_preamble(&mut lines);
        assert_eq!(lines.len(), 2);
    }

    #[test]
    fn a_slug_is_a_safe_snake_case_name() {
        assert_eq!(
            slug("Each Slide Has Spoken Words", 0),
            "each_slide_has_spoken_words"
        );
        assert_eq!(slug("Who holds the floor?", 1), "who_holds_the_floor");
        assert_eq!(
            slug("!!!", 2),
            "slide_2",
            "a title with no word characters still names a slide"
        );
    }
}
