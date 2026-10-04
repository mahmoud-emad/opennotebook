//! Parsing the model's script output.
//!
//! The wire format is one line per spoken line, `speaker_id: text`. Chosen over
//! JSON because a malformed line is one bad line rather than an unparseable
//! document, and because a spoken line has no structure worth nesting.

use opennotebook_session::SpeakerId;

use crate::error::ScriptError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScriptedLine {
    pub speaker_id: SpeakerId,
    pub text: String,
}

/// Parse `speaker: text` lines, keeping only speakers the session declares.
///
/// An unknown speaker is an error rather than a dropped line. Dropping would
/// leave a session whose narration silently lost a turn, and the store's own
/// `dangling_speaker_ids` check would never see it because the line would not
/// be there to check.
///
/// A tag that is a known id wearing punctuation, capitals or the word "id" is
/// resolved rather than refused — see [`resolve`]. That is not leniency about
/// who is speaking: it is the same speaker, spelled the way a small model spells
/// things.
pub fn parse_lines(raw: &str, known: &[SpeakerId]) -> Result<Vec<ScriptedLine>, ScriptError> {
    let mut out = Vec::new();
    for (n, raw_line) in raw.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((speaker, text)) = line.split_once(':') else {
            continue;
        };
        let speaker = speaker
            .trim()
            .trim_start_matches(['-', '*', ' '])
            .to_string();
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        let Some(id) = resolve(&speaker, known) else {
            return Err(ScriptError::UnknownSpeaker {
                line: n + 1,
                speaker,
            });
        };
        out.push(ScriptedLine {
            speaker_id: id,
            text: text.to_string(),
        });
    }
    Ok(out)
}

/// Which declared speaker a tag names, if any.
///
/// Exact first. Then the same id under punctuation and capitals, then the same
/// id with the word "id" stuck on the end — `**Host**` and `host_id` are both
/// `host`. That last one is not hypothetical: with one speaker called `host`,
/// `amazon/nova-micro-v1` read the format line "lines of the form
/// `speaker_id: what they say`" and tagged every line `host_id`, which failed a
/// prep five minutes in over a name the model had got right. The prompt no
/// longer offers it that word, and this resolves it if a model finds another
/// way to the same mistake.
///
/// What it deliberately does NOT do is guess. A tag naming somebody who is not
/// in this session — `narrator` where the speakers are `host` and `expert` — is
/// still an error, because that is a line whose speaker is genuinely unknown.
fn resolve(tag: &str, known: &[SpeakerId]) -> Option<SpeakerId> {
    let exact = SpeakerId(tag.to_string());
    if known.contains(&exact) {
        return Some(exact);
    }
    let norm = |s: &str| -> String {
        s.chars()
            .filter(|c| c.is_ascii_alphanumeric())
            .map(|c| c.to_ascii_lowercase())
            .collect()
    };
    let t = norm(tag);
    if t.is_empty() {
        return None;
    }
    let stripped = t.strip_suffix("id").filter(|s| !s.is_empty());
    if let Some(found) = known.iter().find(|k| {
        let n = norm(&k.0);
        !n.is_empty() && (n == t || Some(n.as_str()) == stripped)
    }) {
        return Some(found.clone());
    }

    // A label that names a speaker by POSITION rather than by id.
    //
    // The prompt shows the real ids and asks for them, and the script model is
    // deliberately small, so it substitutes its own conventions: `A:`/`B:` cost
    // a real prep with "line 3: `A` is not a speaker of this session". `1:`,
    // `Speaker 1:` and `S1:` are the same idea wearing different clothes.
    //
    // The intent is unambiguous — first speaker, second speaker — so it is
    // honoured rather than refused. This is NOT a loosening of the rule that an
    // unknown speaker is an error: a tag that names nobody still is. It only
    // says that "the first one" is a way of naming somebody.
    let ordinal = t
        .strip_prefix("speaker")
        .or_else(|| t.strip_prefix('s'))
        .unwrap_or(&t);
    if let Ok(n) = ordinal.parse::<usize>()
        && n >= 1
    {
        return known.get(n - 1).cloned();
    }
    // A single letter: a, b, c … taken as first, second, third.
    if t.len() == 1
        && let Some(c) = t.chars().next()
        && c.is_ascii_lowercase()
    {
        return known.get((c as u8 - b'a') as usize).cloned();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn known() -> Vec<SpeakerId> {
        vec![SpeakerId("host".into()), SpeakerId("expert".into())]
    }

    #[test]
    fn two_speakers_alternating_parse_in_order() {
        let raw = "host: What is a narrated session?\nexpert: Slides with a spoken script.\n";
        let lines = parse_lines(raw, &known()).unwrap();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].speaker_id, SpeakerId("host".into()));
        assert_eq!(lines[1].text, "Slides with a spoken script.");
    }

    /// A one-speaker session goes through the same parser with a one-element
    /// speaker set. No branch, no special case.
    #[test]
    fn one_speaker_uses_the_same_parser() {
        let lines = parse_lines(
            "narrator: A single voice.\n",
            &[SpeakerId("narrator".into())],
        )
        .unwrap();
        assert_eq!(lines.len(), 1);
    }

    #[test]
    fn blank_lines_and_comments_are_skipped_but_a_stray_speaker_is_not() {
        let lines = parse_lines("# a heading\n\nhost: Real.\n", &known()).unwrap();
        assert_eq!(lines.len(), 1);

        let err = parse_lines("narrator: Not in this session.\n", &known()).unwrap_err();
        assert!(
            matches!(err, ScriptError::UnknownSpeaker { line: 1, .. }),
            "got {err}"
        );
    }

    /// The failure that killed a real prep: one speaker named `host`, every line
    /// tagged `host_id`, five minutes of work thrown away over a spelling.
    #[test]
    fn a_known_speaker_wearing_punctuation_or_the_word_id_is_still_that_speaker() {
        let one = [SpeakerId("host".into())];
        for raw in [
            "host_id: A single voice.\n",
            "**host**: A single voice.\n",
            "Host: A single voice.\n",
            "host id: A single voice.\n",
        ] {
            let lines = parse_lines(raw, &one).unwrap_or_else(|e| panic!("{raw:?}: {e}"));
            assert_eq!(lines[0].speaker_id, SpeakerId("host".into()), "{raw:?}");
            assert_eq!(lines[0].text, "A single voice.");
        }
        // Still an error, because this one really is somebody else.
        assert!(parse_lines("expert: Not here.\n", &one).is_err());
    }

    /// Colons inside the spoken text must not split it.
    #[test]
    fn only_the_first_colon_separates() {
        let lines =
            parse_lines("host: The rule is this: read the discriminator.", &known()).unwrap();
        assert_eq!(lines[0].text, "The rule is this: read the discriminator.");
    }
}

