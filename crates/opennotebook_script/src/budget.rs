//! Hard character budgets, enforced on what is emitted rather than requested.
//!
//! Section 3 requires these because an earlier slide service reported
//! `status: succeeded` and `ok: true` on a slide whose title overflowed its box, whose accent rule
//! struck through a word, and which still carried the literal placeholder
//! `SUBHEAD`. `ok` is a liveness signal, not a quality one, so the defence has
//! to be upstream, and this slice is the upstream.
//!
//! Asking a model for "about 60 characters" is a request, not a guarantee. The
//! budget is applied to the string on its way out.

/// A slide title. Short enough not to wrap in the 980 pixel box the measured
/// overflow came from.
pub const TITLE: usize = 60;

/// One narration line as spoken.
pub const LINE: usize = 320;

/// Total narration per slide. Kokoro was measured at 84 words in 23.22 s, so
/// about 3.6 words per second; section 3 budgets roughly 30 s of audio per
/// slide, which is about 108 words. At an English average near six characters
/// per word that is close to 650.
pub const SLIDE_NARRATION: usize = 650;

/// Characters of narration Kokoro speaks per second, measured on this box on
/// 2026-10-01 across eight built sessions: 7.2 minutes of audio at 17.2
/// characters a second, no session below 16.4. Rounded down, so a session asked
/// for N minutes comes out at N or a little over rather than short.
pub const CHARS_PER_SECOND: usize = 17;

/// The narration budget per slide for a session of `minutes` over `slides`.
///
/// The welcome and the close have their own budget ([`BOOKEND_NARRATION`]
/// each), so they are taken off first. Never below [`SLIDE_NARRATION`], which
/// is what a slide had before the length was a setting: a short session over
/// many slides keeps the depth a slide always had.
pub fn slide_narration(minutes: u32, slides: usize) -> usize {
    let total = minutes as usize * 60 * CHARS_PER_SECOND;
    let body = total.saturating_sub(2 * BOOKEND_NARRATION);
    (body / slides.max(1)).max(SLIDE_NARRATION)
}

/// A per-slide budget above this is written one slide per call.
///
/// The whole-session call asks one reply for every slide's narration. Past
/// about 1,500 characters a slide that reply runs toward the script model's
/// output ceiling, and a truncated reply loses whole slides to the per-slide
/// fallback anyway — so a long session goes straight there, where each call
/// writes one slide and was measured to reach a 4,800 character target.
pub const WHOLE_SESSION_MAX_SLIDE: usize = 1_500;

/// The least room worth giving a line.
///
/// A budget spent down to its last few characters used to emit the fragment
/// that fitted: a real run ended a slide on the spoken line "Moshi util". The
/// line is not shortened at that point, it is destroyed, and it is then
/// synthesised and played. Below this, the line is dropped instead — about a
/// second and a half of speech is the floor for something worth saying.
pub const MIN_LINE: usize = 40;

/// Total narration for a bookend — the welcome in front of the first slide, or
/// the close behind the last.
///
/// Its own budget rather than a share of the slide's, because a first slide
/// legitimately runs longer than a middle one: it carries a welcome as well as
/// its material. It had no budget at all until 2026-09-16, when two bookend
/// lines at `LINE` each could add 640 characters to a 650 character slide and
/// nothing counted them. That went unnoticed because the bookend was being
/// silently dropped for an unrelated reason, which is the same lesson as
/// everywhere else in this module: a limit nobody enforces is discovered by
/// whatever stops hiding it.
///
/// 320 is one `LINE`, about twelve seconds of Kokoro at the measured 3.6 words
/// a second. A welcome is a breath, not a preamble.
pub const BOOKEND_NARRATION: usize = 320;

/// How much of a budget the next line may have, or `None` when what is left is
/// too small to say anything in.
///
/// The whole point is the `None`. Spending a budget down to its last few
/// characters used to emit whatever fitted, and a real run ended a slide on the
/// spoken line "Moshi util" — not a shortened line, a destroyed one, synthesised
/// and played. This says "drop it" instead. It caps at [`LINE`] on the way past,
/// so a caller has one rule rather than two.
pub fn room(spent: usize, total: usize) -> Option<usize> {
    let left = total.saturating_sub(spent).min(LINE);
    (left >= MIN_LINE).then_some(left)
}