/// The allowed `Layout:` values, kept verbatim from the slide spec of an
/// earlier slide service.
///
/// A layout it does not know is not a layout: the renderer picks the composition
/// from this closed set, so an invented name degrades to whatever it guesses.
/// Validated here rather than trusted, because the model supplies it.
pub const LAYOUTS: &[&str] = &[
    "centered title",
    "left text / right image",
    "right text / left image",
    "two columns",
    "full-bleed image with overlay text",
    "stat + supporting text",
    "quote centered",
    "text only",
];

/// The element type labels a slide line may carry, plus the image slot.
const ELEMENT_LABELS: &[&str] = &["Subhead", "Point", "Stat", "Quote", "Caption"];

/// Split a slide reply into its spoken half and its on-slide half.
///
/// The model is asked for two blocks in one call: narration lines, then a
/// `SLIDE:` marker, then the slide's own copy. One call, because the two are
/// written from the same retrieved material and a second call would double the
/// per-slide cost to say the same thing twice.
///
/// Returns `(spoken, on_slide)`. Everything before the marker is spoken; the
/// element lines after it are kept only if they are shaped like elements, so a
/// model that ignores the format costs the slide's copy and not its narration.
pub fn split_slide_reply(raw: &str) -> (String, Vec<String>) {
    let mut spoken = String::new();
    let mut elements = Vec::new();
    let mut in_slide = false;

    for line in raw.lines() {
        let t = line.trim();
        if !in_slide {
            if let Some(rest) = marker_rest(t) {
                in_slide = true;
                // `SLIDE: Layout: text only` on one line. The remainder is the
                // first element rather than something to drop, and the marker
                // must still be consumed — left in the spoken half it parses as
                // a line by a speaker called `SLIDE`, which failed a real prep
                // with "`SLIDE` is not a speaker of this session".
                if !rest.is_empty()
                    && let Some(e) = element_line(rest)
                {
                    elements.push(e);
                }
                continue;
            }
            // Slide copy that arrived before the marker, or with no marker at
            // all. It is routed to the elements rather than left in the spoken
            // half, because `parse_lines` treats an unknown prefix as a fatal
            // unknown speaker — so one stray `Point:` would fail the slide.
            // These labels cannot collide with a speaker id: ids come from the
            // session roster and are matched against it.
            if let Some(e) = element_line(t) {
                elements.push(e);
                continue;
            }
            if is_layout_line(t) {
                continue;
            }
            spoken.push_str(line);
            spoken.push('\n');
            continue;
        }
        if t.is_empty() {
            continue;
        }
        if let Some(e) = element_line(t) {
            elements.push(e);
        }
    }
    (spoken, elements)
}

/// Whether `t` is a `Layout:` line.
///
/// Dropped from the spoken half wherever it appears. [`layout_of`] reads it off
/// the whole reply, so nothing is lost, and leaving it in would fail the slide
/// as a line by a speaker called `Layout`.
fn is_layout_line(t: &str) -> bool {
    t.trim_matches(|c: char| c == '-' || c == '*' || c == '#' || c == ' ')
        .split_once(':')
        .is_some_and(|(k, _)| k.trim().eq_ignore_ascii_case("layout"))
}

/// `Some(rest)` when `t` is the slide marker, where `rest` is anything that
/// followed it on the same line.
///
/// Written to accept what models actually emit rather than what the prompt asks
/// for: `SLIDE:`, `slide`, `**SLIDE:**`, `## SLIDE`, `- SLIDE:`, `ON SLIDE:`,
/// and the marker with its first element trailing behind it. Anything looser
/// would swallow narration, so the word itself must be the whole key.
fn marker_rest(t: &str) -> Option<&str> {
    let trimmed = t.trim_matches(|c: char| {
        c == '-' || c == '*' || c == '#' || c == '_' || c == ' ' || c == '\t'
    });
    let (key, rest) = match trimmed.split_once(':') {
        Some((k, r)) => (k, r.trim()),
        None => (trimmed, ""),
    };
    let key = key
        .trim_matches(|c: char| c == '*' || c == '_' || c == '#' || c == ' ')
        .trim();
    if key.eq_ignore_ascii_case("slide") || key.eq_ignore_ascii_case("on slide") {
        Some(rest)
    } else {
        None
    }
}

/// One element line, normalised, or `None` when it is not one.
///
/// Leading bullets are stripped because the spec's own examples carry them and a
/// model copies what it sees; the label is matched case-insensitively and
/// re-emitted in the spec's own casing so the renderer's parser sees exactly
/// what its documentation describes.
fn element_line(t: &str) -> Option<String> {
    let body = t.trim_start_matches(['-', '*', ' ']).trim();
    if body.starts_with("[image:") {
        return Some(body.to_string());
    }
    let (label, rest) = body.split_once(':')?;
    let label = label.trim();
    let rest = rest.trim();
    if rest.is_empty() {
        return None;
    }
    let canonical = ELEMENT_LABELS
        .iter()
        .find(|l| l.eq_ignore_ascii_case(label))?;
    Some(format!("{canonical}: {rest}"))
}

/// The layout the model asked for, or `None` when it named one that is not real.
pub fn layout_of(raw: &str) -> Option<String> {
    for line in raw.lines() {
        let t = line.trim().trim_start_matches(['-', '*', ' ']).trim();
        let Some((k, v)) = t.split_once(':') else {
            continue;
        };
        if !k.trim().eq_ignore_ascii_case("layout") {
            continue;
        }
        let want = v.trim().to_ascii_lowercase();
        if let Some(l) = LAYOUTS.iter().find(|l| **l == want) {
            return Some((*l).to_string());
        }
    }
    None
}

#[cfg(test)]
mod slide_spec_tests {
    use super::*;

    const REPLY: &str = "host: Attention lets a token look at every other token.\n\
        expert: And the weights are learned, not fixed.\n\
        SLIDE:\n\
        Layout: left text / right image\n\
        - Point: query, key, value\n\
        - Subhead: learned, not fixed\n\
        - [image: three vectors meeting at a dot product] — right half\n";