/// Trim to `max` at a word boundary, or return the string unchanged.
///
/// For text that is read, not spoken: titles and on-slide elements. Narration
/// goes through [`fit_spoken`], which never ends mid-sentence.
///
/// Truncation is visible in the result rather than silent: the caller compares
/// lengths to know it happened. There is no ellipsis, because this text is
/// spoken and an ellipsis is not a sound.
pub fn fit(text: &str, max: usize) -> String {
    let text = text.trim();
    if text.chars().count() <= max {
        return text.to_string();
    }
    let truncated: String = text.chars().take(max).collect();
    match truncated.rfind(char::is_whitespace) {
        Some(cut) if cut > max / 2 => truncated[..cut].trim_end().to_string(),
        _ => truncated.trim_end().to_string(),
    }
}

/// Trim a SPOKEN line to `max` at a sentence boundary, or return it unchanged.
///
/// Whole sentences, never a word boundary. Cutting at the last word that fits
/// produced a spoken line ending "…making this session an" — synthesised,
/// played, and heard as the narrator stopping mid-thought. A line that is too
/// long now loses its last sentences instead, so what is said is finished.
///
/// When not even the first sentence fits, the cut falls at the last clause
/// break (`,` `;` `:`) and becomes a full stop; failing that, the last word,
/// also closed with a full stop. One character is kept back for that stop, so
/// the budget holds either way.
pub fn fit_spoken(text: &str, max: usize) -> String {
    let text = text.trim();
    if text.chars().count() <= max || max < 2 {
        return fit(text, max);
    }
    let head: String = text.chars().take(max).collect();
    let chars: Vec<(usize, char)> = head.char_indices().collect();
    let mut sentence_end = None;
    let mut clause_end = None;
    for (k, &(i, c)) in chars.iter().enumerate() {
        let followed_by_space = chars.get(k + 1).is_some_and(|&(_, n)| n.is_whitespace());
        if !followed_by_space {
            continue;
        }
        match c {
            '.' | '!' | '?' => sentence_end = Some(i + c.len_utf8()),
            ',' | ';' | ':' => clause_end = Some(i),
            _ => {}
        }
    }
    if let Some(end) = sentence_end {
        return head[..end].trim_end().to_string();
    }
    // Room for the full stop that closes it.
    let head: String = text.chars().take(max - 1).collect();
    let cut = clause_end
        .filter(|&i| i > max / 3 && i < head.len())
        .or_else(|| head.rfind(char::is_whitespace).filter(|&i| i > max / 3))
        .unwrap_or(head.len());
    let body = head[..cut]
        .trim_end()
        .trim_end_matches([',', ';', ':', '-', '—'])
        .trim_end();
    format!("{body}.")
}

/// The whole sentences of a spoken line that fit in `max`, or `None` when not
/// even its first sentence does.
///
/// [`fit_spoken`] always returns something, and when no sentence fits it closes
/// a clause with a full stop it made up. That is how a real session said "How
/// does the 'Inner Monologue' method contribute to the." — a question with its
/// object cut off, said aloud and then never answered. In a conversation a line
/// that does not fit is better dropped than mangled, so the script uses this.
pub fn whole_sentences(text: &str, max: usize) -> Option<String> {
    let text = text.trim();
    if text.chars().count() <= max {
        return (!text.is_empty()).then(|| text.to_string());
    }
    let head: String = text.chars().take(max).collect();
    let chars: Vec<(usize, char)> = head.char_indices().collect();
    let mut end = None;
    for (k, &(i, c)) in chars.iter().enumerate() {
        if matches!(c, '.' | '!' | '?') && chars.get(k + 1).is_some_and(|&(_, n)| n.is_whitespace())
        {
            end = Some(i + c.len_utf8());
        }
    }
    end.map(|e| head[..e].trim_end().to_string())
}