    /// The two halves are separated, and the marker itself is in neither.
    #[test]
    fn the_spoken_half_and_the_printed_half_come_apart() {
        let (spoken, elements) = split_slide_reply(REPLY);
        assert!(spoken.contains("host: Attention lets"), "{spoken}");
        assert!(spoken.contains("expert: And the weights"), "{spoken}");
        assert!(!spoken.contains("SLIDE"), "the marker leaked: {spoken}");
        assert!(!spoken.contains("Point:"), "slide copy leaked: {spoken}");
        assert_eq!(elements.len(), 3, "{elements:?}");
        assert_eq!(elements[0], "Point: query, key, value");
        assert!(elements[2].starts_with("[image:"), "{elements:?}");
    }

    /// `Layout:` is not an element — it is the one composition line, read
    /// separately, so it must not also arrive as copy to print.
    #[test]
    fn the_layout_line_is_not_an_element() {
        let (_, elements) = split_slide_reply(REPLY);
        assert!(
            !elements.iter().any(|e| e.starts_with("Layout")),
            "{elements:?}"
        );
        assert_eq!(layout_of(REPLY).as_deref(), Some("left text / right image"));
    }

    /// A composition outside the closed set is refused, because the renderer
    /// would otherwise guess the layout as well as the copy.
    #[test]
    fn an_invented_layout_is_refused() {
        assert_eq!(
            layout_of("SLIDE:\nLayout: dramatic diagonal swoosh\n"),
            None
        );
        // Case and spacing are the model's, not the spec's.
        assert_eq!(
            layout_of("Layout:   TWO COLUMNS  \n").as_deref(),
            Some("two columns")
        );
    }

    /// A line that is not a known element is dropped rather than printed.
    /// `Visual direction: something painterly` is prose the spec forbids.
    #[test]
    fn an_unknown_element_label_is_dropped() {
        let (_, elements) =
            split_slide_reply("SLIDE:\nVisual direction: something painterly\n- Point: kept\n");
        assert_eq!(elements, vec!["Point: kept"]);
    }

    /// No marker at all: everything is narration and the slide gets no copy.
    /// The narration is the half that must survive a model ignoring the format.
    #[test]
    fn a_reply_with_no_marker_is_all_narration() {
        let (spoken, elements) = split_slide_reply("host: just talking\n");
        assert!(spoken.contains("host: just talking"));
        assert!(elements.is_empty());
    }
}

#[cfg(test)]
mod marker_tests {
    use super::*;

    /// Every punctuation a model has been seen to wrap the marker in. A marker
    /// that is not recognised stays in the spoken half and is then parsed as a
    /// speaker line — the exact failure a real prep hit: "`SLIDE` is not a
    /// speaker of this session".
    #[test]
    fn the_marker_is_recognised_however_it_is_dressed() {
        for m in [
            "SLIDE:",
            "slide",
            "**SLIDE:**",
            "## SLIDE",
            "- SLIDE:",
            "ON SLIDE:",
            "  slide:  ",
        ] {
            let raw = format!("host: spoken\n{m}\nLayout: two columns\n- Point: kept\n");
            let (spoken, elements) = split_slide_reply(&raw);
            assert!(
                !spoken.to_ascii_lowercase().contains("slide"),
                "marker {m:?} leaked into narration: {spoken:?}"
            );
            assert_eq!(elements, vec!["Point: kept"], "marker {m:?}");
        }
    }

    /// The marker with its first element on the same line keeps both.
    #[test]
    fn a_marker_carrying_its_first_element_keeps_it() {
        let (spoken, elements) = split_slide_reply("host: spoken\nSLIDE: Point: inline\n");
        assert!(spoken.contains("host: spoken"));
        assert_eq!(elements, vec!["Point: inline"]);
    }

    /// A speaker whose narration merely mentions slides is not a marker.
    #[test]
    fn narration_about_slides_is_not_the_marker() {
        let (spoken, elements) =
            split_slide_reply("host: the next slide shows the encoder\nSLIDE:\n- Point: k\n");
        assert!(spoken.contains("the next slide shows"), "{spoken}");
        assert_eq!(elements, vec!["Point: k"]);
    }
}

#[cfg(test)]
mod stray_copy_tests {
    use super::*;