/// True when `fit` would shorten this string. Lets a caller report a budget
/// breach rather than discover a short line later.
pub fn exceeds(text: &str, max: usize) -> bool {
    text.trim().chars().count() > max
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twenty_minutes_over_five_slides_is_about_four_thousand_characters_a_slide() {
        let per = slide_narration(20, 5);
        assert_eq!(
            per,
            (20 * 60 * CHARS_PER_SECOND - 2 * BOOKEND_NARRATION) / 5
        );
        // At the measured 17 characters a second the whole session is 20 minutes.
        let spoken = per * 5 + 2 * BOOKEND_NARRATION;
        assert!(
            (spoken / CHARS_PER_SECOND) as i64 - 20 * 60 >= -1,
            "{spoken}"
        );
        assert!(
            per > WHOLE_SESSION_MAX_SLIDE,
            "a 20 minute session is written per slide"
        );
    }

    #[test]
    fn a_short_session_keeps_the_depth_a_slide_always_had() {
        assert_eq!(slide_narration(2, 12), SLIDE_NARRATION);
        assert_eq!(slide_narration(3, 5), SLIDE_NARRATION);
        assert_eq!(slide_narration(5, 0), slide_narration(5, 1));
    }

    #[test]
    fn a_line_that_is_too_long_loses_whole_sentences_not_words() {
        // The line that was spoken as "...making this session an".
        let line = "Welcome, everyone. Today, we'll explore the Transformer architecture \
                    and how we measure it. Understanding these elements will empower you \
                    to optimize your own projects, making this session an essential guide.";
        let got = fit_spoken(line, 200);
        assert_eq!(
            got,
            "Welcome, everyone. Today, we'll explore the Transformer architecture and how we measure it."
        );
        // No sentence end inside the budget: a clause, closed as a sentence.
        let got = fit_spoken(
            "One very long opening clause about attention, and then a lot more words",
            60,
        );
        assert!(got.chars().count() <= 60);
        assert_eq!(got, "One very long opening clause about attention.");
        // Short enough: untouched.
        assert_eq!(fit_spoken("Short.", 50), "Short.");
    }

    #[test]
    fn a_line_with_no_whole_sentence_in_its_room_is_dropped_not_mangled() {
        // The line a real session ended a slide on, as "...contribute to the."
        let q = "How does the 'Inner Monologue' method contribute to the model's ability \
                 to handle overlapping speech in real time?";
        assert_eq!(whole_sentences(q, 60), None);
        assert_eq!(
            whole_sentences("It predicts text first. Then audio follows it.", 30),
            Some("It predicts text first.".to_string())
        );
        assert_eq!(whole_sentences("Short.", 50), Some("Short.".to_string()));
    }

    #[test]
    fn a_budget_spent_to_its_last_few_characters_buys_nothing() {
        assert_eq!(room(0, SLIDE_NARRATION), Some(LINE));
        assert_eq!(room(SLIDE_NARRATION - 200, SLIDE_NARRATION), Some(200));
        // The line that would have been "Moshi util".
        assert_eq!(room(SLIDE_NARRATION - 10, SLIDE_NARRATION), None);
        assert_eq!(room(SLIDE_NARRATION, SLIDE_NARRATION), None);
        assert_eq!(room(SLIDE_NARRATION + 50, SLIDE_NARRATION), None);
        // Exactly the floor is still worth saying.
        assert_eq!(
            room(SLIDE_NARRATION - MIN_LINE, SLIDE_NARRATION),
            Some(MIN_LINE)
        );
    }

    #[test]
    fn a_string_within_budget_is_untouched() {
        assert_eq!(
            fit("  Each slide has spoken words  ", TITLE),
            "Each slide has spoken words"
        );
        assert!(!exceeds("short", TITLE));
    }

    #[test]
    fn an_overlong_string_is_cut_at_a_word_boundary() {
        let long = "The narration engine attaches a spoken script to each slide and every line carries a speaker";
        let cut = fit(long, TITLE);
        assert!(
            cut.chars().count() <= TITLE,
            "budget must hold, got {}",
            cut.chars().count()
        );
        assert!(!cut.ends_with(char::is_whitespace));
        assert!(
            long.starts_with(&cut),
            "the cut must be a prefix of the original"
        );
        assert!(exceeds(long, TITLE));
    }

    /// A single word longer than the budget has no boundary to cut at. It must
    /// still respect the budget rather than overflow the box.
    #[test]
    fn a_single_overlong_word_is_still_cut() {
        let word = "a".repeat(TITLE + 40);
        assert_eq!(fit(&word, TITLE).chars().count(), TITLE);
    }
}

/// The longest one on-slide element may be.
///
/// Short on purpose. A slide is read at a glance while something else is being
/// said, so an element is a few words, not a sentence — and the renderer has to
/// fit it in a box whose size the theme owns. Measured against the failure this
/// replaces: narration lines of up to 320 characters were being sent as slide
/// copy and arrived as text overflowing its box (phase 1 spec, section 3).
pub const ELEMENT: usize = 72;

/// How many elements one slide may carry beside its title.
///
/// Four. A slide that lists six things is a document, and the narration is
/// already carrying the detail — the printed copy is the anchor, not the record.
pub const SLIDE_ELEMENTS: usize = 4;

/// The same words, punctuated so the voice actually pauses where the writing
/// says to.
///
/// # Why this exists
///
/// A dash is a held beat in writing, and the script model uses it constantly:
/// "what Moshi is — let's break it down". Kokoro does not hear it. Sent as
/// written, the two halves run together into one breathless phrase, which is
/// the opposite of what the dash was for.
///
/// Measured on this box against `af_bella`, same words each time:
///
/// | written as | spoken length |
/// |---|---|
/// | `is—let's` (em dash) | 1.886 s |
/// | `is ... let's` | 1.851 s |
/// | `is, let's` | 1.906 s |
/// | `is; let's` | 1.982 s |
/// | `is. let's` | 1.884 s |
/// | `is. Let's` | **2.167 s** |
///
/// So only a full stop buys a real hold — about 280 ms — and **only when the
/// next word is capitalised**. A lowercase continuation after the period is
/// worth nothing at all: 1.884 s against a 1.886 s baseline. That one detail is
/// the whole implementation, and it is why this is not a one-line `replace`.
///
/// Ranges are left alone. An en dash between digits — `2014–2015`, `pages 3–7`
/// — is not a pause, and turning it into a sentence break would say "two
/// thousand fourteen. Two thousand fifteen."
pub fn speakable(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len() + 8);
    let mut i = 0usize;

    while i < chars.len() {
        let c = chars[i];
        if c != '\u{2014}' && c != '\u{2013}' {
            out.push(c);
            i += 1;
            continue;
        }

        // A dash with digits hard against both sides is a range, not a beat.
        let prev = out.chars().rev().find(|c| !c.is_whitespace());
        let next = chars[i + 1..].iter().find(|c| !c.is_whitespace()).copied();
        let tight = chars
            .get(i.wrapping_sub(1))
            .is_some_and(|p| p.is_ascii_digit())
            && chars.get(i + 1).is_some_and(|n| n.is_ascii_digit());
        if tight
            || (prev.is_some_and(|p| p.is_ascii_digit())
                && next.is_some_and(|n| n.is_ascii_digit()))
        {
            out.push(c);
            i += 1;
            continue;
        }

        // Drop any space the dash was sitting on, end the sentence, and start
        // the next one — capitalised, or the stop is not heard.
        while out.ends_with(' ') {
            out.pop();
        }
        // Nothing to end yet: a line that opens on a dash just loses it.
        if out.trim_end().is_empty() {
            i += 1;
            while chars.get(i).is_some_and(|n| n.is_whitespace()) {
                i += 1;
            }
            continue;
        }
        if !out.ends_with(['.', '!', '?']) {
            out.push('.');
        }
        out.push(' ');
        i += 1;
        while chars.get(i).is_some_and(|n| n.is_whitespace()) {
            i += 1;
        }
        if let Some(&n) = chars.get(i) {
            out.extend(n.to_uppercase());
            i += 1;
        }
    }
    out
}

#[cfg(test)]
mod speakable_tests {
    use super::speakable;

    /// The reported line. The halves have to come apart, and the second has to
    /// start with a capital or the stop is silent.
    #[test]
    fn a_dash_becomes_a_stop_the_voice_can_hear() {
        assert_eq!(
            speakable("what Moshi is—let's break it down"),
            "what Moshi is. Let's break it down"
        );
        assert_eq!(
            speakable("what Moshi is — let's break it down"),
            "what Moshi is. Let's break it down"
        );
    }

    /// A range is not a pause. "2014. 2015" would be read as two numbers.
    #[test]
    fn a_range_is_left_alone() {
        assert_eq!(
            speakable("between 2014–2015 it changed"),
            "between 2014–2015 it changed"
        );
        assert_eq!(speakable("pages 3–7"), "pages 3–7");
    }

    /// No doubled punctuation when the clause already ended.
    #[test]
    fn an_existing_stop_is_not_doubled() {
        assert_eq!(
            speakable("It works. — now the why"),
            "It works. Now the why"
        );
    }

    /// A line that opens on a dash has nothing to end; the dash just goes.
    #[test]
    fn a_leading_dash_is_dropped() {
        assert_eq!(
            speakable("— and that is the point"),
            "and that is the point"
        );
    }

    /// Text with no dash comes back untouched, including its own sentences.
    #[test]
    fn ordinary_text_is_unchanged() {
        let t = "Attention lets a token look at every other token. The weights are learned.";
        assert_eq!(speakable(t), t);
    }
}