    /// Slide copy with no marker in front of it must not fail the slide.
    ///
    /// `parse_lines` treats an unrecognised prefix as a fatal unknown speaker,
    /// so a model that forgets the marker used to lose the whole slide rather
    /// than just its copy. The narration is the half that has to survive.
    #[test]
    fn stray_copy_never_reaches_the_speaker_parser() {
        let (spoken, elements) = split_slide_reply(
            "host: spoken line\nLayout: two columns\n- Point: stray\nhost: another\n",
        );
        assert!(!spoken.contains("Point:"), "{spoken}");
        assert!(!spoken.contains("Layout:"), "{spoken}");
        assert!(spoken.contains("host: spoken line"), "{spoken}");
        assert!(spoken.contains("host: another"), "{spoken}");
        assert_eq!(elements, vec!["Point: stray"]);
    }

    /// Narration whose text happens to contain a colon is still narration.
    #[test]
    fn a_colon_inside_narration_is_not_an_element() {
        let (spoken, elements) =
            split_slide_reply("host: the rule is simple: attend to everything\n");
        assert!(spoken.contains("attend to everything"), "{spoken}");
        assert!(elements.is_empty(), "{elements:?}");
    }
}

#[cfg(test)]
mod narration_survival_tests {
    use super::*;

    /// The reply that broke a real prep: the model did the slide half and
    /// skipped the narration entirely. The split must report an empty spoken
    /// half so the caller can retry, rather than silently producing a slide
    /// with no voice.
    #[test]
    fn a_reply_with_only_slide_copy_leaves_no_narration() {
        let raw = "SLIDE: Listing running processes with ps\n\
                   Layout: centered title\n\
                   Point: ps command\n\
                   Subhead: snapshot of current processes\n";
        let (spoken, elements) = split_slide_reply(raw);
        assert!(
            spoken.trim().is_empty(),
            "spoken should be empty: {spoken:?}"
        );
        assert_eq!(elements.len(), 2, "{elements:?}");
        assert_eq!(layout_of(raw).as_deref(), Some("centered title"));
    }

    /// The observed good shape, from the same model: one narration line, then
    /// the marker carrying the slide title, then the block. The narration must
    /// come through intact and the title must not be mistaken for an element.
    #[test]
    fn the_models_usual_shape_keeps_its_narration() {
        let raw = "host: Let's explore the ps command for listing running processes.\n\n\
                   SLIDE: Listing running processes with ps\n\n\
                   Layout: centered title\n\n\
                   Point: ps command\n";
        let (spoken, elements) = split_slide_reply(raw);
        assert!(spoken.contains("host: Let's explore"), "{spoken:?}");
        assert_eq!(elements, vec!["Point: ps command"]);
    }
}

#[cfg(test)]
mod positional_speaker_tests {
    use super::*;

    fn ids() -> Vec<SpeakerId> {
        vec![SpeakerId("host".into()), SpeakerId("expert".into())]
    }

    /// The labels a real prep actually failed on, and their relatives. The model
    /// is told the ids; when it invents its own convention the intent is still
    /// plain, and a prep must not die over it.
    #[test]
    fn a_speaker_named_by_position_resolves() {
        for (tag, want) in [
            ("A", "host"),
            ("B", "expert"),
            ("a", "host"),
            ("b", "expert"),
            ("1", "host"),
            ("2", "expert"),
            ("Speaker 1", "host"),
            ("speaker2", "expert"),
            ("S1", "host"),
            ("s2", "expert"),
        ] {
            assert_eq!(
                resolve(tag, &ids()).map(|s| s.0),
                Some(want.to_string()),
                "tag {tag:?}"
            );
        }
    }

    /// Real ids still win, and are not reinterpreted as positions.
    #[test]
    fn the_real_ids_still_resolve_first() {
        assert_eq!(resolve("host", &ids()).map(|s| s.0), Some("host".into()));
        assert_eq!(
            resolve("expert", &ids()).map(|s| s.0),
            Some("expert".into())
        );
        assert_eq!(resolve("HOST", &ids()).map(|s| s.0), Some("host".into()));
    }

    /// A position nobody occupies is still nobody. A one-speaker session has no
    /// second voice, and inventing one would be worse than refusing.
    #[test]
    fn a_position_past_the_roster_is_still_unknown() {
        let one = vec![SpeakerId("host".into())];
        assert_eq!(resolve("B", &one), None);
        assert_eq!(resolve("3", &ids()), None);
    }

    /// A tag that names nobody is still an error. This is not a rule that
    /// everything resolves to somebody.
    #[test]
    fn a_meaningless_tag_still_resolves_to_nobody() {
        assert_eq!(resolve("Narrator", &ids()), None);
        assert_eq!(resolve("Note", &ids()), None);
    }
}
